# Antigravity adapter: architecture and tool reference

Superseded: the core runtime's review gate ([Safety](../gemini.md#safety), [research](../research/antigravity-auto-review.md)) replaces this adapter's reviewer and command broker.

This describes the code in [`adapters/antigravity/`](../../adapters/antigravity/) as it works today, limits included. It is the first runtime adapter of CLI Subagents and currently ships its own MCP server. The plan for turning it into one adapter behind a shared server is in [design.md](../design.md).

## Execution path

```text
MCP host (Codex, Claude Code, any stdio MCP client)
  -> adapters/antigravity/.venv/Scripts/python.exe launch_mcp.py
  -> runtime secret loaded from machine-local state
  -> FastMCP stdio server (mcp_server.py)
  -> validate project, prompt, model and requested directories
  -> settings_guard: trust the project, scope writes, disable AI-credit overages
  -> temporary executor workspace with a PreToolUse hook
  -> agy --mode=accept-edits --model ... --effort ... --print=...
  -> Windows ConPTY capture
  -> prompt.md / result.md / summary.md / meta.json in a machine-local runs directory
  -> MCP result, or a background job handle
```

AGY runs its own agent loop, tools and login. The host only receives the result. The Gemini and Opus shortcuts select models inside AGY; neither one launches Claude Code.

AGY on Windows returns empty stdout when it is started from a non-interactive pipe, so the adapter runs it inside ConPTY through `pywinpty` and strips terminal control sequences from the captured text.

## Command review broker

Headless AGY has no approval prompt, so the adapter adds one:

1. A PreToolUse hook (`review_gate.py`) sees every tool call. Reads and ordinary edits inside the project are decided by fixed rules. Agent configuration, `.git`, credential stores and secret files are always denied.
2. Direct terminal commands are denied. The worker writes a small JSON request (`cwd`, `command`, `timeout_seconds`) into its temporary request directory and calls `reviewed_exec.cmd <approval-id>`.
3. The hook sends that request to a separate, tool-less Gemini call in plan mode (`reviewer_client.py`) together with [`review_policy.md`](../../adapters/antigravity/review_policy.md). The reviewer must answer with strict JSON. Anything else counts as a denial.
4. On approval the hook writes an HMAC-signed record bound to the request bytes, the project, the request directory and the running server. It expires after 60 seconds and can be used once.
5. `approved_exec.py` verifies and consumes the approval, checks that the request didn't change, strips secret-looking environment variables and runs the command.

OS file locks on a small pool of broker IDs stop two server processes from using the same slot. This is a code-level control, not a formally verified sandbox.

## Files

| File | Purpose |
|---|---|
| `launch_mcp.py` | Entry point. Loads the runtime secret, then starts the stdio server. |
| `mcp_server.py` | FastMCP tools, input validation, workspace trust, run records, async jobs. |
| `agy_adapter.py` | Builds AGY arguments, appends the terminal execution contract, sets up the gated workspace. |
| `agy_pty.py` | ConPTY capture and cleanup on Windows, subprocess fallback elsewhere. |
| `capabilities.py` | Probes `agy --version` and `--help` for supported flags; optional live probe. |
| `model_catalog.py` | Reads `agy models`, falls back to `models.json`. |
| `models.json` | Old model labels used only as a fallback. |
| `settings_guard.py` | Locked, minimal edits to AGY's `settings.json`. |
| `executor_workspace.py` | Temporary worker workspace, hook file, broker lease. |
| `hook_command.py` | Hook command quoting and Python launcher choice. |
| `review_gate.py` | Rule checks, reviewer call, approval issuing. |
| `reviewer_client.py` | Tool-less reviewer prompt and strict decision parsing. |
| `review_policy.md` | The reviewer's policy. Written for this project; not a copy of any vendor's internal prompt. |
| `broker_security.py` | State paths, token leases, request validation, HMAC approvals. |
| `approved_exec.py` | Consumes an approval and runs the exact approved command. |
| `reviewed_exec.cmd` | Windows entry point for the broker; finds `.venv` next to itself. |
| `scripts/bootstrap_local.py` | Creates local secrets and generates host and AGY config. Dry run by default. |
| `tests/` | Offline unit tests (no model calls). |

## MCP tools

