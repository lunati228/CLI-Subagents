---
name: cli-subagents
description: How to run subagents through CLI Subagents (the cli-subagents-* agent types in Claude Code; subagent_start, subagent_wait, subagent_result, subagent_message elsewhere). Use it like your built-in subagent tool whenever work can be delegated, run in parallel, reviewed independently, or moved to another of the user's accounts or another agent CLI (Claude Code, Codex, Antigravity). Skip it for trivial answers and tiny edits.
---

# CLI Subagents

These tools are your subagent tool. Use them the way you would use a built-in "spawn a subagent" tool: hand over a self-contained task, keep working, collect the result, check it. Behind them, each subagent is a separate agent CLI (Claude Code, Codex or Antigravity) running on one of the user's own accounts with the same MCP servers, plugins, skills and instructions as the main setup. Choosing an account, and switching when one runs out of quota, happen on their own; you don't manage them.

## In Claude Code: always start a relay agent

In Claude Code, start every CLI Subagents job with your Agent tool and one of the three relay agent types. This holds at every depth: a subagent that hands work to Codex or Gemini starts a relay agent the same way.

| Runs in | `subagent_type` |
|---|---|
| Codex | `cli-subagents:cli-subagents-codex` |
| Antigravity (Gemini) | `cli-subagents:cli-subagents-antigravity` |
| Claude Code on another account | `cli-subagents:cli-subagents-claude` |

Don't run `csa run`, `csa start` or `csa follow` in your own shell, and don't call `subagent_start`. A job started that way never appears in the user's agent list, and your turn is blocked while you wait on it. A relay agent appears with its own color, the user can open it and watch every step the worker takes (commands, edits, messages, reasoning), and you get the usual notice when it finishes.

```text
Agent tool:
  subagent_type: cli-subagents:cli-subagents-codex
  description: Sol 6.1 high · edit · fix the login test
  run_in_background: true
  prompt: |
    model=gpt-6.1-sol
    effort=high
    access=edit

    <the complete, self-contained task>
```

- Description: `<model> · <access> · <task in a few words>`. The user reads it to see what runs where.
- Prompt: option lines first, one per line (`model=...`, `effort=...`, `access=read|edit`, and only when needed `account=NAME`, `cwd=PATH`, `timeout=SECONDS`, `override=USER_WORDS` under the Limits rule below), then a blank line and the complete task written as below. `access` defaults to `edit` in the Claude relay and `read` in the Codex and Antigravity relays.
- Start it in the background and keep working. Don't poll or wait in a shell; the notice arrives when it finishes.
- Its final message is the worker's result, unchanged. If the worker asks something, the agent stops and tells you; answer with SendMessage to that agent and it passes the answer on. While the job runs, SendMessage "cancel" stops it and anything else goes to the worker as a follow-up. After the job has finished, SendMessage to the same relay continues the worker's conversation as a new job; the relay follows it and returns its result.

The relay sends the whole initial prompt to `csa run --follow --window 60 --runtime <runtime> --relay -` on stdin. csa reads the leading option lines; the relay never shell-quotes their values. Keep the prompt format above, including the blank separator before the task. Invalid options return an authenticated FAILED result. Prompts, answers and follow-ups use literal stdin: Bash checks its heredoc delimiter for collisions, and PowerShell prefixes payload lines inside a literal here-string before removing those prefixes and piping UTF-8. Quotes and shell metacharacters stay literal. Answers and follow-ups remain message text, even when they begin with `key=value`.

The MCP tools below work everywhere. In Codex, `csa setup --host codex` installs the same three relays as native agents (`cli-subagents-codex` and so on); start them with Codex's own subagent tool as [the Codex note](hosts/codex.md) shows, and use the MCP tools there only when those agents are missing. In Antigravity, `csa setup --host antigravity` installs them as a companion plugin of subagents; start them as [the Antigravity note](hosts/antigravity.md) shows. In Claude Code, too, use the MCP tools only when the relay agent types are missing.

## The loop with the MCP tools

