"""Stand-in for `claude` on the server in the remote runtime tests.

It speaks the parts of Claude Code's interface the server uses:
- `auth status --json` says logged in unless fake-logged-out exists in the account folder;
- `-p /usage` prints session and weekly percentages (fake-usage.txt in the
  account folder, default 10);
- `-p --input-format stream-json --output-format stream-json` reads user
  messages from stdin and answers each with an init event (once), assistant
  events and a result event;
- `--session-id` / `--resume` keep a session file where Claude Code keeps it,
  and a resume of a missing session fails like the real one.

Prompt lines steer it:
  MCP: <server> <tool> <json arguments>   call a tool on an MCP server from --mcp-config
  PC_RUN: <command>                       the same as MCP: pc pc_run {"command": ...}
  SLOW [seconds]                          wait before answering (default 2)
  LIMIT                                   end with a usage-limit error
  LOGIN                                   end like an account that isn't logged in
Every call is recorded in fake-calls.jsonl and every prompt in fake-prompts.jsonl
in the account folder.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def project_dir_name(cwd):
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def flag(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def mcp_call(server, tool, arguments):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    headers.update(server.get("headers") or {})

    def post(payload):
        request = urllib.request.Request(server["url"], data=json.dumps(payload).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read()
            return json.loads(body) if body else None

    try:
        post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fake", "version": "1"}}})
        post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        reply = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": arguments}})
    except urllib.error.HTTPError as exc:
        return f"ERROR: HTTP {exc.code}"
    except OSError as exc:
        return f"ERROR: {type(exc).__name__}"
    if reply.get("error"):
        return "ERROR: " + str(reply["error"].get("message"))
    result = reply.get("result") or {}
    text = "\n".join(item.get("text", "") for item in result.get("content") or [])
    return ("ERROR: " if result.get("isError") else "") + text


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def main(argv):
    home = Path(os.environ["CLAUDE_CONFIG_DIR"])
    home.mkdir(parents=True, exist_ok=True)
    with open(home / "fake-calls.jsonl", "a", encoding="utf-8") as log:
        log.write(json.dumps({"argv": argv, "cwd": os.getcwd(),
                              "oauth_env": "CLAUDE_CODE_OAUTH_TOKEN" in os.environ}) + "\n")

    if argv[:3] == ["auth", "status", "--json"]:
        logged_in = not (home / "fake-logged-out").exists()
        print(json.dumps({"loggedIn": logged_in, "authMethod": "claude.ai" if logged_in else "none"}))
        return 0
    if argv[:2] == ["-p", "/usage"]:
        usage = home / "fake-usage.txt"
        percent = usage.read_text().strip() if usage.exists() else "10"
        print(f"Current session: {percent}% used\nCurrent week (all models): 20% used")
        return 0

    resume = flag(argv, "--resume")
    session_id = resume or flag(argv, "--session-id") or str(uuid.uuid4())
    sessions = home / "projects" / project_dir_name(os.getcwd())
    session_path = sessions / f"{session_id}.jsonl"
    if resume and not session_path.exists():
        print(f"No conversation found with session ID: {resume}", file=sys.stderr)
        return 1
    sessions.mkdir(parents=True, exist_ok=True)
    servers = {}
    if flag(argv, "--mcp-config"):
        servers = json.loads(Path(flag(argv, "--mcp-config")).read_text()).get("mcpServers") or {}

    started = False
    turn = 0
    for line in sys.stdin:
        if not line.strip():
            continue
        message = json.loads(line)
        text = "".join(block.get("text", "") for block in message["message"]["content"])
        with open(home / "fake-prompts.jsonl", "a", encoding="utf-8") as log:
            log.write(json.dumps({"session_id": session_id, "text": text}) + "\n")
        if not started:
            emit({"type": "system", "subtype": "init", "session_id": session_id, "model": "fake-model",
                  "mcp_servers": [{"name": name, "status": "connected"} for name in servers]})
            started = True
        turn += 1
        is_error = False
        outputs = []
        answer = None
        for step in text.splitlines():
            step = step.strip()
            if step.startswith("SLOW"):
                words = step.split()
                time.sleep(float(words[1]) if len(words) > 1 and words[1].replace(".", "", 1).isdigit() else 2)
            elif step.startswith("PC_RUN:"):
                step = "MCP: pc pc_run " + json.dumps({"command": step.split("PC_RUN:", 1)[1].strip()})
            elif step == "LIMIT":
                answer, is_error = "Claude usage limit reached. Your limit will reset at 5pm.", True
            elif step == "LOGIN":
                answer, is_error = "Invalid API key · Please run /login", True
            if step.startswith("MCP:"):
                _, name, tool, *rest = step.split(" ", 3)
                arguments = json.loads(rest[0]) if rest else {}
                tool_id = f"t{turn}-{len(outputs)}"
                emit({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": tool_id, "name": f"mcp__{name}__{tool}", "input": arguments}]}})
                output = mcp_call(servers[name], tool, arguments) if name in servers else f"ERROR: no {name} server"
                emit({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "content": [{"type": "text", "text": output}]}]}})
                outputs.append(output)
        if answer is None:
            answer = " | ".join(outputs) if outputs else f"echo: {text[-300:]}"
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}})
        with open(session_path, "a", encoding="utf-8") as session:
            session.write(json.dumps({"user": text, "assistant": answer}) + "\n")
        emit({"type": "result", "subtype": "error_during_execution" if is_error else "success", "is_error": is_error,
              "result": answer, "session_id": session_id, "total_cost_usd": 0.001, "num_turns": 1,
              "usage": {"input_tokens": 10, "output_tokens": 5,
                        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
