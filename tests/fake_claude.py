"""Stand-in for `claude -p --output-format stream-json` used by the tests.

Behaviour is chosen by environment variables:
  FAKE_CLAUDE_LIMITED   comma-separated account folder names that are "out of quota"
  FAKE_CLAUDE_SCENARIO  success (default) | auth | slow | crash | nostdin | max_turns |
                       success_exit_1 | success_pause | success_quota | success_auth |
                       budget | usage_no_result
  FAKE_CLAUDE_RECORD    file to append {"argv", "env", "stdin"} records to
  FAKE_CLAUDE_COST      result's total_cost_usd (default 0.01; budget uses its cap)
"""

import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def main():
    argv = sys.argv[1:]
    if os.environ.get("FAKE_CLAUDE_SCENARIO") == "nostdin":
        time.sleep(120)  # never reads its prompt, like a hung CLI
        return 0
    prompt = sys.stdin.read()
    record = os.environ.get("FAKE_CLAUDE_RECORD")
    if record:
        with open(record, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": argv, "env": dict(os.environ), "stdin": prompt}) + "\n")

    # Tests that don't set CLAUDE_CONFIG_DIR must not leave a "default" folder in the working directory
    # (the repository root when the suite runs), so fall back to a folder under the temp directory.
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path(tempfile.gettempdir()) / "csa-fake-claude" / "default")
    account = config_dir.name
    projects = config_dir / "projects"
    if "--resume" in argv:
        session = argv[argv.index("--resume") + 1]
        if not list(projects.glob(f"*/{session}.jsonl")):
            emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
                  "result": f"No conversation found with session ID: {session}"})
            return 1
    else:
        session = argv[argv.index("--session-id") + 1] if "--session-id" in argv else "new-session"
    # Like the real CLI: one transcript per session, under the project's folder name.
    folder = projects / re.sub(r"[^A-Za-z0-9]", "-", os.path.normpath(os.getcwd()))
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{session}.jsonl", "a", encoding="utf-8") as transcript:
        transcript.write(json.dumps({"account": account, "prompt": prompt}) + "\n")
    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "success")
    limited = [item for item in os.environ.get("FAKE_CLAUDE_LIMITED", "").split(",") if item]

    if scenario == "crash":
        sys.stderr.write("boom\n")
        return 3

    emit({
        "type": "system", "subtype": "init", "session_id": session, "model": "claude-haiku-5-5",
        "permissionMode": argv[argv.index("--permission-mode") + 1] if "--permission-mode" in argv else "default",
        "claude_code_version": "9.9.9", "apiKeySource": "none",
        "mcp_servers": [{"name": "shared-server", "status": "connected", "source": "user"}],
        "plugins": [{"name": "demo-plugin", "path": "x"}], "skills": ["alpha"], "agents": ["general-purpose"],
    })
    if scenario == "slow":
        time.sleep(120)
    time.sleep(float(os.environ.get("FAKE_CLAUDE_DELAY", "0") or 0))
    if account in limited:
        emit({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": int(time.time()) + 3600, "rateLimitType": "five_hour",
            "utilization": 1.0, "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": int(time.time()) + 3600}},
        }, "session_id": session})
        emit({"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
              "result": "You've hit your session limit · resets 3:45pm", "session_id": session})
        return 1
    if scenario == "badevent":
        emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": "2026-10-14T10:00:00Z",
              "unifiedWindows": {"five_hour": {"utilization": "n/a"}}}, "session_id": session})
        emit(["not", "an", "object"])
    if scenario == "auth":
        emit({"type": "result", "subtype": "success", "is_error": True,
              "result": "Invalid API key · Please run /login", "session_id": session})
        return 1
    emit({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {}},
        {"type": "text", "text": f"done on {account}"},
    ]}, "session_id": session})
    emit({"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 0.25, "resetsAt": 1}, "seven_day": {"utilization": 0.5, "resetsAt": 2}},
    }, "session_id": session})
    if scenario == "max_turns":
        emit({"type": "result", "subtype": "error_max_turns", "is_error": True,
              "session_id": session, "num_turns": 1})
        return 1
    if scenario == "usage_no_result":
        # stream-json can repeat usage for each content block of one API message.
        for text in ("first block", "second block"):
            emit({"type": "assistant", "message": {
                "id": "message-1", "model": "claude-haiku-5-5",
                "usage": {"input_tokens": 1000, "output_tokens": 100},
                "content": [{"type": "text", "text": text}],
            }, "session_id": session})
        sys.stderr.write("worker stopped before emitting its final cost\n")
        return 3
    if scenario == "budget":
        budget = argv[argv.index("--max-budget-usd") + 1] if "--max-budget-usd" in argv else "0.01"
        emit({"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
              "errors": ["Maximum dollar budget exceeded"], "session_id": session,
              "total_cost_usd": float(os.environ.get("FAKE_CLAUDE_COST", budget)), "num_turns": 1})
        return 1
    emit({"type": "result", "subtype": "success", "is_error": False, "result": f"done on {account}",
          "session_id": session, "total_cost_usd": float(os.environ.get("FAKE_CLAUDE_COST", "0.01")), "num_turns": 1})
    if scenario == "success_pause":
        time.sleep(120)
    if scenario in ("success_exit_1", "success_quota", "success_auth"):
        sys.stderr.write({"success_exit_1": "late CLI failure\n", "success_quota": "usage limit reached\n",
                          "success_auth": "not logged in\n"}[scenario])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
