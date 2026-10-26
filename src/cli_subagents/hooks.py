"""Claude Code hook commands (`csa hook ...`).

* `await` (PostToolUse on subagent_start and subagent_message, run with
  asyncRewake): waits for the job, then exits with code 2 so Claude Code wakes
  the main session, even when it is idle, and shows it the summary printed on
  stderr. It runs again after each subagent_message, so a job that woke the
  session with a question is watched again once it is answered.
* `pending` (SessionStart on resume and compact): names finished jobs whose
  result nobody collected yet.
* `inbox` (PostToolUse inside a Claude worker): hands the worker follow-ups the
  main session sent, as additional context next to the tool result.
* `relay-write` (PreToolUse on Write): allows only a known relay's new payload
  file in the csa home inbox; every other request receives no decision.

Notification hooks must never break the session that runs them: every failure
ends quietly with exit code 0. The read-only shell guard has a separate entry
point that denies a command when validation fails.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, TextIO

from cli_subagents import jobs, messages
from cli_subagents.config import jobs_dir
from cli_subagents.fsutil import try_lock, unlock

POLL_SECONDS = 2.0
# hooks/hooks.json gives the hook 7200 s; wake the session a little before that.
AWAIT_DEADLINE_SECONDS = 7080
RESULT_PREVIEW_CHARS = 1500
MAX_PENDING_JOBS = 10
WAKE = 2
# A new watcher waits this long for a previous one that is just exiting.
WATCH_LOCK_SECONDS = 5.0


def read_input(stream: TextIO | None = None) -> dict[str, Any]:
    try:
        data = json.loads((stream or sys.stdin).read() or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def find_job_id(value: Any, depth: int = 0) -> str | None:
    """The job to watch inside a tool response, in whatever shape the host passes it.

    MCP tool results reach the hook as text content holding our JSON, possibly
    wrapped in a content list or a {"content": [...]} object. A subagent_message
    reply for a finished job names its continuation in "new_job"; that job wins.
    """

    if depth > 6:
        return None
    if isinstance(value, dict):
        if isinstance(value.get("new_job"), dict):
            found = find_job_id(value["new_job"], depth + 1)
            if found:
                return found
        job_id = value.get("job_id")
        if isinstance(job_id, str) and job_id:
            return job_id
        for key in ("structuredContent", "content", "text", "result", "output"):
            if key in value:
                found = find_job_id(value[key], depth + 1)
                if found:
                    return found
        return None
    if isinstance(value, list):
        for item in value:
            found = find_job_id(item, depth + 1)
            if found:
                return found
        return None
    if isinstance(value, str) and "job_id" in value:
        try:
            return find_job_id(json.loads(value), depth + 1)
        except ValueError:
            match = re.search(r'"new_job"\s*:\s*\{[^{}]*?"job_id"\s*:\s*"([0-9a-z-]{1,64})"', value)
            match = match or re.search(r'"job_id"\s*:\s*"([0-9a-z-]{1,64})"', value)
            return match.group(1) if match else None
    return None


# await ---------------------------------------------------------------------


def _finished_summary(job_id: str, status: dict[str, Any]) -> str:
    result = jobs.read_result(job_id, RESULT_PREVIEW_CHARS)
    lines = [f"CLI Subagents job {job_id} finished: {status.get('outcome')}."]
    if status.get("message"):
        lines.append(f"Message: {status['message']}")
    text = (result.get("result") or "").strip()
    if text:
        lines += ["Result:", text]
        if result.get("result_truncated"):
            lines.append(f"[first {RESULT_PREVIEW_CHARS} characters; call subagent_result with job_id {job_id} for the rest]")
    return "\n".join(lines)


def _messages_summary(job_id: str, new: list[dict[str, Any]], questions: list[dict[str, Any]]) -> str:
    lines = [f"CLI Subagents job {job_id} is still running and has something for you."]
    open_ids = {item["id"] for item in questions}
    for item in new:
        if item.get("id") not in open_ids:
            lines.append(f"Message [{item.get('id')}]: {item.get('text')}")
    for item in questions:
        lines.append(f"Question [{item['id']}]: {item['text']}")
    if questions:
        lines.append(f'Answer with subagent_message (job_id "{job_id}", reply_to set to the question id).')
    lines.append("Send follow-ups with subagent_message; call subagent_wait to collect the result when it finishes.")
    return "\n".join(lines)


def await_job(payload: dict[str, Any], deadline_seconds: float = AWAIT_DEADLINE_SECONDS) -> tuple[int, str]:
    """Wait for the job named in a subagent_start or subagent_message response.

    Returns (exit code, stderr text). One watcher per job: if another one is
    already waiting for it, this one leaves quietly.
    """

    job_id = find_job_id(payload.get("tool_response"))
    if not job_id:
        return 0, ""
    try:
        jobs.read_status(job_id)
    except ValueError:
        return 0, ""
    handle = try_lock(jobs.job_dir(job_id) / "watch.lock", WATCH_LOCK_SECONDS)
    if handle is None:
        return 0, ""  # another watcher already waits for this job
    try:
        return _watch(job_id, deadline_seconds)
    finally:
        unlock(handle)


def _watch(job_id: str, deadline_seconds: float) -> tuple[int, str]:
    deadline = time.monotonic() + deadline_seconds
    while True:
        try:
            status = jobs.read_status(job_id)
        except ValueError:
            return 0, ""
        if status.get("state") == "done":
            if not jobs.claim_delivery(job_id):
                return 0, ""  # the model already collected it (subagent_wait, subagent_result)
            return WAKE, _finished_summary(job_id, status)
        if messages.has_new_for_host(job_id):
            # Only entries nobody was shown yet wake the session (taken atomically),
            # so a question already seen through subagent_wait or another watcher
            # doesn't wake it a second time.
            new = messages.take_for_host(job_id)
            if new:
                return WAKE, _messages_summary(job_id, new, messages.open_questions(job_id))
        if time.monotonic() >= deadline:
            return WAKE, (
                f"CLI Subagents job {job_id} is still running after {int(deadline_seconds // 60)} minutes. "
                "Call subagent_wait to keep waiting for it."
            )
        time.sleep(POLL_SECONDS)


# pending -------------------------------------------------------------------


def _same_dir(left: str, right: str) -> bool:
    def norm(path: str) -> str:
        return os.path.normcase(os.path.normpath(path.replace("/", os.sep)))

    return bool(left) and bool(right) and norm(left) == norm(right)


def pending_jobs(cwd: str, limit: int = MAX_PENDING_JOBS) -> list[dict[str, Any]]:
    """Finished jobs for `cwd` whose result was never delivered, newest first."""

    folder = jobs_dir()
    if not folder.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for path in folder.iterdir():
        if not path.is_dir() or not (path / jobs.NOTIFY_PENDING).exists():
            continue
        try:
            status = jobs.read_status(path.name)
        except ValueError:
            continue
        if status.get("state") == "done" and _same_dir(str(status.get("cwd") or ""), cwd):
            found.append(status)

    def finished(status: dict[str, Any]) -> float:
        return float(status.get("finished_at") or status.get("updated_at") or status.get("created_at") or 0)

    return sorted(found, key=finished, reverse=True)[:limit]


def pending_context(payload: dict[str, Any]) -> dict[str, Any] | None:
    found = pending_jobs(str(payload.get("cwd") or os.getcwd()))
    if not found:
        return None
    lines = ["CLI Subagents jobs that finished without their result being collected (newest first):"]
    for status in found:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(status.get("finished_at") or status.get("updated_at") or 0))
        line = f"- {status['job_id']}: {status.get('outcome')}, finished {when}"
        if status.get("message"):
            line += f" ({str(status['message'])[:200]})"
        lines.append(line)
    lines.append("Call subagent_result with a job_id to read one.")
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(lines)}}


# inbox (worker side) ---------------------------------------------------------


def inbox_context(job_id: str) -> dict[str, Any] | None:
    # Shown once, but not marked read: the runner re-delivers anything the worker
    # didn't read explicitly, because hook context is only a hint to the model.
    new = messages.take_for_hook(job_id)
    if not new:
        return None
    parts = []
    for item in new:
        if item.get("reply_to"):
            parts.append(f"Answer from the main session to your question {item['reply_to']}: {item.get('text')}")
        else:
            parts.append(f"New instruction from the main session, apply it to your current task: {item.get('text')}")
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "\n\n".join(parts)}}


# Entry point -------------------------------------------------------------------


def relay_write_permission(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Allow only a native relay's new, size-capped Write in the home inbox.

    No decision for other tools, agents, paths or malformed payloads: the host's
    normal permission rules still apply. agent_type is documented common input.
    """

    from cli_subagents.relay_input import MAX_BYTES, validate_path

    names = {f"cli-subagents-{runtime}" for runtime in ("claude", "codex", "antigravity", "opencode")}
    names |= {f"cli-subagents:{name}" for name in tuple(names)}
    agent_type = payload.get("agent_type")
    if (payload.get("hook_event_name") != "PreToolUse" or payload.get("tool_name") != "Write"
            or not isinstance(agent_type, str) or agent_type not in names):
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    path, content = tool_input.get("file_path"), tool_input.get("content")
    if not isinstance(path, str) or not isinstance(content, str):
        return None
    try:
        if len(content.encode("utf-8")) > MAX_BYTES:
            return None
        validate_path(path, new=True, home_only=True)
    except (ValueError, OSError):
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow",
        "permissionDecisionReason": "New CLI Subagents relay payload in the csa inbox",
    }}


