"""Fake OpenCode v2 JSON CLI; never imports or launches any installed agent.

FAKE_OPENCODE_SCENARIO: success, free_limit, rate_limit, quota, auth, capacity,
v1_error, plugin_missing, bad_variant, exit_1,
success_exit_1, pause, denied_command. FAKE_OPENCODE_VERSION
chooses --version output. Records only run calls, using FAKE_OPENCODE_RECORD
or the shared FAKE_CLAUDE_RECORD.
"""

import json
import os
from pathlib import Path
import sys
import time


def emit(kind, session, **payload):
    print(json.dumps({"type": kind, "timestamp": 123, "sessionID": session, **payload}), flush=True)


def main():
    argv = sys.argv[1:]
    if argv == ["--version"]:
        print(os.environ.get("FAKE_OPENCODE_VERSION", "2.1.4"))
        return 0
    if argv[:2] == ["auth", "login"]:
        return 0
    prompt = sys.stdin.read()
    record = os.environ.get("FAKE_OPENCODE_RECORD") or os.environ.get("FAKE_CLAUDE_RECORD")
    if record:
        with open(record, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": argv, "env": dict(os.environ), "stdin": prompt}) + "\n")
    if not argv or argv[0] != "run" or "--standalone" not in argv:
        sys.stderr.write("fake requires run --standalone\n")
        return 2
    scenario = os.environ.get("FAKE_OPENCODE_SCENARIO", "success")
    session = argv[argv.index("-s") + 1] if "-s" in argv else "ses_fake_opencode"
    if scenario == "exit_1":
        sys.stderr.write("early CLI failure\n")
        return 1
    emit("step_start", session, part={"id": "start", "type": "step-start"})
    errors = {
        "free_limit": {"type": "provider.free-limit", "message": "Free usage limit reached."},
        "rate_limit": {"type": "provider.rate-limit", "message": "Too many requests", "retryAfterMs": 60000},
        "quota": {"type": "provider.quota", "message": "Provider quota reached"},
        "auth": {"type": "provider.auth", "message": "Invalid API key"},
        "capacity": {"type": "provider.internal", "message": "The model is overloaded", "status": 503},
        "v1_error": {"name": "APIError", "data": {"message": "Rate limit exceeded", "statusCode": 429}},
        # What v2.1.4 reports when the session keeps csa's gate model, or a variant is missing.
        "plugin_missing": {"type": "provider.no-route", "message": "Model unavailable: cli-subagents/gate-not-ready"},
        "bad_variant": {"type": "provider.no-route",
                        "message": "Variant unavailable for opencode/deepseek-v4.1-flash-free: medium"},
    }
    if scenario in errors:
        emit("error", session, error=errors[scenario])
        return 1
    if scenario == "plant_protected_pause":
        # What a command launcher killed before its own check leaves behind: its
        # recovery record (a pre-command snapshot), then the item it planted.
        import subprocess
        config = json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])
        source = (Path(config["plugins"][-1]) / "server.js").read_text(encoding="utf-8")
        settings = json.loads(source.split("const settings = ", 1)[1].split(";\n", 1)[0])
        sys.path.insert(0, str(Path(settings["sandboxScript"]).resolve().parents[3]))
        from cli_subagents.sandbox.launcher import snapshot
        from cli_subagents.sandbox.model import Policy
        context = Path(settings["context"])
        value = Policy.from_dict(json.loads(context.read_text(encoding="utf-8"))["command_sandbox"]["policy"])
        gone = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
        (context.parent / "sandbox").mkdir(exist_ok=True)
        (context.parent / "sandbox" / "recovery-fixture.json").write_text(json.dumps(
            {"version": 1, "policy": value.to_dict(), "baseline": snapshot(value), "pid": int(gone.stdout),
             "started": 1}), encoding="utf-8")
        planted = Path.cwd() / ".claude"
        planted.mkdir()
        (planted / "settings.json").write_text("fixture", encoding="utf-8")
    if scenario in ("sandbox_command", "launcher_plant_pause"):
        import subprocess
        config = json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])
        source = (Path(config["plugins"][-1]) / "server.js").read_text(encoding="utf-8")
        settings = json.loads(source.split("const settings = ", 1)[1].split(";\n", 1)[0])
        invocation = {"command": "python -m unittest", "cwd": os.getcwd(), "env": dict(os.environ),
                      "timeout": 10000, "shell": "/bin/sh"}
        checked = subprocess.run([settings["python"], settings["sandboxScript"], settings["context"]],
                                 input=json.dumps(invocation), capture_output=True, text=True)
        if checked.returncode:
            sys.stderr.write(checked.stderr)
            return 1
        prepared = json.loads(checked.stdout)
        args = ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"] if os.name == "nt" else ["-c"]
        if scenario == "launcher_plant_pause":
            # The project's test plants a protected name and sleeps; the job's
            # cancel then stops the launcher with the rest of the worker tree.
            subprocess.Popen([prepared["shell"], *args, prepared["command"]])
        else:
            done = subprocess.run([prepared["shell"], *args, prepared["command"]])
            if done.returncode:
                return done.returncode
    if scenario == "denied_command":
        # Exercise the exact Python entrypoint that the generated JS hook calls.
        import subprocess

        config = json.loads(os.environ["OPENCODE_CONFIG_CONTENT"])
        source = (Path(config["plugins"][-1]) / "server.js").read_text(encoding="utf-8")
        settings = json.loads(source.split("const settings = ", 1)[1].split(";\n", 1)[0])
        payload = {"tool": "shell", "input": {"command": "rm -rf /", "cwd": os.getcwd()}}
        result = subprocess.run([settings["python"], settings["script"], settings["context"]],
                                input=json.dumps(payload), capture_output=True, text=True)
        answer = json.loads(result.stdout)
        if answer.get("decision") != "deny":
            sys.stderr.write("review gate failed to block denied command\n")
            return 1
        emit("tool_use", session, part={"id": "tool", "tool": "shell", "state": {"status": "error", "input": payload["input"], "error": answer["reason"]}})
        text = "denied command was blocked"
    else:
        emit("reasoning", session, part={"id": "reasoning", "text": "checking the task"})
        emit("tool_use", session, part={"id": "tool", "tool": "read", "state": {"status": "completed", "input": {"path": "AGENTS.md"}}})
        text = "opencode done" + (" after resume" if "-s" in argv else "")
    emit("text", session, part={"id": "answer", "text": text})
    emit("step_finish", session, part={"id": "finish", "reason": "stop", "cost": 0,
                                     "tokens": {"input": 10, "output": 2, "reasoning": 1, "cache": {"read": 3, "write": 0}}})
    if scenario in ("pause", "plant_protected_pause", "launcher_plant_pause"):
        time.sleep(120)
    if scenario == "success_exit_1":
        sys.stderr.write("late CLI failure\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
