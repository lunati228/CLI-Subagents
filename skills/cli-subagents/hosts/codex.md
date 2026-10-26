# CLI Subagents in Codex

## Native relay agents

Prefer the native relay agents over the MCP start/wait tools when the current
client exposes custom agent selection. They put each job in a native subagent
thread the user can inspect. `csa setup --host codex` installs these standalone
agents in the main Codex home's `agents/` directory; `csa update` refreshes them.
Start a new Codex session after installation. Installing the plugin alone does
not register the agents; run setup too.

| Worker CLI | Native `agent_type` |
|---|---|
| Claude Code | `cli-subagents-claude` |
| Codex | `cli-subagents-codex` |
| Antigravity / Gemini | `cli-subagents-antigravity` |
| OpenCode v2 | `cli-subagents-opencode` |

Use the native `spawn_agent` tool with that `agent_type` and a self-contained
task. For the default v1 tool schema, a Codex worker example is:

```json
{
  "agent_type": "cli-subagents-codex",
  "message": "model=gpt-6.1-sol\neffort=high\naccess=read\n\nReview the authentication module and report correctness risks with file references.",
  "fork_context": false
}
```

Use the fields in the actual tool schema if the client exposes another version
(v2 also requires `task_name` and uses `fork_turns="none"` for a fresh context).
Do not put the worker's model in the spawn call's `model` field. All four
relays select `gpt-6-luna` at low reasoning themselves. Option lines in the
message select the external worker: `model=`, `access=`, `account=`, `effort=`,
`cwd=`, `timeout=` and `override=` (the colon form is also accepted). Use
`override` only with the user's authorization described in the main skill.

Keep the returned native agent ID. Send follow-ups, answers and cancellation to
that same relay with native `send_input`, including after it finishes. With v2,
use `followup_task` to resume an idle relay; `send_message` alone does not start
an idle agent's next turn. The relay retains the csa job and event offset and
runs `csa message JOB --follow --from OFFSET --relay-file PATH`. A finished job continues its
worker conversation, and the relay validates any new job ID. Send `cancel` or
`stop` to cancel its worker before closing the native agent. Keep the native
thread open while follow-ups are needed.

The relay returns a token-checked `[csa] DONE` or `[csa] FAILED` line and the
worker's exact result. A QUESTION turn asks the parent for an answer; reply to
the same native agent. A missing or mismatched frame is an error. The relay
never performs the task itself. Its shell calls show worker events, while the
native agent notification delivers completion to the parent. Use `/subagents`
in the CLI to inspect threads; the app also exposes subagent threads, though
its exact live-output presentation has not been checked locally.

Custom agents inherit the parent's permissions and MCP configuration. The relay
uses native apply_patch only to create a plain-text payload at the absolute
path returned by `csa relay-path --workspace`, inside the caller's writable
`.csa-relay-inbox/`. `csa run/message --relay-file PATH` consumes and deletes
it. Keep quotes and Windows paths literal; apply_patch may normalize line
endings or add a final newline. csa writes an inbox `.gitignore` containing `*`
before issuing a path, keeping leftovers out of Git. Codex can disable selected
features but has no enforced per-role tool
allowlist. Keep shell execution and inbox file creation enabled in the parent.
If custom agent selection is
absent, native agents are disabled, or the relay model is unavailable, use the
MCP tools below. Do not claim a native thread was started for an MCP-only job.

## MCP fallback

After `csa setup`, or `codex plugin marketplace add lunati228/CLI-Subagents` and `codex plugin add cli-subagents@cli-subagents` from a terminal (or a `[mcp_servers.cli-subagents]` entry with `command = "csa"`, `args = ["mcp"]`), the tools are `subagent_start`, `subagent_wait`, `subagent_message`, `subagent_status`, `subagent_result`, `subagent_cancel`, `subagent_list` and `subagent_accounts` on the `cli-subagents` server. Use them when a native relay is unavailable.

## Waiting

Codex ends a single MCP call after a few minutes, so `subagent_wait` returns after at most 240 seconds, and `wait_seconds` on `subagent_start` and `subagent_status` is capped at the same value. If jobs are still running, do other work and call `subagent_wait` again. Nothing is lost between calls; messages and results wait for you.

## Where the shared setup lives

Subagents on the user's other ChatGPT accounts get the setup of the main Codex home (`~/.codex`, or `CODEX_HOME`). Add new things there:

| To add | Put it in |
|---|---|
| A skill | `~/.codex/skills/<name>/SKILL.md`, or `<project>/.agents/skills/` |
| Instructions | `~/.codex/AGENTS.md` (all projects) or `<project>/AGENTS.md` |
| An MCP server | `codex mcp add <name> -- <command>` or `[mcp_servers.<name>]` in `~/.codex/config.toml` |
| A plugin | `codex plugin add <plugin>@<marketplace>` |
| A custom agent | `~/.codex/agents/<name>.toml`, or `<project>/.codex/agents/` |
| Model defaults, features, profiles | `~/.codex/config.toml` |

Don't edit files inside `~/.cli-subagents/homes/codex/`; they're rebuilt from the main home before each run. Each account keeps its own login (`auth.json`), session history, and its own `[projects.*]`, `[hooks.state.*]` and `[notice.*]` tables in `config.toml`; the rest of that file comes from the main home.

## Mixing CLIs

Claude Code, Antigravity and OpenCode subagents started from here get a note listing `~/.codex/AGENTS.md`, the project's `AGENTS.md` and your skill folders. Claude Code also reads a project `AGENTS.md` by itself when the project has no `CLAUDE.md`.

OpenCode uses the user's own OpenCode profile. Omit `model` to keep its configured default; with none set, csa uses `opencode/deepseek-v4.1-flash-free`. An unavailable model fails the job instead of falling back. Free models are for personal projects only. OpenCode has no OS sandbox; see [OpenCode](../../../docs/opencode.md) before choosing it for edits.
