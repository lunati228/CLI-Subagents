# CLI Subagents in Claude Code

## Tools

The plugin is installed from a terminal with `csa setup`, or with `claude plugin marketplace add lunati228/CLI-Subagents` and `claude plugin install cli-subagents@cli-subagents`; no slash command is needed. It loads after a restart or `/reload-plugins`.

The plugin adds four agent types for your Agent tool: `cli-subagents:cli-subagents-claude` (orange), `cli-subagents:cli-subagents-codex` (green), `cli-subagents:cli-subagents-antigravity` (blue) and `cli-subagents:cli-subagents-opencode` (purple). Each is a small Haiku relay that starts the job with `csa run --follow` and prints the worker's steps as they happen, so the user can open it in the agent list and watch. Start them in the background; the description `<model> · <access> · <task>` is what the user sees. They need `csa` on PATH. In auto mode, Claude Code's safety check decides on each `csa` call; in other modes the user approves `csa` Bash calls (an allow rule such as `Bash(csa:*)` saves the prompts).

The plugin's MCP tools appear as `mcp__plugin_cli-subagents_cli-subagents__subagent_start` and so on (or `mcp__cli-subagents__...` if the server was added by hand). `/cli-subagents:delegate <task>` runs the whole loop.

## Waiting

`subagent_wait` may run up to an hour. After about two minutes Claude Code moves the call to the background and notifies you when it returns, so start jobs, call `subagent_wait`, and keep working on anything that doesn't depend on them.

The plugin's hooks also work without a wait. After `subagent_start`, a background hook wakes an idle session when the job finishes or asks a question. After a resume or compact, a hook lists finished jobs whose result nobody collected; read one with `subagent_result`. Each result is delivered once.

## Where the shared setup lives

Subagents on the user's other Claude accounts get everything from the main config folder (`~/.claude`, or `CLAUDE_CONFIG_DIR`). When you or the user add something new, put it there and every account has it on the next run:

| To add | Put it in |
|---|---|
| A skill | `~/.claude/skills/<name>/SKILL.md` (all projects) or `<project>/.claude/skills/<name>/SKILL.md` |
| A subagent definition | `~/.claude/agents/<name>.md` or `<project>/.claude/agents/` |
| A slash command | `~/.claude/commands/<name>.md` or `<project>/.claude/commands/` |
| A workflow | `~/.claude/workflows/` or `<project>/.claude/workflows/` |
| Rules | `~/.claude/rules/` or `<project>/.claude/rules/` |
| Instructions | `~/.claude/CLAUDE.md` (all projects) or `<project>/CLAUDE.md` |
| An MCP server | `claude mcp add --scope user ...` (all projects) or `<project>/.mcp.json` |
| A plugin | `claude plugin install <plugin>@<marketplace>` from a terminal (after `claude plugin marketplace add <owner/repo>`), or `/plugin install ...` in the main session |
| Settings, hooks, env, permissions | `~/.claude/settings.json` |

Never edit files inside `~/.cli-subagents/homes/`; those account folders are links to the setup above and are rebuilt before each run. Things that stay per account: the claude.ai login, claude.ai connectors, and browser logins for remote MCP servers (`/mcp` once per account). Tools built into the Claude desktop app itself (browser or terminal panes) belong to the app and aren't available to workers.

## Mixing CLIs

A Codex, Antigravity or OpenCode subagent started from here gets a note listing `~/.claude/CLAUDE.md`, the project's `CLAUDE.md` and `.claude/rules`, and your skill folders, and reads them first. Mention anything else it needs in the task.

OpenCode uses the user's OpenCode profile and is available when csa finds the v2 binary. Omit `model` to keep its configured default. Free models are for personal projects only, and OpenCode has no OS sandbox. See [OpenCode](../../../docs/opencode.md) for access levels and data terms.
