"""PC bridge: a small MCP server that gives a remote Claude Code job file and
shell tools on this PC, confined to one project folder.

It speaks MCP over Streamable HTTP with plain JSON responses on 127.0.0.1 only.
The server reaches it through the job's reverse SSH forward. Every request
must carry the job's bearer token.

Tools: pc_read, pc_write, pc_edit, pc_list, pc_glob, pc_grep, pc_run. Every
path argument is relative to the project folder; anything that resolves
outside it (through "..", an absolute path or a link) is refused, and so are
files that usually hold secrets. pc_run refuses commands on a deny list.

The command deny list is a guard against obvious mistakes, not a sandbox: a
command that is allowed runs with the full rights of the user on this PC.

Standard library only.
"""

from __future__ import annotations

import fnmatch
import hmac
import http.server
import json
import locale
import os
import re
import secrets
import signal
import socket
import socketserver
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

__all__ = ["Bridge", "PROTOCOL_VERSIONS", "TOOL_NAMES"]

IS_WINDOWS = os.name == "nt"

# Newest first. The answer to initialize echoes the client's version when it
# is in this list, otherwise it offers the newest.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

MAX_FILE_BYTES = 5 * 1024 * 1024  # pc_read, pc_edit and pc_grep skip larger files
MAX_BODY_BYTES = 64 * 1024 * 1024  # largest HTTP request body accepted
READ_DEFAULT_LIMIT = 2000  # lines returned by pc_read when no limit is given
READ_MAX_LINE = 2000  # longer lines are cut in pc_read output
LIST_MAX = 2000  # entries shown by pc_list
GLOB_MAX = 1000  # paths returned by pc_glob
GREP_MAX_MATCHES = 500  # matching lines returned by pc_grep
GREP_MAX_LINE = 300  # longer matching lines are cut
GREP_DEADLINE_S = 60  # pc_grep stops scanning after this long
RUN_DEFAULT_TIMEOUT = 120  # seconds, pc_run when no timeout_s is given

# Folders that pc_glob and pc_grep never walk into.
SKIP_DIRS = frozenset({".git", "node_modules"})
# Folders whose contents count as secret, wherever they are in the project.
SECRET_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".docker"})
# File names that count as secret (matched case-insensitively).
SECRET_NAMES = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", ".netrc", ".git-credentials",
    ".npmrc", ".pypirc", "*credentials*.json", "*.kdbx", ".claude.json",
)

