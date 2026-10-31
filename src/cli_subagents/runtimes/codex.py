"""Codex adapter: `codex exec --json` workers with a shared setup across accounts.

Each account has its own `CODEX_HOME` holding its login (`auth.json`, forced to
file storage so logins never share a keyring entry), sessions and state
databases. From the primary home, every run gets:

* `config.toml` (MCP servers, model defaults, features), taken from the primary
  table by table, except the account's own `[projects.*]` (trust),
  `[hooks.state.*]` and `[notice.*]` tables, which Codex writes per home;
* `skills/` and `plugins/` entries, linked; `AGENTS.md`, hard-linked.

Every run, on the primary account too, turns this tool's own MCP server off
with `-c` overrides so workers can't start subagents of their own.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tomllib
from typing import Any

from cli_subagents import accounts as pool, sync
from cli_subagents.hosts import parent_note
from cli_subagents.config import Account, RuntimeConfig, state_dir
from cli_subagents import mcp_translate
from cli_subagents.fsutil import copy_exclusive, file_lock, find_program, is_link, no_window_kwargs, same_path, write_text
from cli_subagents.runtimes.base import (
    WORKER_TOOLS_NOTE,
    parent_server,
    AUTH,
    ERROR,
    SUCCESS,
    USAGE_LIMIT,
    AttemptResult,
    JobSpec,
    Launch,
    clean_environment,
)

SYNC_PLAN = sync.SyncPlan(folders=("skills", "plugins"), files=("AGENTS.md",))  # entries linked, file hard-linked
FILE_AUTH = "cli_auth_credentials_store=file"
SELF_COMMANDS = {"csa", "csa.exe", "cli-subagents-mcp", "cli-subagents-mcp.exe"}
PLUGIN_SERVER = "cli-subagents"  # this tool's server name in its plugin manifests
BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")

WORKER_NOTE = (
    "[You are running as a delegated subagent for another agent session, which will read your "
    "final message. Work only on this task, stay inside its scope, and end with a short summary "
    "of what you did, the result, and anything left open.]\n\n"
)

LIMIT_RE = re.compile(r"usage limit|rate limit|too many requests|\b429\b|quota|try again at", re.IGNORECASE)
CAPACITY_RE = re.compile(r"selected model is at capacity|\bserver(?:[ _]is)?[ _]overloaded\b", re.IGNORECASE)
AUTH_RE = re.compile(r"not logged in|\b401\b|unauthorized|log ?in again|token (has )?expired|refresh token", re.IGNORECASE)
TOOL_ITEMS = {"command_execution", "file_change", "mcp_tool_call", "web_search"}


ROLLOUT_TAIL_BYTES = 4_000_000
WINDOW_NAMES = {300: "five_hour", 10080: "seven_day"}


def find_rollout(home: Path, thread_id: str) -> Path | None:
    """The session file Codex wrote for a thread: <home>/sessions/YYYY/MM/DD/rollout-<time>-<thread>.jsonl."""

    if not thread_id or not re.fullmatch(r"[A-Za-z0-9-]+", thread_id):
        return None
    matches = list((home / "sessions").glob(f"*/*/*/rollout-*-{thread_id}.jsonl"))
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def last_rate_limits(path: Path) -> dict[str, Any] | None:
    """The last `rate_limits` object in a rollout file. Reads nothing else from the conversation."""

    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - ROLLOUT_TAIL_BYTES))
            lines = handle.read().splitlines()
    except OSError:
        return None
    for raw in reversed(lines):
        if b'"rate_limits"' not in raw:
            continue
        try:
            payload = json.loads(raw).get("payload")
        except (ValueError, AttributeError):
            continue  # a cut-off first line, or not an event
        limits = payload.get("rate_limits") if isinstance(payload, dict) else None
        if isinstance(limits, dict) and isinstance(limits.get("primary") or limits.get("secondary"), dict):
            return limits
    return None


def usage_windows(limits: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    """Codex's primary/secondary windows as the pool stores them: utilization 0..1 and reset epoch seconds."""

    windows: dict[str, dict[str, float | None]] = {}
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if not isinstance(window, dict) or not isinstance(window.get("used_percent"), (int, float)):
            continue
        minutes = window.get("window_minutes")
        name = WINDOW_NAMES.get(int(minutes)) if isinstance(minutes, (int, float)) else None
        name = name or ("five_hour" if key == "primary" else "seven_day")
        resets = window.get("resets_at")
        windows[name] = {
            "utilization": float(window["used_percent"]) / 100.0,
            "resets_at": float(resets) if isinstance(resets, (int, float)) else None,
        }
    return windows


