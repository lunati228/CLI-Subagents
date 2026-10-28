# Configuration

CLI Subagents keeps everything under `~/.cli-subagents/` (or `CLI_SUBAGENTS_HOME`). `csa accounts add` writes the config for you; edit it by hand for the other options.

## Model capacity retries

When a worker exits because the model is temporarily overloaded, csa retries on the same account. It resumes the conversation if the CLI supplied a session ID; otherwise it repeats the task. The default waits are 30, 60 and 120 seconds. The CLI's own retries finish before csa starts this schedule.

Set these keys under `[claude]`, `[codex]`, `[antigravity]` or `[opencode]`:

```toml
[codex]
capacity_retries = 3          # additional attempts; 0 disables capacity retries
capacity_retry_seconds = 30   # initial wait, doubled for each retry
```

`capacity_retries` must be a nonnegative integer; `capacity_retry_seconds` must be a positive integer. Both default to the values above for every runtime. During a wait, status shows the retry number and delay, and `csa follow` prints a `[csa] model at capacity, retrying in 30 s (1 of 3)` notice with its job ID and token. The account slot stays reserved, and cancellation and the task timeout still apply. Capacity retries and waits share the task's timeout budget. Exhausting the retries ends the job as `error` with `model at capacity after 3 retries`. Capacity failures do not set quota cooldowns or switch accounts.

See [the signal research](research/capacity-errors.md) for the overload messages each runtime recognizes.

## OpenCode discovery

OpenCode is available without account registration when csa finds `opencode` on PATH. If it is missing, csa leaves it out of the available runtimes and checks again on later starts. OpenCode v2 (`@opencode/cli`) is required; v1 gets a clear upgrade message. To disable discovery:

```toml
[opencode]
enabled = false
```

