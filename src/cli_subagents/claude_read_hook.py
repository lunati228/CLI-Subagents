"""PreToolUse shell guard for Claude read jobs; no model calls or state writes.

Run with the worker's Python interpreter and -I, so neither its working folder
nor PYTHONPATH can shadow the validator's imports. Safe commands produce no
decision: Claude's dontAsk mode and permission rules still decide access.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any, TextIO


def deny(reason: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "Read-only job: " + reason,
    }}


def validate(payload: dict[str, Any]) -> dict[str, Any] | None:
    from cli_subagents.agy_review import commands, paths

    tool_input = payload.get("tool_input")
    if payload.get("tool_name") == "Monitor":
        # Monitor runs its command in Bash's shell; only a WebSocket source runs nothing.
        if isinstance(tool_input, dict) and tool_input.get("command") is None and tool_input.get("ws") is not None:
            return None
        return deny("Monitor runs shell commands, which this guard can't check; use Bash or PowerShell")
    if payload.get("tool_name") not in ("Bash", "PowerShell"):
        return None
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or not command.strip():
        return deny("the shell command is missing")
    # Git Bash still treats backslashes as escapes on Windows. The shared
    # parser permits them there for PowerShell paths; do not let Bash turn a
    # supposed operand such as \\--output=x into an option after validation.
    if payload["tool_name"] == "Bash" and "\\" in command:
        return deny("Bash commands must use forward slashes and no backslash escapes")
    cwd = payload.get("cwd", os.getcwd())
    if not isinstance(cwd, str) or not cwd:
        return deny("the working folder is missing")
    homes = [str(Path.home()), os.environ.get("CLAUDE_CONFIG_DIR", "")]
    reason = ""

    def path_ok(value: str) -> bool:
        nonlocal reason
        _normal, _resolved, secret = paths.check(value, base=cwd, homes=homes)
        if secret:
            reason = secret
            return False
        return True

    if not commands.read_only(command, cwd=cwd, path_ok=path_ok):
        return deny(reason or "the command or its options are not plainly read-only")
    return None


def main(stream: TextIO | None = None) -> int:
    try:
        payload = json.load(stream or sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("expected a hook object")
        output = validate(payload)
    except Exception:
        output = deny("the shell command could not be validated")
    if output:
        print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.exit(main())