def _default_home() -> Path:
    return Path.home() / ".codex"


def _is_self_server(server: Any) -> bool:
    if not isinstance(server, dict):
        return False
    command = Path(str(server.get("command") or "")).name.lower()
    args = server.get("args")
    args = [str(item) for item in args] if isinstance(args, list) else []
    return command in SELF_COMMANDS or any("cli_subagents" in item for item in args)


def config_files(home: Path, cwd: str) -> list[Path]:
    """The config.toml files a Codex run reads: the account home's, then project ones up to the repository root."""

    files = [home / "config.toml"]
    project: list[Path] = []
    folder = Path(cwd)
    for current in (folder, *folder.parents):
        project.append(current / ".codex" / "config.toml")
        if (current / ".git").exists():
            break
    else:
        project = project[:1]  # not in a repository: only the folder itself
    files += [path for path in reversed(project) if not same_path(path.parent, home)]
    return [path for path in files if path.is_file()]


def self_server_overrides(files: list[Path]) -> list[str]:
    """`-c` arguments that turn this tool's own MCP server off for a worker.

    Codex hands MCP servers a filtered environment, so CLI_SUBAGENTS_DEPTH never
    reaches a nested `csa mcp`; the recursion guard has to happen here. Every
    configured server that runs csa or the cli_subagents module is disabled, and
    so is `cli-subagents`, the name the plugin uses.

    Verified with codex-cli 0.162.1: `-c mcp_servers.<name>.enabled=false` for a
    name config.toml doesn't define (absent, or provided only by a plugin) makes
    Codex refuse to load its config ("invalid transport"). Such a name also gets a
    stub command; that loads, and it replaces a plugin's server of the same name.
    Codex splits `-c` keys on every dot, so names must be bare TOML keys.
    """

    defined: set[str] = set()
    own: set[str] = {PLUGIN_SERVER}
    for path in files:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise RuntimeError(
                f"could not read Codex config {path}: {exc}. Fix it so CLI Subagents can turn its own MCP "
                "server off for the worker."
            ) from exc
        servers = data.get("mcp_servers") or {}
        if not isinstance(servers, dict):
            raise RuntimeError(f"Codex config {path}: mcp_servers is not a table")
        for name, server in servers.items():
            defined.add(str(name))
            if _is_self_server(server):
                own.add(str(name))
    overrides: list[str] = []
    for name in sorted(own):
        if not BARE_KEY_RE.fullmatch(name):
            raise RuntimeError(
                f"Codex MCP server '{name}' runs CLI Subagents but its name can't be turned off with -c; "
                "rename it to letters, digits, '-' and '_' only"
            )
        if name not in defined:
            overrides += ["-c", f"mcp_servers.{name}.command='csa'"]
        overrides += ["-c", f"mcp_servers.{name}.enabled=false"]
    return overrides


def configured_server_names(files: list[Path]) -> set[str]:
    """Names of the MCP servers the user's Codex config files define (unreadable files count as none)."""

    names: set[str] = set()
    for path in files:
        try:
            servers = tomllib.loads(path.read_text(encoding="utf-8")).get("mcp_servers")
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        if isinstance(servers, dict):
            names.update(str(name) for name in servers)
    return names