# Environment variables that are not passed to commands run by pc_run.
_ENV_DROP_WORDS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY")
_ENV_DROP_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_OAUTH")

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WIN_RESERVED_RE = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)
_LOCAL_ORIGIN_RE = re.compile(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Command policy for pc_run
# ---------------------------------------------------------------------------

# Characters that can come right before a command name.
_CMD_START = r"(?:^|[\s;&|(\"'`])"
# Git global options, e.g. "-C dir", "-c key=value", "--no-pager".
_GIT_OPTS = r"(?:\s+-{1,2}\S+(?:\s+(?!-)[^\s;&|]+)?)*"

_DENY_RULES: tuple[tuple[str, str], ...] = (
    (r"\bgit(?:\.exe)?" + _GIT_OPTS + r"\s+push\b", "git push is not allowed"),
    (r"\bgit(?:\.exe)?" + _GIT_OPTS + r"\s+remote\s+(?:add|set-url)\b",
     "changing git remotes is not allowed"),
    (_CMD_START + r"(?:ssh|sftp)(?:\.exe)?(?=\s|$)", "ssh and sftp are not allowed"),
    (_CMD_START + r"(?:scp|rsync)(?:\.exe)?\s[^;&|\n]*?(?:@|[^\s:\\/=]{2,}:)",
     "copying to a remote host is not allowed"),
    (_CMD_START + r"(?:nc|ncat|netcat)(?:\.exe)?(?=\s|$)", "netcat is not allowed"),
    # curl uploads: short flag clusters holding d, T or F (case-sensitive),
    # the long data, upload and form flags, or a POST/PUT/PATCH request.
    (r"\bcurl(?:\.exe)?\b[^;&|\n]*?(?:(?-i:\s-[A-Za-z]*[dTF])"
     r"|\s--(?:data|upload-file|form|json)"
     r"|\s(?:-X|--request)\s*=?\s*['\"]?(?:post|put|patch)\b)",
     "uploading with curl is not allowed"),
    (r"\bwget(?:\.exe)?\b[^;&|\n]*?\s--(?:post-data|post-file|body-data|body-file"
     r"|method\s*=?\s*['\"]?(?:post|put|patch))",
     "uploading with wget is not allowed"),
    (r"\b(?:invoke-webrequest|invoke-restmethod|iwr|irm)\b",
     "Invoke-WebRequest and Invoke-RestMethod are not allowed"),
    (_CMD_START + r"format(?:\.com)?\s+(?:[a-z]:|/)", "formatting a drive is not allowed"),
    (r"\bformat-volume\b|\bmkfs\b", "formatting a drive is not allowed"),
    (r"\bdiskpart\b", "diskpart is not allowed"),
    (_CMD_START + r"reg(?:\.exe)?\s+(?:add|delete|import)\b",
     "changing the registry is not allowed"),
    (r"\bschtasks\b", "scheduled tasks are not allowed"),
    (_CMD_START + r"(?:shutdown|reboot|poweroff)(?:\.exe)?(?=\s|$)"
     r"|\b(?:stop-computer|restart-computer)\b",
     "shutting down or restarting is not allowed"),
    # Secret paths. ".env" only counts when it does not follow a word
    # character, so "process.env" in a script is fine.
    (r"(?<![\w.$-])\.(?:ssh|aws|gnupg)\b|(?<![\w$])\.env\b"
     r"|\bid_(?:rsa|ed25519|ecdsa)\b|\.git-credentials\b|(?<![\w.-])\.netrc\b"
     r"|\.credentials\b|\.claude\.json\b|(?<![\w-])\.claude[\\/]+\.",
     "the command mentions a secret file or folder"),
)

_COMPILED_DENY = tuple((re.compile(p, re.IGNORECASE), why) for p, why in _DENY_RULES)

# Commands that delete files. Their targets must stay inside the project.
_DELETE_CMDS = frozenset({"rm", "rmdir", "rd", "del", "erase", "remove-item", "ri", "unlink", "shred"})
_CMD_EXE_DELETE = frozenset({"rmdir", "rd", "del", "erase"})
_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;&|\n()]")
_TOKEN_RE = re.compile(r'"[^"]*"|\'[^\']*\'|\S+')
_CMD_EXE_FLAG = re.compile(r"^/[a-z](?::\S*)?$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

_WHERE = "The path is relative to the user's project folder on their PC."

_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "pc_read",
        "description": "Read a text file on the user's PC as numbered lines. " + _WHERE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the project folder."},
                "offset": {"type": "integer", "minimum": 1,
                           "description": "First line to show, starting at 1."},
                "limit": {"type": "integer", "minimum": 1,
                          "description": f"Number of lines to show (default {READ_DEFAULT_LIMIT})."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "pc_write",
        "description": "Create or replace a text file on the user's PC, creating missing folders. " + _WHERE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the project folder."},
                "content": {"type": "string", "description": "The full new content of the file."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "pc_edit",
        "description": "Replace an exact string in a text file on the user's PC. " + _WHERE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to the project folder."},
                "old_string": {"type": "string", "description": "Exact text to replace; must be unique unless replace_all is true."},
                "new_string": {"type": "string", "description": "Replacement text."},
                "replace_all": {"type": "boolean", "description": "Replace every occurrence (default false)."},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "pc_list",
        "description": "List a folder on the user's PC (folders end with /). " + _WHERE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder path relative to the project folder (default: the project folder)."},
            },
        },
    },
    {
        "name": "pc_glob",
        "description": "Find files on the user's PC by glob pattern such as src/**/*.py. " + _WHERE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string",
                            "description": "Glob relative to the project folder; supports *, ?, **, [abc] and {a,b}."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "pc_grep",
        "description": "Search file contents on the user's PC with a Python regular expression. " + _WHERE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regular expression; use (?i) for case-insensitive."},
                "path": {"type": "string", "description": "File or folder to search, relative to the project folder (default: all)."},
                "glob": {"type": "string", "description": "Only search files matching this glob, e.g. *.py or src/**/*.ts."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "pc_run",
        "description": "Run a shell command on the user's PC (cmd.exe on Windows, sh elsewhere) with output capped. " + _WHERE,
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run."},
                "cwd": {"type": "string", "description": "Working folder relative to the project folder (default: the project folder)."},
                "timeout_s": {"type": "integer", "minimum": 1,
                              "description": f"Timeout in seconds (default {RUN_DEFAULT_TIMEOUT})."},
            },
            "required": ["command"],
        },
    },
)

TOOL_NAMES = tuple(t["name"] for t in _TOOLS)


class _Refused(Exception):
    """A policy refusal (path outside the project, secret file, denied command)."""


