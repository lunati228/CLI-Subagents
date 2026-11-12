# Remote runtime: live check with the real Claude Code (2026-11-12)

What ran: `tests/remote_live_check.py`, the whole csa path on one Windows 11 machine. csa's runner started the relay;
the relay started the PC bridge with csa-parent behind it and ran `tests/fake_ssh.py`, which ran the real server
module, which ran the real Claude Code 2.1.318 in an Anthropic API-key account folder. Only ssh was a stand-in.

```bash
python tests/remote_live_check.py --account-home <API-key account folder> --model claude-haiku-5-5 --budget 1.50 --max-turns 20 --timeout 900
```

- Script sha256: `3567833e2965002789b4d08a23b958bb5d6723ffb657f9e1ff4b876d6bf5d37b`
- Code: commit `e1110af` plus the script; Python 3.12.10, Windows 11 26300
- Date: 2026-11-12, 12:29 Europe/Berlin
- Cost: $0.00304 by Claude Code's figure (list price, Haiku 5.5), cap `--max-budget-usd 1.50`

## Results

- Both MCP servers connected over HTTP through the bridge: `pc` and `csa-parent` (the stdio proxy).
- Built-in tools beyond the server's allowlist: none. The worker called `message_parent` first, then `pc_run`,
  `pc_glob`, `pc_read`, `pc_edit` and `pc_run` again; the bug was fixed and the tests pass afterwards.
- The `.env` read was refused by the bridge ("this file may hold secrets") and the file was unchanged.
- Live follow-up: queued 3.1 s after the start, delivered into the running conversation (`message_status`
  delivered). Claude Code answered the task and the follow-up with a single `result` at 10.2 s, and the final reply
  ends with the follow-up's word. The server counts one result per message, so its 10-second idle close ended the
  input; the job was done at 22.6 s. One attempt; no follow-up turn was needed.
- The check removed what the job added to the account folder: the bundle marker and the job's session folder.

## Raw report

```json
{
  "date": "2026-11-12 12:29 W. Europe Standard Time",
  "claude_code": "2.1.318 (Claude Code)",
  "model_asked": "claude-haiku-5-5",
  "model_reported": "claude-haiku-5-5",
  "outcome": "success",
  "message": "",
  "account": "live",
  "seconds": 23,
  "budget_usd": "1.50",
  "cost_usd": 0.00303943,
  "timing_s": {
    "accepted": 1.0,
    "follow_up_queued": 3.1,
    "results_seen": [
      10.2
    ],
    "job_done": 22.6
  },
  "attempts": 1,
  "mcp_servers": [
    {
      "name": "pc",
      "status": "connected",
      "source": "dynamic"
    },
    {
      "name": "csa-parent",
      "status": "connected",
      "source": "dynamic"
    }
  ],
  "builtin_tools_beyond_allowlist": [],
  "pc_tools_seen": [
    "mcp__pc__pc_edit",
    "mcp__pc__pc_glob",
    "mcp__pc__pc_grep",
    "mcp__pc__pc_list",
    "mcp__pc__pc_read",
    "mcp__pc__pc_run",
    "mcp__pc__pc_write"
  ],
  "parent_tools_seen": [
    "mcp__csa-parent__ask_parent",
    "mcp__csa-parent__check_parent_messages",
    "mcp__csa-parent__message_parent"
  ],
  "tool_calls": [
    "ToolSearch",
    "mcp__csa-parent__message_parent",
    "mcp__pc__pc_run",
    "mcp__pc__pc_glob",
    "mcp__pc__pc_read",
    "mcp__pc__pc_read",
    "mcp__pc__pc_edit",
    "mcp__pc__pc_run",
    "mcp__pc__pc_read"
  ],
  "pc_audit": [
    {
      "tool": "message_parent",
      "server": "csa-parent",
      "allowed": true,
      "reason": "proxied"
    },
    {
      "tool": "pc_run",
      "server": null,
      "allowed": true,
      "reason": "failed"
    },
    {
      "tool": "pc_glob",
      "server": null,
      "allowed": true,
      "reason": "ok"
    },
    {
      "tool": "pc_read",
      "server": null,
      "allowed": true,
      "reason": "ok"
    },
    {
      "tool": "pc_read",
      "server": null,
      "allowed": true,
      "reason": "ok"
    },
    {
      "tool": "pc_edit",
      "server": null,
      "allowed": true,
      "reason": "ok"
    },
    {
      "tool": "pc_run",
      "server": null,
      "allowed": true,
      "reason": "ok"
    },
    {
      "tool": "pc_read",
      "server": null,
      "allowed": false,
      "reason": "this file may hold secrets"
    }
  ],
  "messages_to_parent": [
    "Starting: running `python -m unittest -v` in the project, then I'll fix the failing bug, rerun the tests, and try reading .env."
  ],
  "questions_answered": [],
  "follow_up": {
    "id": "a5ec3180",
    "delivered": true,
    "in_result": true
  },
  "relay_events": [
    "accepted",
    "delivered"
  ],
  "results_in_stream": 1,
  "bug_fixed": true,
  "tests_exit_code_after": 0,
  "env_file_unchanged": true,
  "result": "Final test result: `python -m unittest -v` passes (1 test, OK) after changing `add` in `calc.py` from `a - b` to `a + b`.\nReading .env did not work: the read tool refused with \"this file may hold secrets\", and I did not try another way around that block.\n\nPINEAPPLE",
  "server_usage_log": [
    "{\"week\": \"2026-W46\", \"user\": \"me\", \"account\": \"live\", \"model\": \"claude-haiku-5-5\", \"input_tokens\": 12, \"output_tokens\": 1079, \"cache_creation_input_tokens\": 14176, \"cache_read_input_tokens\": 72673, \"cost_usd\": 0.00303943, \"is_error\": false}"
  ],
  "removed_from_account_folder": [
    ".remote-agent-managed.json",
    "projects/C--Users-USER-AppData-Local-Temp-csa-remote-live-9czdk2uo-server-state-users-me-work-my-app-4c9a75cc"
  ],
  "checks": {
    "job succeeded": true,
    "bug fixed and tests pass": true,
    ".env unchanged": true,
    "no built-in file or shell tools": true,
    "pc tools offered": true,
    "parent tools offered": true,
    "message_parent arrived": true,
    "follow-up reached the worker": true
  }
}
```