def parent_mcp(config: RuntimeConfig, spec: JobSpec, files: list[Path], env: dict[str, str]) -> mcp_translate.CodexServers | None:
    """The Claude session's MCP servers as Codex overrides, or None when they aren't shared.

    Only for jobs started from Claude Code; a Codex session's workers use their own config.
    """

    if not config.share_parent_mcp or not spec.parent or spec.parent.get("host") != "claude":
        return None
    try:
        from cli_subagents.config import default_primary, load_config

        try:
            primary = load_config().runtime("claude").primary
        except (OSError, ValueError):
            primary = default_primary("claude")
        collected = mcp_translate.collect_servers(primary, spec.cwd)
    except Exception as exc:  # sharing is a convenience: never fail the job over it
        result = mcp_translate.CodexServers()
        result.warnings.append(f"could not read the Claude session's MCP servers: {type(exc).__name__}")
        return result
    result = mcp_translate.to_codex(collected.servers, configured_server_names(files), env)
    result.warnings = [*collected.warnings, *result.warnings]
    return result


def _is_windows() -> bool:
    return os.name == "nt"


def reasoning_summary_args(config: RuntimeConfig, files: list[Path]) -> list[str]:
    """Request summaries unless the user's Codex config already chooses their detail."""

    if config.reasoning_summary == "off":
        return []
    profile = None
    profiles: dict[str, Any] = {}
    for path in files:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if "model_reasoning_summary" in data:
            return []
        profile = data.get("profile", profile)
        for name, settings in (data.get("profiles") or {}).items():
            if isinstance(settings, dict):
                profiles.setdefault(name, {}).update(settings)
    if isinstance(profile, str) and "model_reasoning_summary" in profiles.get(profile, {}):
        return []
    return ["-c", f"model_reasoning_summary={json.dumps(config.reasoning_summary)}"]


def configured_windows_sandbox(home: Path) -> str | None:
    """The `[windows] sandbox` value in the account home's config.toml, or None when it sets none."""

    try:
        data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    table = data.get("windows")
    value = table.get("sandbox") if isinstance(table, dict) else None
    return str(value) if value is not None else None


def windows_sandbox_args(config: RuntimeConfig, home: Path, access: str) -> list[str]:
    """`-c windows.sandbox=...` for sandboxed jobs on Windows when the user's Codex config has none.

    Without a `[windows] sandbox` setting Codex 0.162.1 has no sandbox backend and refuses
    every command in a sandboxed run. "unelevated" works without admin rights.
    """

    if access == "full" or not _is_windows() or config.windows_sandbox == "off":
        return []
    if configured_windows_sandbox(home) is not None:
        return []
    return ["-c", f'windows.sandbox="{config.windows_sandbox}"']


def windows_sandbox_summary(config: RuntimeConfig) -> str:
    """One line for `csa doctor` on Windows: which sandbox Codex read/edit jobs will use."""

    configured = configured_windows_sandbox(config.primary)
    if configured is not None:
        return f'Codex sandbox on Windows: "{configured}" (from {config.primary / "config.toml"})'
    if config.windows_sandbox == "off":
        return "Codex sandbox on Windows: none set; CLI Subagents adds nothing ([codex] windows_sandbox = \"off\"), so read and edit jobs can't run commands"
    return (f'Codex sandbox on Windows: "{config.windows_sandbox}" (set by CLI Subagents for read and edit jobs; '
            "no [windows] sandbox in your Codex config)")


def _toml_literal(value: str) -> str:
    return "'" + value + "'" if "'" not in value else json.dumps(value)


def _parent_overrides(job_id: str) -> list[str]:
    server = parent_server(job_id)
    prefix = "mcp_servers.csa_parent"
    args = "[" + ", ".join(_toml_literal(item) for item in server["args"]) + "]"
    overrides = [f"{prefix}.command={_toml_literal(server['command'])}", f"{prefix}.args={args}"]
    for key, value in server["env"].items():
        overrides.append(f"{prefix}.env.{key}={_toml_literal(value)}")
    # Codex 0.162: Auto still prompts for unannotated MCP tools; Approve bypasses
    # that prompt. Scope this to the three parent tools, including inherited overrides.
    # https://learn.chatgpt.com/docs/config-file/config-reference
    # https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/core/src/mcp_tool_call.rs
    overrides.append(f'{prefix}.default_tools_approval_mode="approve"')
    for name in ("message_parent", "ask_parent", "check_parent_messages"):
        overrides.append(f'{prefix}.tools.{name}.approval_mode="approve"')
    return [part for override in overrides for part in ("-c", override)]