class _ToolError(Exception):
    """A normal tool failure, such as a missing file. The message goes to the caller."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _is_secret_name(name: str) -> bool:
    low = name.lower()
    return any(fnmatch.fnmatchcase(low, pat) for pat in SECRET_NAMES)


def _is_secret_parts(parts: Iterable[str]) -> bool:
    parts = list(parts)
    if not parts:
        return False
    if any(p.lower() in SECRET_DIRS for p in parts):
        return True
    return _is_secret_name(parts[-1])


def _decode_output(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False) or "utf-8", "replace")


def _child_env() -> dict[str, str]:
    """The current environment without variables that look like credentials."""
    env = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(word in upper for word in _ENV_DROP_WORDS):
            continue
        if upper.startswith(_ENV_DROP_PREFIXES):
            continue
        env[key] = value
    return env


def _glob_part(p: str) -> str:
    """Translate a glob to a regex. "**/" matches zero or more folders, "*"
    and "?" never cross a "/", "{a,b}" picks one of its parts."""
    out: list[str] = []
    i, n = 0, len(p)
    while i < n:
        c = p[i]
        if p.startswith("**/", i):
            out.append(r"(?:[^/]*/)*")
            i += 3
            continue
        if p.startswith("**", i):
            out.append(r".*")
            i += 2
            continue
        if c == "*":
            out.append(r"[^/]*")
        elif c == "?":
            out.append(r"[^/]")
        elif c == "[":
            j = p.find("]", i + 1)
            if j <= i + 1:
                out.append(re.escape(c))
            else:
                body = p[i + 1:j]
                negate = body[0] in "!^"
                if negate:
                    body = body[1:]
                body = re.sub(r"([\\\[\]&~|^])", r"\\\1", body)
                out.append("[" + ("^" if negate else "") + body + "]")
                i = j + 1
                continue
        elif c == "{":
            j = p.find("}", i + 1)
            if j == -1 or "{" in p[i + 1:j]:
                out.append(re.escape(c))
            else:
                alts = p[i + 1:j].split(",")
                out.append("(?:" + "|".join(_glob_part(a) for a in alts) + ")")
                i = j + 1
                continue
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def _glob_regex(pattern: str) -> re.Pattern[str]:
    flags = re.DOTALL | (re.IGNORECASE if IS_WINDOWS else 0)
    return re.compile(_glob_part(pattern) + r"\Z", flags)


def _is_link(entry: os.DirEntry) -> bool:
    """True for symlinks and, on Windows, any reparse point (junctions too)."""
    if entry.is_symlink():
        return True
    if IS_WINDOWS:
        try:
            attrs = entry.stat(follow_symlinks=False).st_file_attributes
        except (OSError, AttributeError):
            return False
        return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    return False


class _Capped:
    """Collects a byte stream but keeps only its first and last bytes."""

    def __init__(self, cap: int) -> None:
        self.head_cap = cap // 2
        self.tail_cap = cap - self.head_cap
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self.head_cap - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            extra = len(self.tail) - self.tail_cap
            if extra > 0:
                del self.tail[:extra]

    def text(self) -> str:
        omitted = self.total - len(self.head) - len(self.tail)
        if omitted <= 0:
            return _decode_output(bytes(self.head) + bytes(self.tail))
        return (_decode_output(bytes(self.head))
                + f"\n... [{omitted} bytes omitted] ...\n"
                + _decode_output(bytes(self.tail)))


def _pump(stream, sink: _Capped) -> None:
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            sink.add(chunk)
    except (OSError, ValueError):
        pass


def _kill_tree(proc: subprocess.Popen) -> None:
    """Stop a process and everything it started."""
    if IS_WINDOWS:
        taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
        try:
            subprocess.run([taskkill, "/F", "/T", "/PID", str(proc.pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _rpc_error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _rpc_result(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _check_args(schema: dict[str, Any], args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Check arguments against a tool's flat schema. Integers and booleans
    given as strings are converted, since some clients send them that way."""
    out = dict(args)
    props = schema.get("properties", {})
    for name in schema.get("required", []):
        if name not in args or args[name] is None:
            return out, f"missing required argument: {name}"
    for name, spec in props.items():
        if name not in args or args[name] is None:
            out.pop(name, None)
            continue
        value = args[name]
        kind = spec.get("type")
        if kind == "string":
            if not isinstance(value, str):
                return out, f"argument {name} must be a string"
        elif kind == "integer":
            if isinstance(value, bool):
                return out, f"argument {name} must be an integer"
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            elif isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
                value = int(value)
            if not isinstance(value, int):
                return out, f"argument {name} must be an integer"
            out[name] = value
        elif kind == "boolean":
            if isinstance(value, str) and value.lower() in ("true", "false"):
                value = value.lower() == "true"
            if not isinstance(value, bool):
                return out, f"argument {name} must be true or false"
            out[name] = value
    return out, None


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR would let another program bind the same port,
    # so the port is bound exclusively instead.
    allow_reuse_address = False

    def server_bind(self) -> None:
        if IS_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # HTTPServer.server_bind does a reverse DNS lookup that can stall;
        # the plain TCP bind is enough here.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

    def handle_error(self, request, client_address) -> None:
        # No tracebacks on the console: they could carry request details.
        pass


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "pc-bridge"
    sys_version = ""
    bridge: "Bridge"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Requests are not logged; tool calls go to the audit callback.
        pass

    def _send(self, status: int, body: bytes = b"", headers: Iterable[tuple[str, str]] = ()) -> None:
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers:
            self.send_header(key, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status: int, obj: Any, headers: Iterable[tuple[str, str]] = ()) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), headers)

    def _reject(self, status: int, error: str, headers: Iterable[tuple[str, str]] = ()) -> None:
        # Read a small unread body first: closing a socket with unread data
        # makes Windows reset the connection before the client sees the reply.
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if 0 < n <= 1024 * 1024:
            self.rfile.read(n)
        self.close_connection = True
        self._send_json(status, {"error": error}, headers)

    def _gate(self) -> bool:
        """Check token, path and origin. Sends the error response and returns
        False when the request may not go on."""
        header = self.headers.get("Authorization", "")
        scheme, _, given = header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
                given.strip().encode("utf-8"), self.bridge._token_bytes):
            self._reject(401, "unauthorized", [("WWW-Authenticate", "Bearer")])
            return False
        if self.path.split("?", 1)[0] != "/mcp":
            self._reject(404, "not found")
            return False
        origin = self.headers.get("Origin")
        if origin and not _LOCAL_ORIGIN_RE.match(origin):
            self._reject(403, "origin not allowed")
            return False
        return True

    def _read_body(self) -> bytes | None:
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            parts: list[bytes] = []
            size_total = 0
            while True:
                line = self.rfile.readline(1024)
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    return None
                if size == 0:
                    while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                        pass
                    return b"".join(parts)
                size_total += size
                if size_total > MAX_BODY_BYTES:
                    return None
                parts.append(self.rfile.read(size))
                self.rfile.readline(1024)
        length = self.headers.get("Content-Length")
        try:
            n = int(length) if length is not None else -1
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY_BYTES:
            return None
        return self.rfile.read(n)

    def do_POST(self) -> None:  # noqa: N802
        if not self._gate():
            return
        body = self._read_body()
        if body is None:
            self.close_connection = True
            self._send_json(413, {"error": "missing, malformed or too large body"})
            return
        status, payload, headers = self.bridge._handle_body(body)
        if payload is None:
            self._send(status, b"", headers)
        else:
            self._send_json(status, payload, headers)

    def _not_allowed(self) -> None:
        if not self._gate():
            return
        self._reject(405, "method not allowed", [("Allow", "POST")])

    do_GET = _not_allowed  # noqa: N815
    do_DELETE = _not_allowed  # noqa: N815
    do_PUT = _not_allowed  # noqa: N815
    do_PATCH = _not_allowed  # noqa: N815


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------