Hosts may add a prefix, for example `mcp__antigravity_agents__antigravity_delegate_model`.

| Tool | Arguments and behavior |
|---|---|
| `antigravity_delegate_model` | `prompt`, exact `model`, absolute project `cwd`; optional `effort`, `timeout_seconds`, `mode`, `add_dirs`, `return_mode`, `summary_max_bullets`. Runs synchronously. |
| `antigravity_delegate_gemini` | `prompt`, `cwd`, common options. Pins `gemini-4-hydrogen-high` at `high` effort. |
| `antigravity_delegate_opus` | `prompt`, `cwd`, common options. Pins `claude-opus-4-6-thinking`; effort left unset. |
| `antigravity_delegate_gemini_async` | Same as the Gemini shortcut, but starts a background job. |
| `antigravity_delegate_opus_async` | Same as the Opus shortcut, but starts a background job. |
| `antigravity_run_status` | `job_id`, optional `include_output`. Returns `running`, `completed` or `failed` plus result metadata. |
| `antigravity_get_summary` | `job_id`. Summary text and path. |
| `antigravity_get_result_path` | `job_id`. Paths of the saved output. |
| `antigravity_current_settings` | `cwd`. Non-secret settings, trusted roots, limits, loaded default models. |
| `antigravity_capabilities` | `cwd`, optional `include_live_probe`. The live probe sends a real prompt. |
| `antigravity_list_models` | `cwd`. Live `agy models` with file fallback. |
| `antigravity_smoke_test` | `cwd`, `worker` (`gemini` or `opus`). Sends a real test prompt. |

Common values:

- `mode`: `advise` or `workspace`. `advise` only adds an instruction to the prompt. It is not a read-only sandbox.
- `return_mode`: `file_summary` (default) returns the summary and saves the full answer to disk; `full` returns everything.
- `timeout_seconds`: 10 to 1800, default 300. The host's own tool timeout must be longer (the generated Codex config uses 1900).
- `add_dirs`: every path must resolve inside `cwd`.
- Prompts are limited to 12,000 characters after the server adds its wrappers, with a warning at 8,000.

Use `antigravity_delegate_model` when you need to control both model and effort. A running server keeps old shortcut defaults until it is restarted.

## Local configuration and state

| Location | Purpose |
|---|---|
| `%USERPROFILE%/.codex/config.toml` | Codex MCP registration; its trusted projects also count as trusted roots. |
| `%USERPROFILE%/.gemini/antigravity-cli/settings.json` | AGY settings. The adapter adds project trust, a scoped write rule and broker command grants. |
| `%LOCALAPPDATA%/agy/bin/agy.exe` | Usual AGY install location; `AGY_CMD` overrides it. |
| `%LOCALAPPDATA%/Codex/antigravity-bridge/` | Broker secret, runtime secret, broker IDs, approvals, token leases and run records. Never commit this. |
| `<project>/.codex-agy-*` | Short-lived worker and reviewer workspaces. Removed after each run. |

Environment variables are listed in [configuration.md](configuration.md).

## Known limitations

1. One runtime. AGY is the only CLI this adapter launches.
2. Windows only in practice. The broker uses a `.cmd` launcher and ConPTY; the subprocess fallback is untested on Linux and macOS.
3. `advise` is not read-only. Both modes start AGY with `accept-edits`, and the gate allows ordinary edits inside the project.
4. Output is parsed from terminal text and `SUMMARY:` / `DETAILS:` markers, not from a structured event stream.
5. Some paths treat an exit status of `None` with non-empty output as success, so timeouts and crashes can look like answers.
6. Jobs live in memory in daemon threads (last 50 kept). There is no cancel tool, no persistence across restarts, no async variant of the generic tool and no native session resume.
7. The adapter changes AGY settings: it trusts the project, adds a write rule and sets `useG1Credits` to `false`.
8. Every reviewed command costs an extra AGY model call, which adds latency and uses quota.
9. The broker expects `.venv` next to `reviewed_exec.cmd`, and hook paths may not contain spaces.
10. Google's FAQ says consumer plans don't support wrapping `agy` inside third-party coding agents. See [provider terms](../research/provider-terms.md).

The unit tests run offline with mocks. They don't prove live CLI compatibility, account eligibility or resistance to every attack on the reviewer.