# Tables Codex writes into each home itself; the account's copy wins over the primary's.
ACCOUNT_TABLES = (("projects",), ("hooks", "state"), ("notice",))
TABLE_HEADER_RE = re.compile(r"^\s*\[(\[?)\s*(.+?)\s*\]\]?\s*(?:#.*)?$")


def _key_path(text: str) -> tuple[str, ...] | None:
    """Split a TOML table name (`projects.'/repo'`) into its keys; None if it isn't one."""

    keys: list[str] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index] in " \t":
            index += 1
        if index < len(text) and text[index] in "\"'":
            quote = text[index]
            end = index + 1
            while end < len(text) and text[end] != quote:
                end += 2 if quote == '"' and text[end] == "\\" else 1
            if end >= len(text):
                return None
            raw = text[index + 1 : end]
            if quote == '"':
                try:
                    raw = json.loads('"' + raw + '"')
                except ValueError:
                    pass
            keys.append(raw)
            index = end + 1
        else:
            match = BARE_KEY_RE.match(text, index)
            if not match:
                return None
            keys.append(match.group(0))
            index = match.end()
        while index < len(text) and text[index] in " \t":
            index += 1
        if index == len(text):
            break
        if text[index] != ".":
            return None
        index += 1
    return tuple(keys) if keys else None


def _toml_blocks(text: str) -> list[tuple[tuple[str, ...] | None, bool, str]]:
    """Cut a TOML document into (table name, is array table, text) blocks; the root block has no name."""

    blocks: list[tuple[tuple[str, ...] | None, bool, str]] = []
    name: tuple[str, ...] | None = None
    array = False
    lines: list[str] = []
    in_string: str | None = None  # an open multi-line string's delimiter
    for line in text.splitlines(keepends=True):
        match = None if in_string else TABLE_HEADER_RE.match(line)
        path = _key_path(match.group(2)) if match else None
        if path is not None:
            blocks.append((name, array, "".join(lines)))
            name, array, lines = path, bool(match.group(1)), []
        lines.append(line)
        for delimiter in ('"""', "'''"):
            if in_string in (None, delimiter) and line.count(delimiter) % 2:
                in_string = None if in_string else delimiter
    blocks.append((name, array, "".join(lines)))
    return blocks


def _account_owned(name: tuple[str, ...] | None) -> bool:
    return name is not None and any(name[: len(prefix)] == prefix for prefix in ACCOUNT_TABLES)


def merge_config(primary: str, account: str) -> str:
    """The primary config with the account's own `[projects.*]`, `[hooks.state.*]` and `[notice.*]` tables kept.

    Everything else, the root keys included, comes from the primary. A primary table
    of those kinds stays only where the account has no table of the same name.
    """

    kept = [(name, array, text) for name, array, text in _toml_blocks(account) if _account_owned(name)]
    if not kept:
        return primary
    own = {name for name, _array, _text in kept}
    parts = [text for name, _array, text in _toml_blocks(primary) if not (_account_owned(name) and name in own)]
    merged = "".join(parts)
    if merged and not merged.endswith("\n"):
        merged += "\n"
    for _name, _array, text in kept:
        merged += text if text.endswith("\n") else text + "\n"
    return merged


