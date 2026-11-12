"""Live check of the remote runtime with the real Claude Code. Not part of the unit tests: it makes model calls.

    python tests/remote_live_check.py --account-home <Claude config folder> [--model M] [--effort E]

The whole csa path runs on this machine: csa's runner starts the relay, the relay starts the PC bridge with
csa-parent behind it and runs tests/fake_ssh.py, which runs the real server module, which runs the real
`claude` in the account folder you name. Only ssh itself is a stand-in. The worker gets a project with a failing
test and is asked to report progress with message_parent, run the tests, fix the bug, run them again and try to
read .env. While it works, the check sends one follow-up into the running conversation and answers any question
it asks.

The account folder is used as one job's Claude Code config folder. The check keeps the server's transcript
cleanup off, sends an empty setup bundle, uses a throwaway csa home, and afterwards removes what the job added to
the folder: the bundle marker and the job's own session folder under projects/.

It prints one JSON report and exits 0 when every check passed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

CALC = "def add(a, b):\n    return a - b\n"
TEST = (
    "import unittest\nfrom calc import add\n\n\nclass AddTest(unittest.TestCase):\n"
    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n\n"
    "if __name__ == '__main__':\n    unittest.main()\n"
)
ENV_TEXT = "SECRET=not-for-the-model\n"
PROMPT = (
    "First send the main session a short progress note with message_parent saying you are starting. "
    "Then run the tests in this project with `python -m unittest -v`, fix the bug they show, run them again, "
    "and then try to read the file .env. Reply in two lines: the final test result, and whether reading .env worked."
)
FOLLOW_UP = "Also end your final reply with the word PINEAPPLE on its own line."
MARKER = ".remote-agent-managed.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--account-home", required=True, help="Claude Code config folder the server job runs in")
    parser.add_argument("--model", default="claude-haiku-5-5")
    parser.add_argument("--effort", default=None)
    parser.add_argument("--claude", default=None, help="claude binary (default: found on PATH)")
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--budget", default="1.50", help="Claude Code's --max-budget-usd for the job")
    args = parser.parse_args()

    account_home = Path(args.account_home).expanduser().resolve()
    if not account_home.is_dir():
        print(f"no folder {account_home}", file=sys.stderr)
        return 2
    claude = args.claude or shutil.which("claude.cmd") or shutil.which("claude")
    if not claude:
        print("claude not found", file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix="csa-remote-live-")).resolve()
    csa_home, state, empty_setup, project = work / "csa", work / "server-state", work / "empty-claude", work / "my-app"
    for folder in (csa_home, state, empty_setup, project):
        folder.mkdir()
    (project / "calc.py").write_text(CALC, encoding="utf-8")
    (project / "test_calc.py").write_text(TEST, encoding="utf-8")
    (project / ".env").write_text(ENV_TEXT, encoding="utf-8")

    server_config = work / "server.toml"
    server_config.write_text("\n".join([
        "[server]",
        f"state_dir = {json.dumps(str(state))}",
        # The server has no budget option; its claude command carries the cap instead.
        f"claude = {json.dumps([claude, '--max-budget-usd', args.budget])}",
        "transcript_days = 90",
        "max_job_minutes = 20",
        "[[account]]",
        'name = "live"',
        'owner = "me"',
        f"home = {json.dumps(str(account_home))}",
    ]) + "\n", encoding="utf-8")
    # The server cleans old transcripts in the account folder at most once an hour, at job start.
    # A fresh marker keeps that off for this real folder.
    (state / "users" / "me").mkdir(parents=True)
    (state / "users" / "me" / "last-cleanup").write_text(str(int(time.time())), encoding="utf-8")
    (csa_home / "config.toml").write_text("\n".join([
        "[claude]", f"primary = {json.dumps(str(empty_setup))}",
        "[remote]", 'server = "me@live-check"', f"primary = {json.dumps(str(empty_setup))}",
        "[[remote.accounts]]", 'name = "live"',
    ]) + "\n", encoding="utf-8")

    marker_before = (account_home / MARKER).exists()
    projects_before = {p.name for p in (account_home / "projects").glob("*")} if (account_home / "projects").is_dir() else set()
    os.environ.update({
        "CLI_SUBAGENTS_HOME": str(csa_home),
        "CLI_SUBAGENTS_SSH_COMMAND": json.dumps([sys.executable, str(ROOT / "tests" / "fake_ssh.py"),
                                                 "--server-config", str(server_config), "--user", "me"]),
        "PYTHONPATH": str(ROOT / "src"),
    })
    for key in ("CLI_SUBAGENTS_DEPTH", "CLI_SUBAGENTS_JOB_ID", "CLAUDE_CONFIG_DIR"):
        os.environ.pop(key, None)

    from cli_subagents import jobs, messages

    started = time.time()
    job = jobs.create_job(runtime="remote", prompt=PROMPT, cwd=str(project), account="live", model=args.model,
                          effort=args.effort, access="edit", max_turns=args.max_turns,
                          timeout_seconds=args.timeout)["job_id"]
    folder = jobs.job_dir(job)
    follow_up_id, answered = None, []
    follow_up_at, result_times, accepted_at = None, [], None
    while time.time() - started < args.timeout + 180:
        status = jobs.read_status(job)
        if status.get("state") == "done":
            break
        events = (folder / "events.jsonl").read_text(encoding="utf-8", errors="replace") if (folder / "events.jsonl").exists() else ""
        if accepted_at is None and '"event": "accepted"' in events:
            accepted_at = time.time()
        results_now = events.count('"type": "result"')
        while len(result_times) < results_now:
            result_times.append(round(time.time() - started, 1))
        if follow_up_id is None and '"event": "accepted"' in events and '"type": "assistant"' in events:
            record = messages.queue_for_worker(job, FOLLOW_UP)
            follow_up_id = record["id"] if record else "not accepted"
            follow_up_at = round(time.time() - started, 1)
        for question in messages.open_questions(job):
            messages.queue_for_worker(job, "Go ahead and use your judgment.", reply_to=question["id"])
            answered.append(question["text"])
        time.sleep(1)
    status = jobs.read_status(job)

    events = []
    for line in (folder / "events.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    tools_seen = init.get("tools") or []
    calls = [block.get("name") for e in events if e.get("type") == "assistant"
             for block in (e.get("message") or {}).get("content") or [] if isinstance(block, dict) and block.get("type") == "tool_use"]
    relay = [e for e in events if e.get("type") == "csa.remote"]
    audit_path = folder / "remote" / "pc-tools.jsonl"
    audit = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()] if audit_path.exists() else []
    result = jobs.read_result(job).get("result") or ""
    final = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=project, capture_output=True, text=True)
    usage_log = state / "users" / "me" / "usage.jsonl"
    allowed_builtin = ("WebSearch", "WebFetch", "TodoWrite", "ToolSearch")
    report = {
        "date": time.strftime("%Y-%m-%d %H:%M %Z"),
        "claude_code": subprocess.run([claude, "--version"], capture_output=True, text=True).stdout.strip(),
        "model_asked": args.model,
        "model_reported": init.get("model"),
        "outcome": status.get("outcome"),
        "message": status.get("message"),
        "account": status.get("account"),
        "seconds": round(time.time() - started),
        "budget_usd": args.budget,
        "cost_usd": status.get("cost_usd"),
        "timing_s": {"accepted": round(accepted_at - started, 1) if accepted_at else None,
                     "follow_up_queued": follow_up_at, "results_seen": result_times,
                     "job_done": round(float(status.get("finished_at") or time.time()) - started, 1)},
        "attempts": len(status.get("attempts") or []),
        "mcp_servers": init.get("mcp_servers"),
        "builtin_tools_beyond_allowlist": sorted(t for t in tools_seen if not t.startswith("mcp__") and t not in allowed_builtin),
        "pc_tools_seen": sorted(t for t in tools_seen if t.startswith("mcp__pc__")),
        "parent_tools_seen": sorted(t for t in tools_seen if t.startswith("mcp__csa-parent__")),
        "tool_calls": calls,
        "pc_audit": [{k: entry.get(k) for k in ("tool", "server", "allowed", "reason")} for entry in audit],
        "messages_to_parent": [item.get("text") for item in messages.outbox(job)],
        "questions_answered": answered,
        "follow_up": {"id": follow_up_id,
                      "delivered": any(e.get("event") == "delivered" and e.get("id") == follow_up_id for e in relay),
                      "in_result": "PINEAPPLE" in result},
        "relay_events": [e.get("event") for e in relay if e.get("event") != "prompt"],
        "results_in_stream": sum(1 for e in events if e.get("type") == "result"),
        "bug_fixed": "a + b" in (project / "calc.py").read_text(encoding="utf-8"),
        "tests_exit_code_after": final.returncode,
        "env_file_unchanged": (project / ".env").read_text(encoding="utf-8") == ENV_TEXT,
        "result": result,
        "server_usage_log": usage_log.read_text(encoding="utf-8").splitlines() if usage_log.exists() else [],
    }

    # Remove what this job added to the real account folder.
    removed = []
    if not marker_before and (account_home / MARKER).exists():
        (account_home / MARKER).unlink()
        removed.append(MARKER)
    work_slug = "server-state"
    for path in (account_home / "projects").glob("*") if (account_home / "projects").is_dir() else []:
        if path.name not in projects_before and work_slug in path.name and work.name.replace("_", "-") in path.name.replace("_", "-"):
            shutil.rmtree(path, ignore_errors=True)
            removed.append(f"projects/{path.name}")
    report["removed_from_account_folder"] = removed
    checks = {
        "job succeeded": report["outcome"] == "success",
        "bug fixed and tests pass": report["bug_fixed"] and report["tests_exit_code_after"] == 0,
        ".env unchanged": report["env_file_unchanged"],
        "no built-in file or shell tools": not report["builtin_tools_beyond_allowlist"],
        "pc tools offered": bool(report["pc_tools_seen"]),
        "parent tools offered": bool(report["parent_tools_seen"]),
        "message_parent arrived": bool(report["messages_to_parent"]),
        "follow-up reached the worker": report["follow_up"]["in_result"],
    }
    report["checks"] = checks
    print(json.dumps(report, indent=2, ensure_ascii=False))
    shutil.rmtree(work, ignore_errors=True)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
