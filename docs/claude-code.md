# Claude Code: setup and use

This guide sets up CLI Subagents so a Claude Code session can hand work to `claude -p` workers running under your other Claude accounts, with the same MCP servers, plugins, skills and settings on every account.

Every worker is the official `claude` CLI with your own login, the same as opening a terminal yourself. Anthropic documents running several accounts with separate config folders; see [provider terms](research/provider-terms.md) for the sources.

## Requirements

- Claude Code installed (`claude` on PATH, or the copy bundled with the Claude desktop app, which is found automatically)
- Python 3.11 or newer
- One Claude subscription per extra account you want to use

## 1. Install

The package has no dependencies. Install it with any of:

```bash
pipx install git+https://github.com/lunati228/CLI-Subagents
```

```bash
uv tool install git+https://github.com/lunati228/CLI-Subagents
```

```bash
pip install --user git+https://github.com/lunati228/CLI-Subagents
```

This gives you the `csa` command. Check it with `csa --version`. If the command isn't found, add your Python scripts folder to PATH (`pipx ensurepath` does this for pipx).

Then add the plugin in Claude Code (Codex and Antigravity hosts: see [hosts](hosts.md)):

```text
/plugin install cli-subagents --marketplace lunati228/CLI-Subagents
```

On older Claude Code versions, run `/plugin marketplace add lunati228/CLI-Subagents` and then `/plugin install cli-subagents@cli-subagents`. From a terminal, `csa setup --host claude` does the same through `claude plugin marketplace add lunati228/CLI-Subagents` and `claude plugin install cli-subagents@cli-subagents`; see [hosts](hosts.md#claude-code). The plugin brings the MCP server, the `cli-subagents` skill and the `/cli-subagents:delegate` command.

Without the plugin (or in another MCP host), register the server yourself with the command `csa` and the argument `mcp`, for example in a project `.mcp.json`:

```json
{ "mcpServers": { "cli-subagents": { "command": "csa", "args": ["mcp"] } } }
```

## 2. Add your other accounts

Your normal Claude Code folder (`~/.claude`, or `CLAUDE_CONFIG_DIR` if you set one) is the primary setup. It is the single place where you manage MCP servers, plugins, skills, agents, `CLAUDE.md` and settings.

For each extra account:

```bash
csa accounts add max-2
```

This creates `~/.cli-subagents/homes/claude/max-2`, links in your shared setup, and opens Claude Code's own login. Sign in with the account that profile should use. If you belong to several organizations, switch to the right one in the browser before you click Authorize; the login uses whichever organization is active there.

The tool never reads or copies the login. Each account folder keeps its own credentials, state and session history.

## 3. Check

```bash
csa accounts
csa doctor --live
```

`csa accounts` shows each account's login state, plan, last known usage and cooldown without using quota. `doctor --live` runs one tiny job per account (on Haiku for Claude) and prints what each one loaded (model, permission mode, MCP servers, plugins, skills, agents), then lists any differences between accounts.

## 4. Use it

In Claude Code, ask for delegation in plain words ("have a subagent review the auth module on another account"), use `/cli-subagents:delegate <task>`, or let the skill decide.

### Subagents in the agent list

The plugin adds three agent types, one per CLI:

| Agent type | Runs the task in | Color |
|---|---|---|
| `cli-subagents:cli-subagents-claude` | Claude Code (`claude -p`) on one of your Claude accounts | orange |
| `cli-subagents:cli-subagents-codex` | Codex (`codex exec`) on one of your ChatGPT accounts | green |
| `cli-subagents:cli-subagents-antigravity` | Antigravity (`agy`) on your Google account | blue |

Claude starts them with its Agent tool like any other subagent, so they show up in the agent list and in the desktop app's task list, and Claude gets the usual notice when one finishes. The task description reads `<model> · <access> · <task>`, for example `Opus 5.5 · edit · fix the login test`, so you can see which model works on what. Subagents start them the same way. A job started with `csa run` in a shell or with `subagent_start` doesn't appear in the agent list, so the skill and the rules template tell Claude to always use the relay agents in Claude Code.

Each one is a small relay on Haiku. It pipes the complete prompt, including its leading option lines, into the fixed command `csa run --follow --window 60 --runtime <runtime> --relay -`. csa parses and validates those options; the relay never turns their values into shell arguments. Apostrophes, quotes, backslashes, dollar signs, backticks and Unicode stay literal. Unknown, duplicate or empty options and invalid values produce an authenticated FAILED result without starting a worker. Option parsing stops at the first task line or blank separator, so later `key=value` lines remain task text.

The relay prints the commands, tools, file edits, messages and reasoning summaries the worker CLI reports. Open the agent to watch. Claude Code shows Bash output when the call returns, so the relay returns about once a minute and continues with `csa follow <job> --from <offset> --window 60`. Allow for the next relay turn's delay; this is not a guaranteed refresh interval. Set `window=120` in the relay's initial prompt to use two-minute windows. When the worker asks a question, the relay stops and passes it to the main session; Claude answers with SendMessage and the relay sends the answer on. SendMessage "cancel" stops the job.

Other messages go to the running worker as follow-ups. After the job finishes, SendMessage to the same relay continues the same worker conversation as a new job. The relay uses `csa message <job> --follow --from <offset> --window 60 -`, adopts the new job and token only from that call's first authenticated header, and follows it to its result. The offset applies only while the original job is running; a new continuation starts from the beginning.

Prompts, answers and follow-ups all travel through literal stdin. Bash uses a quoted heredoc with a fixed delimiter, appending `_NEXT` until the delimiter does not occur as a complete payload line. PowerShell prefixes every payload line with `|` inside a single-quoted here-string, removes exactly that prefix with a fixed replacement, and pipes UTF-8 to csa. A payload line such as `'@` cannot close that here-string. Answers and follow-ups are sent as message text; csa does not parse their contents as job options.

The relay uses the main session's account. At a 60-second window, a ten-minute job needs about ten watch calls; a thirty-minute job needs about thirty, plus startup and the final reply. Each continuation re-reads the relay's growing context, mostly from cache. Codex reasoning summaries are capped at one short line each.

Cost estimate, not a measured relay bill (2026-10-19): assume a 20,000-token initial cache write, 10,000 more tokens written over the job, 25,000 cached input tokens per watch call on average, 100 output tokens per continuation, and 2,000 output tokens for copying the task and result. At [Haiku 5.5 list prices](https://platform.claude.com/docs/en/about-claude/pricing) for prompts up to 100,000 tokens ($0.125/M five-minute cache writes, $0.01/M cache reads, $0.50/M output), this is about $0.008 for ten minutes and $0.014 for thirty minutes. Compared with 540-second windows, the added continuation turns cost about $0.0025 and $0.008 respectively under these assumptions. Cache misses, large tasks/results and verbose event histories increase it; prompts over 100,000 tokens use higher rates. Subscription usage counts toward the plan's limits instead of a separate bill. The [older relay estimate](research/native-subagent-display.md#cost-of-the-relay) assumed nine-minute windows.

The relay needs `csa` on PATH. In auto mode, Claude Code's safety check decides on each `csa` call; in the other modes Claude Code asks before the first one, and an allow rule such as `Bash(csa:*)` in your settings skips the prompts.

From a terminal, `csa follow <job>` streams its history and new events immediately until it finishes or nine minutes pass (`--max-wait` changes that). Add `--window 60` only when you want one-minute calls with the window preserved in continuation commands.

### MCP tools

The relay agents are built on the same jobs as the MCP tools, which work in every host:

| Tool | Purpose |
|---|---|
| `subagent_start` | Start a job. Returns a job ID, or the result if it finishes within `wait_seconds` |
| `subagent_wait` | Wait for any of several jobs to finish or send a message (up to an hour; 240 s from a Codex host). Moves to the background in Claude Code and notifies you |
| `subagent_message` | Answer a subagent's question, send a follow-up while it runs, or continue a finished job |
| `subagent_status` | Progress, with an efficient wait |
| `subagent_result` | Final answer, outcome, account, session, cost (`max_chars` up to 80000, default 20000) |
| `subagent_cancel` | Stop a job and everything it started |
| `subagent_list` | Recent jobs |
| `subagent_accounts` | Accounts, logins, usage with reset times, and cooldowns |

From a terminal, the same jobs are available through `csa run` (add `--follow` to watch each step), `csa start`, `csa follow`, `csa status`, `csa result`, `csa cancel`, `csa jobs`, `csa wait` and `csa message`. `csa --home <folder>` uses a different CLI Subagents folder for one command.

`access` controls permissions:

- `read` runs the worker in `dontAsk` mode with an allowlist of read-only tools (`Read`, `Grep`, `Glob`, web search and fetch, native subagents) and shell commands (`git status`, `git diff`, `git log`, `git show`, `git blame`, `ls`, `cat`, `head`, `tail`, `wc`, `grep`, `rg`, and PowerShell's `Get-ChildItem`, `Get-Content`, `Measure-Object`). A worker-only `PreToolUse` hook checks commands and their options against a read-only allowlist and checks named paths for secrets. It denies unknown options, writes and execution helpers such as `git diff --output=FILE`, `git diff --ext-diff`, and `rg --pre=CMD`. Safe commands continue through the normal permission rules. If the check itself can't start or times out, the command is blocked (`onFailure: "block"`, Claude Code 2.1.295 or later). Plan mode isn't used because it blocks every MCP call, including the channel to the main session.
- `edit` uses Claude Code's auto mode by default (edits allowed, with Claude's own classifier checking risky actions). Change it with `edit_permission_mode`.
- `full` skips permission checks and is only for when you ask for it.

## Messages and wake-ups

Workers get three extra tools from a small built-in MCP server: `message_parent` (a progress note, no reply expected), `ask_parent` (a question that waits for your answer, 10 to 1800 seconds, default 600) and `check_parent_messages` (follow-ups sent while the worker runs).

Follow-ups reach a Claude worker in two ways. A `PostToolUse` hook that CLI Subagents adds to the worker shows new follow-ups after its next tool call. Because hook text is only a hint to the model, anything the worker hasn't read with `check_parent_messages` is sent again by the runner as a follow-up turn once the worker finishes (up to five times).

The plugin also ships two hooks for the main session (`hooks/hooks.json`):

- After `subagent_start`, an async `PostToolUse` hook (`csa hook await`) waits for the job. When it finishes or the worker asks a question or sends a note, the hook wakes an idle Claude Code session with a summary. The same hook runs after `subagent_message`, so once you answer a question it waits for the job again, or for the new job when you continued a finished one.
- On resume or compact, a `SessionStart` hook (`csa hook pending`) lists finished jobs in the current project whose result nobody collected.

The first retrieval through `subagent_wait`, `subagent_result`, `csa follow` or the hook claims the finished result and suppresses later automatic notifications. Explicit retrieval with `subagent_result` or `csa follow` can return it again.

## What every account shares

| From the primary setup | How it reaches each account |
|---|---|
| `agents/`, `commands/`, `output-styles/`, `rules/`, `workflows/` | One link to your folder (junction on Windows, symlink elsewhere) |
| Skills | One link per skill; the account's own `skills/synced` stays its own |
| `CLAUDE.md` | A hard link: the same file on disk, not a copy |
| Project memory (`projects/<project>/memory`) | Linked to your memory for that project |
| `settings.json` (hooks, permissions, env, model defaults) | Passed with `--settings`, minus login, gateway and plugin-install keys (`apiKeyHelper`, `forceLoginMethod`, `enabledPlugins`, `ANTHROPIC_*` credentials and similar) |
| User and local-scope MCP servers | Passed with `--mcp-config` |
| Installed, enabled plugins | Passed with `--plugin-dir` |
| Project `.mcp.json`, `.claude/` and `CLAUDE.md` | Already shared, because they live in the repository |

Links are tracked in `.cli-subagents-managed.json` inside each account folder. Copies are only a fallback (a hard link across drives, a link the OS refuses). If an account already has its own file or folder with the same name, it is left alone and reported as a conflict. Because these are links, anything a worker adds there (a new agent, a memory note) lands in your one shared setup.

One more per-account detail: Claude Code skips a project's own `permissions.allow` rules in `-p` runs when that account never trusted the folder. Put permission rules you need in your user `settings.json`, which every account gets.

Tools built into the Claude desktop app itself (its browser or terminal panes, for example) belong to the app, not to your config, so CLI workers can't have them.

Two things stay per account because they belong to the account on Anthropic's side:

- claude.ai connectors (the integrations you add on claude.ai). Each account shows its own. To make every account identical, set `connectors = "off"` in the config, and add the services you need as regular MCP servers in the primary setup.
- OAuth logins for remote MCP servers. If a server needs a browser login, run `csa launch --account <name>` once and complete it with `/mcp`.

## Choosing accounts and failover

With no `account` given, a job goes to the account with the lowest recent usage that has a free slot. Usage comes from the `rate_limit_event` messages Claude Code reports during each run (5-hour and weekly utilization, plus reset times).

An account with `explicit_only = true` is skipped by automatic selection and failover. Use `account=NAME` in the relay, `--account NAME` on the CLI, or the MCP `account` field to select it. When every extra account is disabled or explicit-only, unpinned jobs use the primary account even with `include_primary = false`.

When an account reaches its limit, it cools down and later jobs skip it. The cooldown ends at the first of these that is available: a `Retry-After` value in the error, the reset time Claude Code reported, or `cooldown_minutes` (default 60). It is never longer than 7 days. `csa accounts reset <name>` clears a cooldown early.

Failover is on by default. A job that was running when the limit hit continues on the next account: CLI Subagents copies the session transcript into that account's folder (`transfer_session`) and resumes the same conversation there with a short note, so the subagent picks up where it stopped. (Checked with Claude Code 2.1.295: a transcript copied to another account folder resumes with its full history.) If the transcript can't be moved, the job restarts with the original task and the partial output. A job pinned to one account with `account` doesn't fail over.

To stop at the limit instead, set:

```toml
[claude]
failover = "off"
```

## Limits

For an API-billed account, `spend_caps = [{ amount_usd = 300, window = "24h" }, { amount_usd = 500, window = "7d" }]` applies both rolling caps: $300 over the previous 24 hours and $500 over the previous 7 days. csa reserves the smallest available remainder before each attempt and passes it to Claude as `--max-budget-usd`; `budget` outcomes end without retry or failover, and `override` cannot bypass any cap. Accounts, doctor and `Room:` show every cap's spent and remaining amount. Optional `spend_since` starts counting from a quoted UTC date or datetime. The older single `spend_cap_usd`/`spend_period` form remains supported separately. See [account spend caps](configuration.md#account-spend-caps) for configuration and accounting rules.

This uses Claude Code's cost estimate, normally list price, which can exceed your invoice. Its native budget may overshoot while the final response and native subagents finish. Also set an Anthropic Console organization or workspace spend limit as the billing-side control; see [the verified sources and caveats](research/spend-caps.md).

csa counts the running jobs on the machine and refuses a start when your cap for that account or runtime is reached. It replies with one line, for example `Claude is at its limit: 2 of 2 subagents running (plan preset pro: 2 per account; change with max_jobs). Start this with runtime codex, or wait for one to finish.` Follow the suggestion; there is nothing to calculate. Set `plan`, `max_jobs`, optional `max_jobs_by_room` steps or model `pools` in `config.toml` to cap new starts. Pools can also set `per_account`, `total`, `weekly_jobs` and `cooldown_minutes` for model globs; other models keep working during a pool cooldown. If you set `plan = "auto"`, csa reads your plan from the account's login info. See [Configuration](configuration.md#limits).

Only the user can authorize extra parallel jobs. Never set override on your own judgment; use it only when the user asked for more jobs in this conversation, or confirmed after you warned them and told them how much of the 5-hour window is left. Put their words in a relay option line such as `override=user asked for 6 Codex agents`, the `override` field of `subagent_start`, or `--override "user asked for 6 Codex agents"` on `csa run` or `csa start`. The value must contain text. It bypasses csa's job caps, quota steps, usage ceiling, pool budgets and queueing. Spend caps, explicit-only selection, cooldowns after real usage-limit errors, model and effort restrictions, login, depth and access still apply. Override jobs still count toward later starts. Their override text appears in `csa jobs`, the follow header and status/result summaries. See [override details](configuration.md#a-user-authorized-override).

## Interactive sessions with the shared setup

```bash
csa launch
csa launch --account max-3 -- --model opus
```

`csa launch` opens a normal interactive Claude Code session on the account with the most room left (or the one you name), with the same shared setup. Anything after `--` goes to `claude`.

An interactive session doesn't take one of the account's job slots, so workers can still be sent to that account while it is open. Don't run `csa launch` on an account that workers are using at the same time.

The pool only moves workers between accounts. Your main Claude Code session keeps running on its own account, and its model usage is never spread across the other accounts.

## Troubleshooting

| Symptom | Check |
|---|---|
| `csa` not found by the plugin | Install the package (step 1) and make sure its scripts folder is on PATH, then restart Claude Code |
| `auth` outcome | Run `csa accounts login <name>` |
| An account loads different tools | Run `csa doctor --live` and read the differences; `csa accounts sync <name>` re-links the shared setup |
| Wrong organization after login | Log out with `claude auth logout` in that account (`CLAUDE_CONFIG_DIR` set to its folder), switch organization on claude.ai, log in again |
| `no_account` | Every account is cooling down or busy; `csa accounts` shows when the earliest resets, and `csa accounts reset <name>` clears a cooldown you know is over |
| Job state `lost` | The background runner stopped (for example the computer slept). Start the job again |
| Need to see everything a worker did | `events_path` in the result points to the raw event log |