class CodexParser:
    """Turns `codex exec --json` events into progress and a result."""

    def __init__(self, account: str | None = None, home: Path | None = None) -> None:
        self.account = account  # with home: where to find the rollout file for the rate-limit snapshot
        self.home = home
        self.session_id: str | None = None
        self.init: dict[str, Any] = {}
        self.last_text = ""
        self.tool_uses = 0
        self.last_tool: str | None = None
        self.usage: dict[str, Any] | None = None
        self.completed = False
        self.failures: list[str] = []  # turn.failed
        self.error_events: list[str] = []  # top-level `error` events (retries, stream errors)
        self.error_items: list[str] = []  # `error` items inside a turn
        self._completed_error_count = 0

    def feed(self, event: dict[str, Any]) -> dict[str, Any] | None:
        kind = event.get("type")
        if kind == "thread.started":
            self.session_id = event.get("thread_id")
        elif kind in ("item.completed", "item.started"):
            item = event.get("item") or {}
            item_type = item.get("type")
            if kind == "item.completed" and item_type == "agent_message" and str(item.get("text") or "").strip():
                self.last_text = str(item["text"])
            elif kind == "item.started" and item_type in TOOL_ITEMS:
                self.tool_uses += 1
                self.last_tool = item_type
            elif item_type == "error" and item.get("message"):
                self.error_items.append(str(item["message"]))
        elif kind == "turn.completed":
            self.completed = True
            self.usage = event.get("usage")
            self._completed_error_count = len(self.error_events)
        elif kind == "turn.failed":
            message = (event.get("error") or {}).get("message") or "turn failed"
            info = (event.get("error") or {}).get("codex_error_info") or (event.get("error") or {}).get("code")
            if info:
                message = f"{message} ({info})"
            self.failures.append(str(message))
        elif kind == "error" and event.get("message"):
            info = event.get("codex_error_info") or event.get("code")
            self.error_events.append(str(event["message"]) + (f" ({info})" if info else ""))
        return None

    def progress(self) -> dict[str, Any]:
        progress: dict[str, Any] = {"tool_uses": self.tool_uses, "last_tool": self.last_tool, "last_text": self.last_text[-600:]}
        warnings = [*self.error_events, *self.error_items]
        if warnings:
            progress["warnings"] = [item[:300] for item in warnings[-5:]]
        return progress

    def _record_usage(self) -> float | None:
        """Store the account's rate-limit windows from the rollout file; returns when a full window resets."""

        if not (self.account and self.home and self.session_id):
            return None
        try:
            rollout = find_rollout(self.home, self.session_id)
            limits = last_rate_limits(rollout) if rollout else None
            windows = usage_windows(limits) if limits else {}
            if not windows:
                return None
            info = {"unifiedWindows": {name: {"utilization": w["utilization"], "resetsAt": w["resets_at"]} for name, w in windows.items()}}
            pool.record_rate_limit_event("codex", self.account, info)
        except Exception:
            return None  # usage ranking is a convenience: never fail an attempt over it
        full = [w["resets_at"] for w in windows.values() if w["utilization"] >= 1.0 and w["resets_at"]]
        return max(full) if full else None

    def finish(self, exit_code: int | None, stderr_tail: str) -> AttemptResult:
        resets_at = self._record_usage()
        result = self._finish(exit_code, stderr_tail)
        if result.outcome == USAGE_LIMIT and result.resets_at is None:
            result.resets_at = resets_at
        return result

    def _finish(self, exit_code: int | None, stderr_tail: str) -> AttemptResult:
        common = {"session_id": self.session_id, "usage": self.usage}
        if self.completed and not self.failures and exit_code == 0:
            # A finished turn is a success; errors Codex recovered from stay warnings in progress.
            return AttemptResult(SUCCESS, text=self.last_text, **common)
        # A completed turn can be followed by a fatal CLI failure. Keep earlier
        # recovered errors as warnings, but classify later errors and stderr.
        sources = [*self.failures, *self.error_events[self._completed_error_count:], stderr_tail]
        haystack = "\n".join(part for part in sources if part)
        details = "\n".join(part for part in (haystack, *self.error_events, *self.error_items, stderr_tail) if part)
        message = next((line.strip() for line in details.splitlines() if line.strip()), "")[:300]
        terminal = self.failures[-1:] or self.error_events[self._completed_error_count:][-1:]
        if CAPACITY_RE.search("\n".join([*terminal, stderr_tail])):
            return AttemptResult(ERROR, message=message or "model at capacity", text=self.last_text,
                                 error_text=details[-4000:], at_capacity=True, **common)
        if LIMIT_RE.search(haystack):
            return AttemptResult(
                USAGE_LIMIT, message=message or "usage limit reached", text=self.last_text, error_text=details[-4000:], **common
            )
        if AUTH_RE.search(haystack):
            return AttemptResult(AUTH, message=message or "authentication failed", text=self.last_text, **common)
        message = f"codex exited with status {exit_code}" + (f": {message}" if message else "")
        return AttemptResult(ERROR, message=message, text=self.last_text, **common)


