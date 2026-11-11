"""Wire format between the client on the PC and the server: one JSON object per line.

See docs/remote-agent.md for every message type.
"""

from __future__ import annotations

import json
import re
from typing import IO, Iterator

VERSION = 1
MAX_LINE = 80 * 1024 * 1024  # a bundle line carries a base64 zip of up to 50 MB
MAX_HISTORY = 400 * 1024
PERMISSION_MODES = ("acceptEdits", "default", "plan")
WORKSPACE_MODES = ("pc", "none")
ERROR_CODES = (
    "bad_request",
    "no_account",
    "all_limited",
    "account_not_owned",
    "session_account_busy",
    "claude_failed",
    "internal",
)
_JOB_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SESSION_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{7,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LABEL = re.compile(r"^[^\x00-\x1f/\\]{1,80}$")


class ProtocolError(ValueError):
    """A message that doesn't follow the wire format."""


def encode(message: dict) -> bytes:
    return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def read_messages(stream: IO[bytes]) -> Iterator[dict]:
    """Yield one decoded object per line; blank lines are skipped."""
    while True:
        line = stream.readline(MAX_LINE + 1)
        if not line:
            return
        if len(line) > MAX_LINE:
            raise ProtocolError("message line too long")
        text = line.strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProtocolError(f"not JSON: {type(exc).__name__}") from None
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            raise ProtocolError("a message must be an object with a string type")
        yield message


def _optional_str(job: dict, name: str, limit: int = 200) -> str | None:
    value = job.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ProtocolError(f"{name} must be a short string")
    return value


def validate_job(job: dict) -> dict:
    """Return the job with defaults filled in, or raise ProtocolError."""
    if job.get("type") != "job":
        raise ProtocolError("the first message must be a job")
    if job.get("version") != VERSION:
        raise ProtocolError(f"unsupported version {job.get('version')!r}; this server speaks {VERSION}")
    job_id = job.get("job_id")
    if not isinstance(job_id, str) or not _JOB_ID.match(job_id):
        raise ProtocolError("job_id must be a lowercase uuid4")
    prompt = job.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ProtocolError("prompt must be a non-empty string")
    if len(prompt) > MAX_HISTORY:
        raise ProtocolError("prompt is too long")
    mode = job.get("permission_mode") or "acceptEdits"
    if mode not in PERMISSION_MODES:
        raise ProtocolError(f"permission_mode must be one of {', '.join(PERMISSION_MODES)}")
    max_turns = job.get("max_turns")
    if max_turns is not None and (not isinstance(max_turns, int) or isinstance(max_turns, bool) or not 1 <= max_turns <= 500):
        raise ProtocolError("max_turns must be a number from 1 to 500")

    workspace = job.get("workspace") or {"mode": "none"}
    if not isinstance(workspace, dict) or workspace.get("mode") not in WORKSPACE_MODES:
        raise ProtocolError("workspace.mode must be pc or none")
    if workspace["mode"] == "pc":
        port = workspace.get("bridge_port")
        token = workspace.get("bridge_token")
        label = workspace.get("label")
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ProtocolError("workspace.bridge_port must be a port number")
        if not isinstance(token, str) or not 16 <= len(token) <= 200 or not token.isascii() or not token.isprintable():
            raise ProtocolError("workspace.bridge_token must be 16 to 200 printable ASCII characters")
        if not isinstance(label, str) or not _LABEL.match(label):
            raise ProtocolError("workspace.label must be a short name without slashes")

    bundle = job.get("bundle")
    if bundle is not None:
        if not isinstance(bundle, dict) or not isinstance(bundle.get("sha256"), str) or not _SHA256.match(bundle["sha256"]):
            raise ProtocolError("bundle.sha256 must be a lowercase sha256")

    resume = job.get("resume")
    if resume is not None:
        if not isinstance(resume, dict):
            raise ProtocolError("resume must be an object")
        session_id = resume.get("session_id")
        if not isinstance(session_id, str) or not _SESSION_ID.match(session_id):
            raise ProtocolError("resume.session_id is not a session id")
        if not isinstance(resume.get("account"), str) or not resume["account"]:
            raise ProtocolError("resume.account must name the account the session ran on")
        history = resume.get("history_md")
        if history is not None and (not isinstance(history, str) or len(history.encode("utf-8")) > MAX_HISTORY):
            raise ProtocolError("resume.history_md must be text of at most 400 KB")

    return {
        **job,
        "permission_mode": mode,
        "workspace": workspace,
        "model": _optional_str(job, "model"),
        "effort": _optional_str(job, "effort", 20),
    }


def user_message(text: str) -> dict:
    """One user turn in Claude Code's stream-json input format."""
    return {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def project_dir_name(cwd: str) -> str:
    """Claude Code's folder name for a project: every non-alphanumeric character becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)