1. Start: `subagent_start` with a complete task (`prompt`), the project folder (`cwd`, absolute) and `access` (`read` for analysis and review, `edit` for changes). Claude workers on `read` get read-only tools (file reading, search, web) plus shell commands with read-only options, including `git diff`; a worker hook rejects writes, execution helpers and named secret paths. Start several jobs before waiting if the work is independent.
2. Wait: `subagent_wait` with the job IDs. It returns when a job finishes or a subagent sends a message or question. Call it again for the jobs still running.
3. Answer: if a result shows `open_questions`, reply with `subagent_message` and `reply_to` set to the question id. To give a running subagent new instructions, send `subagent_message` without `reply_to`. In Claude Code a hook also wakes an idle session when a job finishes or asks something, so you can end your turn after starting jobs.
4. Collect and check: finished jobs come back with `outcome` and `result`. `success` means the subagent finished. Collecting a result claims it and suppresses later automatic notifications. Explicit `subagent_result` or `csa follow` calls can return it again. Still read the diff, run the tests and confirm the files exist before relying on it.
5. Follow up: `subagent_message` to a finished job continues the same conversation as a new job.

## Writing the task

The subagent can't see this conversation. State the goal, the files or folders involved, what it must not touch, decisions already made, how to verify the result, and what to report back. Point to files instead of pasting them.

Optional arguments: `runtime` (`claude` by default, `codex`, `antigravity`) when a specific agent or a second opinion from another vendor helps; `model`; `effort`; `max_turns` (Claude workers only); `timeout_seconds` (default 1800); `resume_job` to continue an earlier job's conversation; `account` to pin one account; `add_dirs`.

`access: "full"` skips the CLI's own permission checks. Use it only when the user asked for that.

Codex and Antigravity workers have their own web search and page reading, which reach sites such as Reddit that block other tools, so they suit web research. Antigravity workers find YouTube videos and read their title and description, but can't watch them or read transcripts.

## Limits

Limits are enforced by csa. If a start is refused, follow the suggestion in the message; don't count quotas yourself.

Accounts marked `explicit_only` require an explicit `account`; automatic selection and failover skip them. All account spend caps apply together, including rolling windows, and cannot be bypassed with `override`; a `budget` outcome stops without retry or failover, so follow its message or wait for room under every cap.

Never set override on your own judgment; use it only when the user asked for more jobs in this conversation, or confirmed after you warned them and told them how much of the 5-hour window is left. Use csa's reported usage and reset time; if the window is unknown, say so. Otherwise ask the user or use the home model.

Put the user's own words in `override`, for example the relay line `override=user asked for 6 Codex agents`, the `override` field on `subagent_start`, or `--override "user asked for 6 Codex agents"` on `csa run` or `csa start`. Empty values are rejected. This bypasses only csa's job caps, quota steps, usage ceiling, pool budgets and queueing. Account and pool cooldowns after real usage-limit errors, `models`, `max_effort`, login, depth and access still apply. Override jobs count toward later starts. The text is stored and shown in job lists, follow headers and status/result summaries. A new start needs its own override; a follow-up to a finished job does not inherit it.

## Outcomes

| outcome | What to do |
|---|---|
| `success` | Check the work |
| `usage_limit` | The selected account hit a limit. Unpinned jobs try other accounts when failover is enabled. Check the message for reset times. |
| `no_account` | All accounts are busy or cooling down; wait or tell the user |
| `auth` | For an extra account, ask the user to run `csa accounts login <name>`; add `--runtime codex` or `--runtime antigravity` for non-Claude accounts. For a primary login, use the vendor CLI (`claude auth login`, `codex login` or `agy`). |
| `timeout`, `max_turns` | Narrow the task or raise the limit |
| `error`, `lost` | Read `message` and `warnings`; retry once at most. `model at capacity after N retries` means csa already waited and retried, and the model stayed overloaded. Fall back as your delegation rules say; do not manage another retry timer. |

## Per host

Read the note for the CLI you are running in once: [Claude Code](hosts/claude-code.md), [Codex](hosts/codex.md) or [Antigravity](hosts/antigravity.md). It covers how waiting works there and where to put new skills, MCP servers and instructions so every subagent shares them.

- Claude Code: `subagent_wait` can run for up to an hour; Claude Code moves it to the background and notifies you when it returns.
- Codex: waits end after 240 seconds; call `subagent_wait` again until the jobs finish.
- Antigravity: keep waits to a few minutes and call again.

## What subagents get

Subagents in the same CLI as you load the same setup you have. Subagents in another CLI get a short note listing your instruction files and skill folders to read. Claude and Codex workers get `message_parent` (progress notes), `ask_parent` (a question; it waits for your answer) and `check_parent_messages` (follow-ups you send). Antigravity workers get them only with an effective account cap of exactly 1 and a managed worker folder; the primary file-login fallback has no parent channel. See [Gemini messages](../../docs/gemini.md#messages). Subagents can't start subagents of their own. Tools built into the Claude desktop app itself (its browser or terminal panes, for example) aren't available to them; everything in your config folders and plugins is.

Never put secrets or credentials in a prompt.