class CodexRuntime:
    name = "codex"
    _attempt_account: str | None = None
    _attempt_home: Path | None = None

    def find_binary(self, config: RuntimeConfig) -> list[str] | None:
        override = os.environ.get("CLI_SUBAGENTS_CODEX_COMMAND")
        if override:
            return list(json.loads(override))
        found = None
        if config.binary:
            found = find_program(config.binary) or (config.binary if Path(config.binary).is_file() else None)
        else:
            found = find_program("codex")
        if not found:
            return None
        path = Path(found)
        if path.suffix.lower() in (".cmd", ".ps1", ".bat"):
            # npm shim: call its launcher with node directly instead of going through cmd.exe.
            launcher = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            node = path.parent / "node.exe"
            node_path = str(node) if node.is_file() else find_program("node")
            if launcher.is_file() and node_path:
                return [node_path, str(launcher)]
        return [found]

    def _uses_own_home(self, account: Account) -> bool:
        return not (account.is_primary and same_path(account.home, _default_home()))

    def account_env(self, config: RuntimeConfig, account: Account) -> dict[str, str]:
        env = clean_environment()
        if self._uses_own_home(account):
            env["CODEX_HOME"] = str(account.home)
        return env

    def _auth_args(self, account: Account) -> list[str]:
        return [] if account.is_primary else ["-c", FILE_AUTH]

    def _run_quiet(self, config: RuntimeConfig, args: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
        command = self.find_binary(config)
        if not command:
            return 127, "codex binary not found"
        try:
            completed = subprocess.run(
                [*command, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=60, env=env, stdin=subprocess.DEVNULL, **no_window_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, f"{type(exc).__name__}: {exc}"
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")

    def version(self, config: RuntimeConfig) -> str | None:
        code, output = self._run_quiet(config, ["--version"], env=clean_environment())
        return output.strip().splitlines()[0] if code == 0 and output.strip() else None

    def auth_status(self, config: RuntimeConfig, account: Account) -> dict[str, Any]:
        """`codex login status` for one account. Makes no model request."""

        code, output = self._run_quiet(config, ["login", "status", *self._auth_args(account)], env=self.account_env(config, account))
        text = output.strip()
        logged_in = code == 0 and text.lower().startswith("logged in")
        method = None
        if "chatgpt" in text.lower():
            method = "chatgpt"
        elif "api key" in text.lower():
            method = "api_key"
        return {"loggedIn": logged_in, "authMethod": method, "detail": text.splitlines()[-1][:200] if text else None}

    def login_command(self, config: RuntimeConfig, account: Account) -> tuple[list[str], dict[str, str]]:
        command = self.find_binary(config)
        if not command:
            raise RuntimeError("codex binary not found; install Codex or set [codex] binary")
        return [*command, "login", *self._auth_args(account)], self.account_env(config, account)

    # Shared setup ---------------------------------------------------------

    def prepare(self, config: RuntimeConfig, account: Account, spec: JobSpec) -> dict[str, Any]:
        report = sync.new_report()
        if account.is_primary or same_path(account.home, config.primary):
            return report
        with file_lock(state_dir() / "sync" / f"codex-{account.name}.lock", timeout=30):
            sync.sync(config.primary, account.home, SYNC_PLAN, report)
            self._write_config(config.primary, account.home, report)
        return report

    def _write_config(self, primary: Path, home: Path, report: dict[str, Any]) -> None:
        source = primary / "config.toml"
        destination = home / "config.toml"
        if is_link(destination) or is_link(home):
            report["conflicts"].append("config.toml: is a link in the account folder, left in place")
            return
        # launch() turns this tool's own server off with -c overrides.
        primary_text = source.read_text(encoding="utf-8") if source.is_file() else ""
        current = destination.read_text(encoding="utf-8") if destination.is_file() else None
        content = primary_text
        if current is not None:
            content = merge_config(primary_text, current)
            try:
                tomllib.loads(content)
            except tomllib.TOMLDecodeError as exc:
                content = primary_text
                report["warnings"].append(
                    f"config.toml: could not keep the account's own trust and notice settings ({exc}); "
                    "used the primary config as is"
                )
        if current == content:
            return
        marker = home / ".cli-subagents-config"
        if current is not None and not marker.exists():
            # First run on a folder that already had its own config: keep a copy.
            backup = home / "config.toml.before-cli-subagents"
            if not os.path.lexists(backup):  # a dangling link counts as taken: never write through it
                copy_exclusive(destination, backup)
                report["warnings"].append(f"kept the account's previous config.toml as {backup.name}")
        write_text(destination, content)
        write_text(marker, "config.toml is generated from the primary setup on every run\n")
        report["copied"].append("config.toml")

    # Launch ---------------------------------------------------------------

    def _access_args(self, access: str, resume: bool) -> list[str]:
        if access == "full":
            return ["--dangerously-bypass-approvals-and-sandbox"]
        if access == "read":
            return ["-c", "sandbox_mode=read-only"] if resume else ["-s", "read-only"]
        if resume:
            return ["-c", "sandbox_mode=workspace-write", "-c", "approval_policy=on-request", "-c", "approvals_reviewer=auto_review"]
        return ["--approve-for-me"]

    def launch(
        self, config: RuntimeConfig, account: Account, spec: JobSpec, prompt: str, setup: dict[str, Any]
    ) -> Launch:
        command = self.find_binary(config)
        if not command:
            raise RuntimeError("codex binary not found; install Codex or set [codex] binary in config.toml")
        files = config_files(account.home, spec.cwd)
        no_nesting = self_server_overrides(files)
        self._attempt_account, self._attempt_home = account.name, account.home
        resume = bool(spec.resume_session)
        argv = [*command, "exec"]
        if resume:
            argv += ["resume", str(spec.resume_session)]
        argv += ["--json", "--skip-git-repo-check", *self._auth_args(account), *self._access_args(spec.access, resume)]
        if not resume:
            argv += ["-C", spec.cwd]
            for directory in spec.add_dirs:
                argv += ["--add-dir", directory]
        model = spec.model or config.default_model
        if model:
            argv += ["-m", model]
        if spec.effort:
            # Codex has no "max"; its highest level is "xhigh".
            effort = "xhigh" if spec.effort == "max" else spec.effort
            argv += ["-c", f"model_reasoning_effort={effort}"]
        if config.web_search is not None:
            argv += ["-c", f"web_search={json.dumps(config.web_search)}"]
        argv += reasoning_summary_args(config, files)
        argv += windows_sandbox_args(config, account.home, spec.access)
        if spec.access == "edit" and config.network_access:
            # Codex's workspace-write sandbox blocks network by default, on Windows (mxc) even
            # local sockets, so test runners such as VSTest hang. Claude edit jobs have network too.
            argv += ["-c", "sandbox_workspace_write.network_access=true"]
        argv += no_nesting
        argv += _parent_overrides(spec.job_id)
        env = self.account_env(config, account)
        warnings: list[str] = []
        shared = parent_mcp(config, spec, files, env)
        if shared:
            argv += shared.overrides
            env.update(shared.env)  # values reach the servers by variable name, never on the command line
            warnings = shared.warnings
        argv.append("-")

        env["CLI_SUBAGENTS_DEPTH"] = str(spec.depth)
        env["CLI_SUBAGENTS_JOB_ID"] = spec.job_id
        note = WORKER_NOTE.rstrip("]\n") + WORKER_TOOLS_NOTE + "]\n\n"
        return Launch(argv=argv, env=env, stdin=note + ("" if spec.resume_session else parent_note(spec.parent, "codex")) + prompt, session_id=spec.resume_session, warnings=warnings)

    def interactive(self, config: RuntimeConfig, account: Account, setup: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
        command = self.find_binary(config)
        if not command:
            raise RuntimeError("codex binary not found")
        return [*command, *self._auth_args(account)], self.account_env(config, account)

    def new_parser(self) -> CodexParser:
        # The runner starts one job per process: launch() just recorded this attempt's account.
        return CodexParser(self._attempt_account, self._attempt_home)