class Bridge:
    """MCP server over Streamable HTTP that serves the pc_* tools for one
    project folder. Bind it with start(); the port it returns goes to the
    server, which reaches it through the reverse SSH forward."""

    def __init__(self, root: str | Path, token: str, *, run_timeout_max: int = 1800,
                 output_cap: int = 100_000, extra_deny: Iterable[str] = (),
                 audit: Callable[[dict[str, Any]], None] | None = None,
                 tools: Iterable[str] | None = None) -> None:
        real = os.path.realpath(os.fspath(root))
        if not os.path.isdir(real):
            raise ValueError("root must be an existing folder")
        if not isinstance(token, str) or not token:
            raise ValueError("token must be a non-empty string")
        if output_cap < 2:
            raise ValueError("output_cap must be at least 2")
        if run_timeout_max < 1:
            raise ValueError("run_timeout_max must be at least 1")
        self._root = real
        self._root_cmp = os.path.normcase(real)
        self._token_bytes = token.encode("utf-8")
        self.run_timeout_max = int(run_timeout_max)
        self.output_cap = int(output_cap)
        self._extra_deny = tuple(re.compile(p, re.IGNORECASE) for p in extra_deny)
        self._audit = audit
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._procs: set[subprocess.Popen] = set()
        self._procs_lock = threading.Lock()
        self._handlers: dict[str, Callable[[dict[str, Any]], tuple[str, bool]]] = {
            "pc_read": self._pc_read,
            "pc_write": self._pc_write,
            "pc_edit": self._pc_edit,
            "pc_list": self._pc_list,
            "pc_glob": self._pc_glob,
            "pc_grep": self._pc_grep,
            "pc_run": self._pc_run,
        }
        if tools is not None:
            wanted = set(tools)
            unknown = wanted - set(self._handlers)
            if unknown:
                raise ValueError(f"unknown tools: {', '.join(sorted(unknown))}")
            # A smaller tool set, e.g. read-only jobs or jobs without pc_run.
            self._handlers = {name: handler for name, handler in self._handlers.items() if name in wanted}
        self._schemas = {t["name"]: t["inputSchema"] for t in _TOOLS}

    # -- lifecycle --------------------------------------------------------

    @property
    def root(self) -> str:
        return self._root

    @property
    def port(self) -> int | None:
        return self._server.server_address[1] if self._server else None

    def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        """Start serving in a background thread and return the port."""
        if host != "127.0.0.1":
            raise ValueError("the bridge only binds 127.0.0.1")
        if self._server is not None:
            raise RuntimeError("bridge already started")
        handler = type("_BoundHandler", (_Handler,), {"bridge": self})
        server = _Server((host, port), handler)
        thread = threading.Thread(target=server.serve_forever, name="pc-bridge", daemon=True)
        thread.start()
        self._server, self._thread = server, thread
        return server.server_address[1]

    def stop(self) -> None:
        """Stop serving and stop any command that is still running."""
        server, thread = self._server, self._thread
        self._server = self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        with self._procs_lock:
            procs = list(self._procs)
        for proc in procs:
            if proc.poll() is None:
                _kill_tree(proc)

    # -- JSON-RPC ---------------------------------------------------------

    def _handle_body(self, body: bytes) -> tuple[int, Any, list[tuple[str, str]]]:
        try:
            msg = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            return 400, _rpc_error(None, -32700, "Parse error"), []
        if isinstance(msg, list):
            if not msg:
                return 400, _rpc_error(None, -32600, "Invalid Request"), []
            replies, headers = [], []
            for item in msg:
                reply, extra = self._handle_message(item)
                headers += extra
                if reply is not None:
                    replies.append(reply)
            return (200, replies, headers) if replies else (202, None, headers)
        reply, headers = self._handle_message(msg)
        if reply is None:
            return 202, None, headers
        return 200, reply, headers

    def _handle_message(self, msg: Any) -> tuple[dict[str, Any] | None, list[tuple[str, str]]]:
        if not isinstance(msg, dict):
            return _rpc_error(None, -32600, "Invalid Request"), []
        method = msg.get("method")
        if method is None:
            # A response from the client to a server request; nothing to do.
            if "result" in msg or "error" in msg:
                return None, []
            return _rpc_error(msg.get("id"), -32600, "Invalid Request"), []
        if not isinstance(method, str):
            return _rpc_error(msg.get("id"), -32600, "Invalid Request"), []
        if "id" not in msg:
            return None, []  # notification, e.g. notifications/initialized
        msg_id = msg["id"]
        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return _rpc_error(msg_id, -32602, "params must be an object"), []

        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            result = {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "pc", "version": "1"},
                "instructions": "These tools work on the user's PC. All paths are relative "
                                "to the user's project folder there.",
            }
            return _rpc_result(msg_id, result), [("Mcp-Session-Id", secrets.token_hex(16))]
        if method == "ping":
            return _rpc_result(msg_id, {}), []
        if method == "tools/list":
            return _rpc_result(msg_id, {"tools": [dict(t) for t in _TOOLS if t["name"] in self._handlers]}), []
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments")
            if arguments is None:
                arguments = {}
            if name not in self._handlers:
                return _rpc_error(msg_id, -32602, f"Unknown tool: {name}"), []
            if not isinstance(arguments, dict):
                return _rpc_error(msg_id, -32602, "arguments must be an object"), []
            return _rpc_result(msg_id, self.call_tool(name, arguments)), []
        return _rpc_error(msg_id, -32601, f"Method not found: {method}"), []

    # -- tool dispatch ----------------------------------------------------

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one tool and return its MCP result. Also used by the HTTP layer."""
        event: dict[str, Any] = {"tool": name}
        if name == "pc_run":
            event["command"] = arguments.get("command")
        elif name == "pc_glob":
            event["path"] = arguments.get("pattern")
        else:
            event["path"] = arguments.get("path") or "."
        args, problem = _check_args(self._schemas[name], arguments)
        if problem:
            text, is_error, allowed, reason = problem, True, False, "invalid arguments"
        else:
            try:
                text, is_error = self._handlers[name](args)
                allowed, reason = True, ("ok" if not is_error else "failed")
            except _Refused as exc:
                text, is_error, allowed, reason = f"Refused: {exc}", True, False, str(exc)
            except _ToolError as exc:
                text, is_error, allowed, reason = str(exc), True, True, str(exc)
            except OSError as exc:
                detail = exc.strerror or type(exc).__name__
                text, is_error, allowed, reason = f"{name} failed: {detail}", True, True, detail
            except Exception as exc:  # noqa: BLE001
                text = f"{name} failed: internal error ({type(exc).__name__})"
                is_error, allowed, reason = True, True, "internal error"
        event["allowed"] = allowed
        event["reason"] = reason
        if self._audit is not None:
            try:
                self._audit(event)
            except Exception:  # noqa: BLE001
                pass
        return {"content": [{"type": "text", "text": text}], "isError": is_error}

    # -- confinement ------------------------------------------------------

    def _inside(self, real: str) -> bool:
        try:
            return os.path.commonpath([self._root_cmp, os.path.normcase(real)]) == self._root_cmp
        except ValueError:  # different drives
            return False

    def _rel_parts(self, real: str) -> tuple[str, ...]:
        rel = os.path.relpath(real, self._root)
        return () if rel == "." else tuple(rel.split(os.sep))

    def _resolve(self, raw: Any) -> str:
        """Turn a caller path into a real path inside the root, or refuse.
        Links are followed before the check, and secret files are refused."""
        if raw is None:
            raw = "."
        if not isinstance(raw, str):
            raise _ToolError("path must be a string")
        if raw.strip() in ("", "."):
            return self._root
        if "\x00" in raw:
            raise _Refused("path contains a null byte")
        if raw.startswith(("/", "\\")) or _DRIVE_RE.match(raw) or os.path.isabs(raw):
            raise _Refused("absolute paths are not allowed; use a path relative to the project folder")
        if raw.startswith("~"):
            raise _Refused("home folder paths are not allowed; use a path relative to the project folder")
        seps = r"[\\/]" if IS_WINDOWS else r"/"
        parts = [p for p in re.split(seps, raw) if p not in ("", ".")]
        if IS_WINDOWS:
            # A colon would name a drive or an alternate data stream; Windows
            # drops trailing dots and spaces, so ".env." would open ".env".
            if ":" in raw:
                raise _Refused("colons are not allowed in paths")
            for part in parts:
                if part != ".." and (part.endswith((".", " ")) or _WIN_RESERVED_RE.match(part)):
                    raise _Refused("path uses a reserved Windows name")
        real = os.path.realpath(os.path.join(self._root, raw))
        if not self._inside(real):
            raise _Refused("path is outside the project folder")
        requested = os.path.normpath(raw).replace("\\", "/").split("/")
        if _is_secret_parts(self._rel_parts(real)) or _is_secret_parts(requested):
            raise _Refused("this file may hold secrets")
        return real

    def _shown(self, raw: Any) -> str:
        return raw if isinstance(raw, str) and raw.strip() else "."

    def _walk_files(self, base_real: str, base_rel: str) -> Iterator[tuple[str, str]]:
        """Yield (path, relative posix path) for files under a folder, skipping
        .git, node_modules, secret folders and files, and links that lead
        outside the project. Links to folders inside are walked once."""
        stack = [(base_real, base_rel)]
        seen = {os.path.normcase(base_real)}
        while stack:
            folder, folder_rel = stack.pop()
            try:
                with os.scandir(folder) as it:
                    entries = sorted(it, key=lambda e: e.name, reverse=True)
            except OSError:
                continue
            files: list[tuple[str, str]] = []
            for entry in entries:
                name = entry.name
                low = name.lower()
                rel = f"{folder_rel}/{name}" if folder_rel else name
                try:
                    link = _is_link(entry)
                    is_dir = entry.is_dir()
                except OSError:
                    continue
                if is_dir:
                    if low in SKIP_DIRS or low in SECRET_DIRS:
                        continue
                    if link:
                        real = os.path.realpath(entry.path)
                        key = os.path.normcase(real)
                        if not self._inside(real) or key in seen:
                            continue
                        seen.add(key)
                    stack.append((entry.path, rel))
                    continue
                if _is_secret_name(name):
                    continue
                if link:
                    real = os.path.realpath(entry.path)
                    if not self._inside(real) or _is_secret_parts(self._rel_parts(real)):
                        continue
                files.append((entry.path, rel))
            yield from reversed(files)

    # -- tools ------------------------------------------------------------

    def _read_text_file(self, real: str, shown: str) -> bytes:
        if os.path.isdir(real):
            raise _ToolError(f"{shown} is a folder; use pc_list")
        if not os.path.isfile(real):
            raise _ToolError(f"{shown} does not exist")
        size = os.path.getsize(real)
        if size > MAX_FILE_BYTES:
            raise _ToolError(f"{shown} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
        with open(real, "rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
        if b"\x00" in data[:8192]:
            raise _ToolError(f"{shown} looks like a binary file")
        return data

    def _pc_read(self, args: dict[str, Any]) -> tuple[str, bool]:
        shown = self._shown(args.get("path"))
        real = self._resolve(args.get("path"))
        data = self._read_text_file(real, shown)
        if not data:
            return f"{shown} is empty.", False
        lines = data.decode("utf-8", "replace").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        total = len(lines)
        start = max(1, args.get("offset") or 1)
        limit = max(1, args.get("limit") or READ_DEFAULT_LIMIT)
        if start > total:
            return f"{shown} has only {total} lines.", False
        end = min(total, start + limit - 1)
        out = []
        used = 0
        for number in range(start, end + 1):
            line = lines[number - 1].rstrip("\r")
            if len(line) > READ_MAX_LINE:
                line = line[:READ_MAX_LINE] + " [line cut]"
            row = f"{number:6}\t{line}"
            used += len(row) + 1
            if used > self.output_cap and out:
                # Keep replies small; the caller continues with offset.
                end = number - 1
                break
            out.append(row)
        if start > 1 or end < total:
            out.append(f"[lines {start}-{end} of {total}]")
        return "\n".join(out), False

    def _pc_write(self, args: dict[str, Any]) -> tuple[str, bool]:
        shown = self._shown(args.get("path"))
        real = self._resolve(args.get("path"))
        if real == self._root or os.path.isdir(real):
            raise _ToolError(f"{shown} is a folder")
        data = args["content"].encode("utf-8")
        existed = os.path.exists(real)
        os.makedirs(os.path.dirname(real), exist_ok=True)
        with open(real, "wb") as fh:
            fh.write(data)
        return f"{'Replaced' if existed else 'Created'} {shown} ({len(data)} bytes).", False

    def _pc_edit(self, args: dict[str, Any]) -> tuple[str, bool]:
        shown = self._shown(args.get("path"))
        real = self._resolve(args.get("path"))
        old, new = args["old_string"], args["new_string"]
        replace_all = bool(args.get("replace_all"))
        if old == "":
            raise _ToolError("old_string must not be empty")
        if old == new:
            raise _ToolError("old_string and new_string are the same")
        data = self._read_text_file(real, shown)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise _ToolError(f"{shown} is not UTF-8 text") from None
        count = text.count(old)
        if count == 0 and "\r\n" in text and "\n" in old and "\r\n" not in old:
            # The file uses CRLF line ends but the strings came with LF.
            old = old.replace("\n", "\r\n")
            new = new.replace("\n", "\r\n")
            count = text.count(old)
        if count == 0:
            raise _ToolError(f"old_string was not found in {shown}")
        if count > 1 and not replace_all:
            raise _ToolError(f"old_string was found {count} times in {shown}; "
                             "add more context to make it unique or set replace_all")
        text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        with open(real, "wb") as fh:
            fh.write(text.encode("utf-8"))
        return f"Edited {shown} ({count if replace_all else 1} replacement{'s' if replace_all and count != 1 else ''}).", False

    def _pc_list(self, args: dict[str, Any]) -> tuple[str, bool]:
        shown = self._shown(args.get("path"))
        real = self._resolve(args.get("path"))
        if not os.path.isdir(real):
            raise _ToolError(f"{shown} is not a folder")
        rows = []
        with os.scandir(real) as it:
            for entry in it:
                name = entry.name
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    is_dir = False
                if is_dir and name.lower() in SECRET_DIRS:
                    continue
                if not is_dir and _is_secret_name(name):
                    continue
                rows.append(name + ("/" if is_dir else ""))
        rows.sort(key=str.lower)
        if not rows:
            return f"{shown} is empty.", False
        note = ""
        if len(rows) > LIST_MAX:
            note = f"\n[showing {LIST_MAX} of {len(rows)} entries]"
            rows = rows[:LIST_MAX]
        return "\n".join(rows) + note, False

    def _clean_glob(self, pattern: str) -> str:
        pat = pattern.strip().replace("\\", "/")
        while pat.startswith("./"):
            pat = pat[2:]
        if not pat:
            raise _ToolError("pattern must not be empty")
        if pat.startswith("/") or _DRIVE_RE.match(pat) or pat.startswith("~") or "\x00" in pat:
            raise _Refused("the pattern must be relative to the project folder")
        if ".." in pat.split("/"):
            raise _Refused("the pattern must stay inside the project folder")
        return pat

    def _glob_base(self, pat: str) -> tuple[str, str]:
        """The folder a glob can be walked from: its leading literal parts."""
        base_parts = []
        for part in pat.split("/")[:-1]:
            if any(ch in part for ch in "*?[{"):
                break
            base_parts.append(part)
        base_rel = "/".join(base_parts)
        return (self._resolve(base_rel) if base_rel else self._root), base_rel

    def _pc_glob(self, args: dict[str, Any]) -> tuple[str, bool]:
        pat = self._clean_glob(args["pattern"])
        regex = _glob_regex(pat)
        base_real, base_rel = self._glob_base(pat)
        if not os.path.isdir(base_real):
            return "No files found.", False
        found: list[str] = []
        truncated = False
        for _path, rel in self._walk_files(base_real, base_rel):
            if regex.match(rel):
                if len(found) >= GLOB_MAX:
                    truncated = True
                    break
                found.append(rel)
        if not found:
            return "No files found.", False
        found.sort()
        text = "\n".join(found)
        if truncated:
            text += f"\n[stopped at {GLOB_MAX} results; use a narrower pattern]"
        return text, False

    def _pc_grep(self, args: dict[str, Any]) -> tuple[str, bool]:
        try:
            regex = re.compile(args["pattern"])
        except re.error as exc:
            raise _ToolError(f"invalid regular expression: {exc}") from None
        file_filter = None
        filter_on_path = False
        if args.get("glob"):
            gl = self._clean_glob(args["glob"])
            file_filter = _glob_regex(gl)
            filter_on_path = "/" in gl
        real = self._resolve(args.get("path"))
        if os.path.isdir(real):
            base_rel = "/".join(self._rel_parts(real))
            targets: Iterable[tuple[str, str]] = self._walk_files(real, base_rel)
        elif os.path.isfile(real):
            targets = [(real, "/".join(self._rel_parts(real)))]
        else:
            raise _ToolError(f"{self._shown(args.get('path'))} does not exist")

        deadline = time.monotonic() + GREP_DEADLINE_S
        out: list[str] = []
        used = 0
        note = ""
        for path, rel in targets:
            if time.monotonic() > deadline:
                note = f"\n[stopped after {GREP_DEADLINE_S} s; narrow the search]"
                break
            if file_filter is not None:
                subject = rel if filter_on_path else rel.rsplit("/", 1)[-1]
                if not file_filter.match(subject):
                    continue
            try:
                if os.path.getsize(path) > MAX_FILE_BYTES:
                    continue
                with open(path, "rb") as fh:
                    data = fh.read(MAX_FILE_BYTES + 1)
            except OSError:
                continue
            if b"\x00" in data[:8192]:
                continue
            for number, line in enumerate(data.decode("utf-8", "replace").split("\n"), 1):
                line = line.rstrip("\r")
                if not regex.search(line):
                    continue
                if len(line) > GREP_MAX_LINE:
                    line = line[:GREP_MAX_LINE] + " [line cut]"
                row = f"{rel}:{number}:{line}"
                used += len(row) + 1
                if len(out) >= GREP_MAX_MATCHES or used > self.output_cap:
                    note = f"\n[stopped at {len(out)} matches; narrow the search]"
                    break
                out.append(row)
            if note:
                break
        if not out:
            return "No matches." + note, False
        return "\n".join(out) + note, False

    def command_refusal(self, command: str, cwd: str | None = None) -> str | None:
        """Why a command is refused, or None when it may run. cwd is a real
        path inside the root (default: the root)."""
        for regex, why in _COMPILED_DENY:
            if regex.search(command):
                return why
        for regex in self._extra_deny:
            if regex.search(command):
                return "the command matches a deny rule"
        return self._delete_refusal(command, cwd or self._root)

    def _delete_refusal(self, command: str, cwd: str) -> str | None:
        for segment in _SEGMENT_SPLIT.split(command):
            tokens = [t.strip("\"'") for t in _TOKEN_RE.findall(segment)]
            for i, token in enumerate(tokens):
                name = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
                if name.endswith(".exe"):
                    name = name[:-4]
                if name not in _DELETE_CMDS:
                    continue
                for arg in tokens[i + 1:]:
                    if not arg or arg.startswith("-"):
                        continue
                    if name in _CMD_EXE_DELETE and _CMD_EXE_FLAG.match(arg):
                        continue
                    if not self._delete_target_ok(arg, cwd):
                        return "deleting outside the project folder is not allowed"
                break
        return None

    def _delete_target_ok(self, arg: str, cwd: str) -> bool:
        if arg.startswith("~") or any(ch in arg for ch in "$%`"):
            return False
        if not IS_WINDOWS:
            # Treat backslashes as separators too, so "..\x" counts as "../x".
            arg = arg.replace("\\", "/")
        is_abs = arg.startswith(("/", "\\")) or bool(_DRIVE_RE.match(arg))
        if is_abs and not IS_WINDOWS and _DRIVE_RE.match(arg):
            return False
        candidate = arg if is_abs else os.path.join(cwd, arg)
        real = os.path.realpath(os.path.normpath(candidate))
        if os.path.normcase(real) == self._root_cmp:
            return False  # the whole project folder
        return self._inside(real)

    def _pc_run(self, args: dict[str, Any]) -> tuple[str, bool]:
        command = args["command"]
        if not command.strip():
            raise _ToolError("command must not be empty")
        cwd = self._resolve(args.get("cwd"))
        if not os.path.isdir(cwd):
            raise _ToolError(f"{self._shown(args.get('cwd'))} is not a folder")
        reason = self.command_refusal(command, cwd)
        if reason:
            raise _Refused(reason)
        timeout = args.get("timeout_s") or RUN_DEFAULT_TIMEOUT
        timeout = max(1, min(int(timeout), self.run_timeout_max))

        kwargs: dict[str, Any] = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                       | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            kwargs["start_new_session"] = True  # own process group, killed as one
        proc = subprocess.Popen(command, shell=True, cwd=cwd, env=_child_env(),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, **kwargs)
        with self._procs_lock:
            self._procs.add(proc)
        out, err = _Capped(self.output_cap), _Capped(self.output_cap)
        pumps = [threading.Thread(target=_pump, args=(proc.stdout, out), daemon=True),
                 threading.Thread(target=_pump, args=(proc.stderr, err), daemon=True)]
        for t in pumps:
            t.start()
        deadline = time.monotonic() + timeout
        timed_out = False
        note = ""
        try:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_tree(proc)
            for t in pumps:
                t.join(timeout=5 if timed_out else max(0.0, deadline - time.monotonic()))
            if not timed_out and any(t.is_alive() for t in pumps):
                # The shell ended but a process it started still holds the
                # output open; stop it at the time limit.
                _kill_tree(proc)
                for t in pumps:
                    t.join(timeout=5)
                note = "A background process kept running past the time limit and was stopped.\n"
            try:
                code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                code = None
        finally:
            with self._procs_lock:
                self._procs.discard(proc)
            # A pipe still being read by a stuck reader is left to it;
            # closing it from here can block on Windows.
            for stream, pump in zip((proc.stdout, proc.stderr), pumps):
                if not pump.is_alive():
                    try:
                        stream.close()
                    except OSError:
                        pass

        head = (f"Timed out after {timeout} s; the command and everything it started were stopped.\n"
                if timed_out else f"Exit code: {code}\n")
        text = f"{head}{note}--- stdout ---\n{out.text()}\n--- stderr ---\n{err.text()}"
        return text, timed_out or code != 0