def main(action: str, job_id: str | None = None) -> int:
    try:
        if action == "await":
            code, text = await_job(read_input())
            if text:
                print(text, file=sys.stderr)
            return code
        if action == "pending":
            output = pending_context(read_input())
        elif action == "relay-write":
            output = relay_write_permission(read_input())
        elif action == "inbox":
            try:
                sys.stdin.read()  # the hook payload isn't needed; drain it so the host's write never blocks
            except (OSError, ValueError):
                pass
            output = inbox_context(job_id) if job_id else None
        else:
            return 0
        if output:
            print(json.dumps(output, ensure_ascii=False))
        return 0
    except Exception:  # a hook must never break the session that runs it
        return 0


def shell_command(parts: list[str]) -> dict[str, Any]:
    """A command hook entry that runs `parts` through the hook shell.

    Claude Code runs shell-form hooks with Git Bash, or with PowerShell on
    Windows without Git Bash. Paths use forward slashes and arguments are
    double-quoted, which both shells read the same way. A quoted program name
    needs `&` in PowerShell, which Bash rejects, so the program stays unquoted
    when it is a plain path, and otherwise the hook asks for PowerShell on
    Windows explicitly.
    """

    parts = [str(part).replace("\\", "/") for part in parts]
    plain = re.compile(r"[A-Za-z0-9_./:+-]+")
    special = any(char in part for part in parts for char in '"$`')
    if os.name != "nt":
        import shlex

        return {"type": "command", "command": " ".join(shlex.quote(part) for part in parts)}
    if special:
        quoted = " ".join("'" + part.replace("'", "''") + "'" for part in parts)
        return {"type": "command", "shell": "powershell", "command": "& " + quoted}
    args = " ".join(f'"{part}"' for part in parts[1:])
    if plain.fullmatch(parts[0]):
        return {"type": "command", "command": f"{parts[0]} {args}".strip()}
    return {"type": "command", "shell": "powershell", "command": f'& "{parts[0]}" {args}'.strip()}


def worker_inbox_hook(job_id: str, home: Path) -> dict[str, Any]:
    """The PostToolUse hook group a Claude worker gets for its inbox."""

    shim = Path(__file__).resolve().with_name("hook_shim.py")
    entry = shell_command([sys.executable, str(shim), "--home", str(home), "hook", "inbox", job_id])
    entry["timeout"] = 15
    return {"hooks": [entry]}  # no matcher: every tool


def worker_read_hook() -> dict[str, Any]:
    """The synchronous shell guard installed only in Claude read workers."""

    script = Path(__file__).resolve().with_name("claude_read_hook.py")
    entry = shell_command([sys.executable, "-I", str(script)])
    entry.update(timeout=15, onFailure="block")
    return {"matcher": "Bash|PowerShell", "hooks": [entry]}