`enabled` defaults to `true`. Optional `binary` overrides detection, and `default_model` overrides your OpenCode model for csa jobs. An explicit job model has priority. Without either override, csa keeps the model in your OpenCode config; if none is set, it chooses `opencode/deepseek-v4.1-flash-free`. An unavailable model or effort variant fails the job; csa never lets OpenCode fall back to another model. See [OpenCode](opencode.md#choosing-a-model-and-starting-a-job).

This first version uses one primary OpenCode profile and your existing XDG directories. It does not accept extra OpenCode account tables. Free models are for personal projects only. Their per-IP daily allowance resets at UTC midnight, and a free-limit error stops without account rotation. Clear capacity errors use the retry settings above.

OpenCode `read` and `edit` workers get csa's fixed review rules; a reviewer model is not wired up yet. OpenCode has no OS sandbox, so `edit` workers may change files in the project but can't run tests, builds or other commands that change anything, and web access and other MCP servers are denied. See [OpenCode](opencode.md) for the access levels, generated MCP and instruction config, data terms and Fence sandbox recommendation.

## config.toml

```toml
job_retention_days = 7       # finished job records older than this are deleted
max_depth = 1                # 1 = workers can't start their own subagents
# max_jobs = 8               # most subagents running at once on this machine, all runtimes (default: no cap)

[claude]
primary = "~/.claude"        # the shared setup (default: CLAUDE_CONFIG_DIR or ~/.claude)
include_primary = false      # also run workers on the primary account
failover = "on-limit"        # continue on the next account at a usage limit; "off" to stop
max_jobs_per_account = 1     # parallel workers per account folder
connectors = "account"       # "account" keeps each account's claude.ai connectors; "off" hides them
edit_permission_mode = "auto"  # permission mode for access="edit"
cooldown_minutes = 60        # cooldown when the limit error has no Retry-After or reset time (any cooldown is capped at 7 days)
# binary = "C:/path/to/claude.exe"   # only if auto-detection fails
# default_model = "sonnet"

[[claude.accounts]]
name = "max-2"
home = "~/.cli-subagents/homes/claude/max-2"
enabled = true

[codex]
primary = "~/.codex"        # default: CODEX_HOME or ~/.codex
failover = "on-limit"
# windows_sandbox = "unelevated"   # on Windows: "unelevated", "elevated" or "off" (see docs/codex.md)
# network_access = true            # edit jobs get network inside the sandbox; false keeps Codex's default (off)
# reasoning_summary = "detailed"   # "concise", "auto", "none" or "off"; your Codex setting takes precedence

[[codex.accounts]]
name = "plus-2"
home = "~/.cli-subagents/homes/codex/plus-2"
```

The `[codex]` section takes the same keys as `[claude]`, but ignores `connectors` and `edit_permission_mode`. `connectors` only applies to Claude. Codex uses its own approval and sandbox flags for each `access` level; see [Codex](codex.md#using-it). `share_parent_mcp` (default `true`) applies to Codex, Antigravity and OpenCode: workers get the parent's MCP servers (see [codex.md](codex.md#what-a-worker-gets-from-a-claude-code-session)); for Antigravity your own Gemini servers win when two have the same name. `web_search` only applies to Codex: `"disabled"`, `"cached"`, `"indexed"` or `"live"`, passed as `-c web_search="<value>"` on new and resumed runs. Leaving it unset preserves Codex's own default (`"cached"`). `windows_sandbox` only applies to Codex: on Windows, read and edit jobs get `-c windows.sandbox="<value>"` unless your Codex `config.toml` already sets `[windows] sandbox`; `"off"` adds nothing. `network_access` only applies to Codex and defaults to `true`: `edit` jobs get `-c sandbox_workspace_write.network_access=true`, so commands in the sandbox can reach the network and open local sockets (test runners such as VSTest need them). Set it to `false` to keep Codex's own default, which blocks both; see [codex.md](codex.md#using-it). `[antigravity]` takes `binary`, `default_model`, `edit_permission_mode` (`accept-edits` by default, or `auto` where the account supports it) and `share_parent_mcp`, the review keys below, and the account keys further down.

```toml
[antigravity]
reviewer = "agy"            # who judges actions the fixed rules can't settle: "agy", "claude" or "off"
# reviewer_model = "gemini-3.8-flash-low"   # default for "agy"; "haiku" is the default for "claude"
# reviewer_account = "max-2"                # the Claude account for reviewer = "claude" (default: your primary)
sandbox = true              # run edit jobs with agy --sandbox
```

With `reviewer = "off"` only the fixed rules decide, and everything they would hand to the reviewer is denied. See [Safety](gemini.md#safety) for what the gate does.

Extra Antigravity accounts are at your own risk (Google's FAQ says consumer plans don't support automated account switching; see [Gemini](gemini.md#more-than-one-google-account)). As for the other runtimes, `failover` is `"on-limit"` by default: jobs rotate through the primary and the enabled extra accounts (`include_primary` is `true` by default here). Set `failover = "off"` under `[antigravity]` to keep jobs without `account` on the primary account; an extra account is then used only when a job pins it. An Antigravity account folder stands in for a home folder: it gets its own `.gemini`.

```toml
[antigravity]
failover = "off"            # "on-limit" rotates between the accounts (use at your own risk)

[[antigravity.accounts]]
name = "google-2"
home = "~/.cli-subagents/homes/antigravity/google-2"
```

When every extra account is disabled or explicit-only, workers without an `account` run on the primary account. That is still useful from another host (for example Codex delegating to Claude Code), and with `include_primary = true` the primary account joins the pool.

`max_jobs_per_account` defaults to 1 (see Limits for per-account caps) because several Claude Code processes writing the same state file at once have corrupted it in the past. Raise it if you know your version handles it.

## Explicit-only accounts

Set `explicit_only = true` in an account entry to keep it out of automatic selection and failover. This applies to every runtime. The account runs only when a start names it with `account=NAME` in a relay prompt, `--account NAME` on `csa run` or `csa start`, or the MCP `account` field. It must also be enabled and pass the other limits. With only explicit-only extra accounts and `include_primary = false`, unpinned jobs still fall back to the primary account.

`csa accounts list`, `subagent_accounts` and the `Room:` line show the flag. It defaults to `false`. `override` does not make an explicit-only account eligible for an unpinned job.

## Account spend caps

Spend caps currently enforce budgets for Claude workers; other runtimes reject these spend settings. Add these lines to an existing `[[claude.accounts]]` entry, keeping its `name`, `home` and `enabled` settings:

```toml
explicit_only = true
spend_caps = [
  { amount_usd = 300, window = "24h" },
  { amount_usd = 500, window = "7d" },
]
# spend_since = "2026-10-23"  # optional: exclude costs settled before this UTC date
```

Both caps apply: a $300 allowance over the previous 24 hours and $500 over the previous 7 days. Rolling windows move continuously with the current time. An attempt settled exactly 24 hours or 7 days ago has left that window; an attempt settling now counts.

| Key | Meaning |
|---|---|
| `spend_caps` | List of simultaneous caps, each with a positive, finite `amount_usd` and a `window`, such as `"24h"` or `"7d"`. Every cap must have room for the next attempt. Unset means no list of caps. |
| `spend_since` | Optional ISO UTC date or datetime that excludes costs settled earlier, for example `"2026-10-23"` or `"2026-10-23T14:30:00Z"`. A date starts at midnight UTC; a datetime without an offset is UTC. Other timezone offsets are rejected. Each window also respects this accounting start. Move it forward to start counting anew. |
| `spend_cap_usd` | Older single-cap form: positive, finite USD cap for accounted spend. Use this with `spend_period` instead of `spend_caps`; combining the two forms is rejected. |
| `spend_period` | Period for `spend_cap_usd`: `"month"` by default, or `"day"`, `"week"`, `"total"`. Calendar periods use UTC: days start at midnight, weeks on Monday, and months on the first. `"total"` never rolls over. |

csa sums each account's attempt `cost_usd`, including failed attempts, using the time each attempt ends. Costs come from Claude Code's `total_cost_usd`, normally a list-price estimate; managed pricing can change that estimate. A capped attempt that ends without a usable cost consumes its full granted budget, including cancellation, timeout or crash. csa does not guess prices from incomplete token events. Existing job history is imported; records with neither cost nor saved budget also consume allowance conservatively. Set `spend_since` to exclude history you intend to leave outside the allowance. Spend history survives normal job-record cleanup in a separate ledger.

Before starting an attempt, csa subtracts settled spend and running reservations from each cap. It uses the smallest available remainder as the attempt's budget. If any window has less than $1 available, the start is refused with the spend, cap and blocking window. For example, $240 spent in 24 hours and $430 in 7 days leaves $60 and $70 respectively, so the next attempt receives $60.

csa reserves that budget under a lock and passes it to Claude as `--max-budget-usd`. The reservation counts against every cap until settlement. Concurrent starts see that reservation and can use only room left under every cap; increasing `max_jobs` or using `override` cannot allocate the same money twice. All running reservations count even as windows advance or `spend_since` changes. A budget stop has outcome `budget` and ends without retry or failover. `when_full = "queue"` and `override` cannot bypass spend checks.

`csa accounts list`, `subagent_accounts`, `csa doctor` and the `Room:` line show every cap with its spent, reserved and remaining USD. Remaining is the amount available after reservations. `csa accounts reset NAME` clears cooldowns; use `spend_since` to reset spend accounting. A future `spend_since` refuses jobs until that time. Resuming an explicit-only account requires naming it again.

The estimate can be higher than the invoice, so this accounting errs on the safe side. Resumed Claude sessions can report earlier spend again, and summing attempt costs can count it again. Claude checks its native budget after a response completes, so the last response and native subagents stopping can exceed it. Set an organization or workspace spend limit in the Anthropic Console as the authoritative billing-side control, and leave headroom. The local cap covers csa workers using this state folder; interactive sessions and other machines need their own controls. See [spend-cap research](research/spend-caps.md) for sources and caveats.

## Reasoning summaries

`[codex] reasoning_summary` defaults to `"detailed"`. Allowed values are `"detailed"`, `"concise"`, `"auto"`, `"none"` and `"off"`. New runs and resumes get `-c model_reasoning_summary="<value>"` unless your Codex config already sets it (including project config and the active profile). `"none"` requests no summaries; `"off"` adds nothing. Other runtimes do not accept this key. See [Codex reasoning summaries](codex.md#reasoning-summaries) for model support and display details.

## Follow windows

`csa run --follow`, `csa follow` and `csa message --follow` accept `--window SECONDS`, a positive integer. Bare `--window` uses 60 seconds. This bounds each watch call and repeats the window in its authenticated continuation command, including after a worker question. `--max-wait` still provides an upper bound when it is shorter. The worker keeps running when a watch call returns.

The Claude Code relay agents use `--window 60` so the app can show a finished Bash call's output about once a minute, plus the relay's next-turn delay. Supply `window=120` in the relay's initial prompt to change it. Without `--window`, terminal use keeps streaming with the existing 540-second watch limit (`--max-wait` changes it). This is a command option, not a `config.toml` key.

## Limits

CLI Subagents counts active jobs in `~/.cli-subagents/jobs` before it starts one. That covers nested subagents and other chat sessions on the same machine. With the default `when_full = "refuse"`, a full explicit account, plan, runtime or global cap refuses the start at once. The message names the cap and what to do instead. Accounts using only `max_jobs_per_account` queue extra jobs in the runner instead.

Plan presets and extra caps are opt-in. By default, each account runs one worker (`max_jobs_per_account = 1`). The usage ceiling also applies by default: accounts at or above 95% of a known usage window get no new starts. Presets apply only when you set `plan`.

| Key | Where | Meaning |
|---|---|---|
| `plan` | `[codex]`, `[claude]`, `[antigravity]` (the primary account) and each `[[<runtime>.accounts]]` | A preset: `free`, `plus`, `pro`, `max-5x`, `max-20x`, `team`, `ultra` and so on, per runtime (table in [research/limits.md](research/limits.md)). `"auto"` reads a Claude account's plan from its login info. No `plan` means no preset. |
| `max_jobs` | an account entry | Most jobs running on that account. Beats the preset. |
| `max_effort` | an account entry | Highest effort. A higher request is lowered to it and the job gets a warning. |
| `models` | an account entry | Models this account may run. A job asking for another model skips the account. |
| `primary_max_jobs`, `primary_max_effort`, `primary_models` | `[<runtime>]` | The same three for the primary account. |
| `max_jobs` | `[<runtime>]` | Most jobs running on that runtime, over all accounts. |
| `max_jobs_by_room` | `[<runtime>]` | Optional quota-scaled runtime cap; descending `[percent_left, jobs]` steps (below). |
| `pools` | `[[<runtime>.pools]]` | Optional model globs, caps, rolling weekly budgets and separate cooldowns (below). |
| `max_jobs` | top level | Most jobs running over all runtimes. |
| `usage_ceiling` | `[<runtime>]` | Default `0.95`. With `when_full = "refuse"`, an account at or above this share of its 5-hour or weekly window gets no new jobs. It applies only when usage is known; csa never guesses. |
| `usage_ceiling_windows` | `[<runtime>]` | Optional list of window names that `usage_ceiling` counts. For example `usage_ceiling_windows = ["five_hour"]` makes only the 5-hour window block new jobs, so a nearly full weekly window no longer refuses starts. Unset counts every window. |
| `when_full` | `[<runtime>]` | `"refuse"` (default): full explicit caps refuse the start. `"queue"`: admit jobs despite full slots, cooldowns or the usage ceiling, with the exceptions below. |
| `max_jobs_per_account` | `[<runtime>]` | Default `1` for accounts without an explicit cap or plan preset. Extra jobs wait for a slot in the runner; this setting alone never refuses a start. |

Order for one account: its own `max_jobs`, then its plan preset, then `max_jobs_per_account`. A `fallback = true` account still comes after every other account.

`when_full = "queue"` skips admission checks for global and fixed runtime `max_jobs`. It still checks model allowlists, `max_jobs_by_room` and pool weekly budgets. The runner waits up to 15 minutes for busy account, pool, global or runtime slots (including `max_jobs_by_room`). It re-checks account eligibility, model allowlists, cooldowns, account caps and pool limits. It does not enforce `usage_ceiling` again. If no eligible account remains, such as when all accounts are cooling down, the job can end with `no_account` at once.

```toml
[codex]
plan = "plus"                       # the primary account: 4 jobs at once
usage_ceiling = 0.95

[[codex.accounts]]
name = "free-2"
home = "~/.cli-subagents/homes/codex/free-2"
plan = "free"                       # 1 job, effort up to high
fallback = true                     # only after the other accounts
models = ["gpt-6-luna"]             # the free plan's model
```

A refusal looks like this:

```
Codex is at its limit: 4 of 4 subagents running (plan preset plus: 4 per account; change with max_jobs). Start this with runtime claude, or wait for one to finish.
Only if the user explicitly asked for more parallel jobs, start again with override=<their words>; otherwise ask the user (say how much of the 5-hour window is left) or use the home model.
Room: Claude 2 accounts (no usage data) · Codex primary 8% left (5h, resets 03:12)
```

The `Room:` line is also in the `subagent_start` and `subagent_accounts` results. With quota steps or pools configured, it appears in the `csa run --follow` header too. It shows what is already known, marks old data with its age (`as of 2 h ago`) and leaves out accounts with no data. Codex usage is read from the newest rollout file in each Codex account folder (the `rate_limits` field only). Claude usage comes from the events of jobs that already ran. Antigravity shows job counts and cooldowns, with no remaining-quota estimate. Nothing starts a job to measure usage.

`csa accounts`, `subagent_accounts` and `csa doctor` list each account's cap, effort cap and model list, and where each one comes from. Where usage is known, they show each window's percentage used (`usage`) and when it resets (`usage_resets`), in local time with a countdown, for example `"five_hour": "2026-10-18 01:26 (in 4 h 15 min)"`. For Claude accounts they also suggest a plan (read from `oauthAccount.userRateLimitTier` and `organizationRateLimitTier` in the account's `.claude.json`, nothing else). A suggestion is not applied until you set `plan`.

### A user-authorized override

For one start, `override` holds the user's own words authorizing more parallel jobs, for example `"user asked for 6 Codex agents"`. Empty or whitespace-only values are rejected. Pass it as `override=...` in a relay prompt, the `override` field of `subagent_start`, or `--override "..."` on `csa run` and `csa start`:

```bash
csa run --runtime codex --override "user asked for 6 Codex agents" "Review the parser"
```

Never set override on your own judgment; use it only when the user asked for more jobs in this conversation, or confirmed after you warned them and told them how much of the 5-hour window is left. Otherwise follow the refusal's suggestion or use the home model. Use the reported usage and reset time; if the 5-hour window is unknown, say so.

Override bypasses csa's global, runtime and per-account job caps (including plan presets and `max_jobs_per_account`), `max_jobs_by_room`, `usage_ceiling`, pool `per_account`, `total` and `weekly_jobs`, and `when_full` queueing. Spend caps and explicit-only selection still apply, as do real quota or usage-limit cooldowns, including pool cooldowns. Account `models` and `max_effort`, login requirements, depth limits and access levels still apply too.

Override jobs count toward later starts and keep their normal quota records. The override text is stored in the job's spec and status, and shown by `csa jobs`, the `csa run --follow` header, `subagent_status` and `subagent_result`. It applies to that start only; continuing a finished job does not inherit it.

### Quota-scaled caps

`max_jobs_by_room` is optional under any runtime. Each step is `[percent_left, jobs]`, in descending order, ending at 0 percent. The first step with **more than** that percentage left sets the runtime cap. The job caps must be nonnegative integers and cannot increase as quota falls.

```toml
[codex]
max_jobs_by_room = [[75, 4], [50, 3], [25, 2], [5, 1], [0, 0]]
web_search = "live"
```

This example allows 4 jobs above 75 percent, 3 above 50, 2 above 25, 1 above 5, and none at 5 percent or less. An exact threshold uses the next step: 75 percent allows 3 jobs. csa uses each account's smaller remaining share across its open 5-hour and weekly windows, then takes the highest share among eligible regular accounts. Fallback accounts do not raise the cap. Windows that already reset are ignored. With no known usage it uses the top step. Antigravity reports no remaining quota, so it always uses the top step.

The lowest applicable cap wins. A lower cap refuses new starts, including with `when_full = "queue"`; jobs already running keep going. A refusal names the current percentage and cap:

```text
Codex has 28% of its quota left, so 2 jobs at a time (max_jobs_by_room); 2 are running. Run this on Claude, or wait for one to finish.
```

### Model pools

Optional `[[<runtime>.pools]]` tables split model limits and cooldowns within an account. The first pool whose `models` globs match the requested model wins. A job without a model uses `default_model`. A job matching no pool keeps the account-wide rules.

| Pool key | Meaning |
|---|---|
| `name` | Unique simple name within this runtime. |
| `models` | Nonempty list of model globs. Matching is case-sensitive. |
| `per_account` | Most running jobs from this pool on one account. |
| `total` | Most running jobs from this pool across all accounts. |
| `weekly_jobs` | Most jobs started from this pool per account in a rolling 7 days. |
| `cooldown_minutes` | Pool cooldown when an error gives no reset time or Retry-After. Falls back to the runtime's value. |

The four numeric pool keys are optional positive integers. Account, runtime and global caps still apply. For example:

```toml
[antigravity]
max_jobs = 6
max_jobs_per_account = 3

[[antigravity.pools]]
name = "argon"
models = ["gemini-4-argon-*"]
per_account = 1
total = 2
weekly_jobs = 3
cooldown_minutes = 10080

[[antigravity.pools]]
name = "flash"
models = ["gemini-3.8-flash-*"]
total = 6
```

A pooled usage-limit error cools down only that account's pool. Its reported reset time wins; otherwise csa uses Retry-After, then the pool or runtime cooldown. Other pools on the account stay available. Failover selects another account with a free pool slot and weekly budget, where failover is enabled.

Weekly counts come from job records. A job counts once on each account it started on, including failed and cancelled jobs and each account used during failover. Follow-up turns within the same job do not add starts. Refused jobs and jobs that never started do not count. Waiting jobs reserve budget until they start or finish, so a burst cannot overbook it. When `weekly_jobs` is configured, automatic cleanup keeps at least 7 days of job records even if `job_retention_days` is lower. Deleting those records by hand removes their budget history.

```text
Argon budget used up: 3 of 3 jobs this week on every account (weekly_jobs in pool argon). Use Opus instead.
```

The alternative names depend on which other installed runtimes have room. `csa accounts`, `subagent_accounts`, `csa doctor` and the `Room:` line show the current quota cap and each account's pools, for example `Argon: 1 running of 1, 2 of 3 this week, cooling down until ...`. Pool totals appear across accounts too. `csa accounts reset <name>` clears the account's pool cooldowns as well as its account-wide cooldown; it does not clear weekly counts.

### What goes where

- Defaults: one running worker per account with extra jobs queued, `usage_ceiling = 0.95` when usage is known, and `when_full = "refuse"` for explicit caps. Plans and extra caps are opt-in.
- Your `config.toml` holds your plans and caps: `plan`, `max_jobs`, `models`, `fallback`.
- Your agent instructions (`CLAUDE.md` and the like) hold preferences, such as which model to use for what kind of task.
- csa decides by itself: it counts running jobs, checks cooldowns and usage, picks an account, and lowers the effort when the account's plan needs it.

## Files

| Path | Contents |
|---|---|
| `config.toml` | The configuration above |
| `homes/<runtime>/<account>/` | Account folders (login, state, sessions, linked shared setup) |
| `homes/antigravity/primary/` | The folder Antigravity jobs on your primary account run in (its login stays in the OS keyring) |
| `homes/antigravity/.reviewer/<account>/` | The bare folder the Antigravity reviewer runs from |
| `jobs/<job_id>/` | `spec.json`, `status.json`, `events.jsonl`, `result.md`, `stderr-*.log`, `runner.log`, the message logs `inbox.jsonl` and `outbox.jsonl`, for Claude workers the `mcp.json` and `settings.json` they launch with, and for Antigravity workers the review gate's `review-context.json`, `review.jsonl` (every decision) and `review-cache.json` |
| `state/<runtime>-usage.json` | Last known usage, cooldowns and last use per account |
| `state/<runtime>-spend.json` | Durable per-attempt spend and running budget reservations; job retention does not remove this history |
| `state/leases/` | Lock files for per-account job slots |
| `cache/claude/<account>/` | Generated `settings.json` overlay and MCP config passed to workers |

Job records and generated files contain your prompts, results and MCP server settings. They stay on your machine and are created with owner-only permissions where the OS supports it.

## Environment variables

| Variable | Effect |
|---|---|
| `CLI_SUBAGENTS_HOME` | Use a different root folder (`csa --home <folder>` sets it for one command) |
| `CLI_SUBAGENTS_DEPTH` | Set by CLI Subagents in workers; used to refuse nested delegation |
| `CLAUDE_CONFIG_DIR` | Default for `[claude] primary` when the config doesn't set it |
| `CODEX_HOME` | Default for `[codex] primary` when the config doesn't set it |

Workers start from a cleaned environment. Variables that identify or authenticate the host session (`ANTHROPIC_*`, `CLAUDE*`, `CODEX_COMPANION_*` and similar) are removed, so a worker can only use its own account's login. Put environment settings you want workers to have into the `env` block of your primary `settings.json`; it is passed on, minus login-related keys.
