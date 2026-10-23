# Account spend caps

Research date: 2026-10-23. Sources are Anthropic documentation and its public SDK source.

## Claude Code's run budget

The [CLI reference](https://code.claude.com/docs/en/cli-reference) documents `--max-budget-usd` for print mode (`-p`) and `stream-json` as a print output format. Anthropic's [Python SDK transport](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/transport/subprocess_cli.py) builds a command with `--output-format stream-json --verbose` and appends `--max-budget-usd` when configured, confirming that combination in its own implementation. The SDK repository has an [MIT license](https://github.com/anthropics/claude-agent-sdk-python/blob/main/LICENSE); no code was copied.

For example, the existing csa print invocation can include:

```text
claude -p --output-format stream-json --verbose --max-budget-usd 300.00
```

The [Python result schema](https://code.claude.com/docs/en/agent-sdk/python#resultmessage) identifies a budget stop as `type = "result"`, `subtype = "error_max_budget_usd"`, with `is_error = true`. Read the cost on error results too. csa maps this subtype to `budget` and stops without retry or failover.

The [agent loop guide](https://code.claude.com/docs/en/agent-sdk/agent-loop#budget-headroom) explains the enforcement boundary: Claude checks estimated spend after receiving a model response. That response finishes and counts toward cost even if it crosses the cap. Running native subagents can add spend before they stop. The excess can therefore include one response plus those subagents' remaining work; the flag does not promise an exact dollar ceiling. Budget enforcement over native subagents requires Claude Code 2.1.217 or newer. Set limits with headroom.

## Cost accounting

Claude's [cost-tracking guide](https://code.claude.com/docs/en/agent-sdk/cost-tracking) calls `total_cost_usd` a client-side estimate. Assistant events repeat usage across content blocks sharing `message.id`; deduplicate them. Their output-token counts are incomplete, and they omit native subagent costs. A crash can emit `error_during_execution` with all cost fields zeroed. A budget result's `usage` excludes the crossing response, while its `total_cost_usd` includes it.

Since Claude Code 2.1.277, resumed results can include earlier session spend; summing attempts can overcount. The native budget counts the current invocation only. csa retains reported attempt totals conservatively and charges a capped attempt's full reservation when a reliable cost is missing, without estimating prices from partial tokens.

Claude normally reports list prices; managed `modelPricing` can substitute contracted rates. The [Claude cost guide](https://code.claude.com/docs/en/costs#report-spend-at-your-contracted-rates) distinguishes those estimates from the invoice. On 2026-10-21 the owner observed about $2,380 in Claude Code estimates for about 40 Opus jobs, while the Anthropic Console showed about $430 to $470. This observation motivated the conservative cap; the difference was not reconciled, and does not establish a billing error or its cause.

## Anthropic Console controls

Set a Console spend limit as the authoritative billing-side control as well as the local csa cap. It covers the organization or workspace across clients and machines, while csa sees only its own ledger.

The [API limits documentation](https://platform.claude.com/docs/en/api/rate-limits#spend-limits) describes calendar-month organization caps. In Console **Settings > Billing > Spend limits**, use **Adjust limit** or **Set limit** to set a lower limit. Tier caps reset at 00:00 UTC on the first of the next month. A reached tier cap returns HTTP 429 with `enforced_spend_limit_reached`; configured organization or workspace limits normally return HTTP 400. The Claude Code workspace can instead return 429 with `retry-after`. Raising or removing a configured limit restores access sooner. The older URLs [docs.claude.com](https://docs.claude.com/en/api/rate-limits) and [docs.anthropic.com](https://docs.anthropic.com/en/api/rate-limits) redirect to this documentation.

For a workspace, open **Settings > Workspaces**, select it, then **Spend limits > Monthly limit > Change limit**. Workspace limits must be lower than the organization limit; unset ones inherit it. The default workspace cannot have a separate spend limit. Email thresholds only send notifications. See the [workspace help page](https://support.claude.com/en/articles/9796807-create-and-manage-workspaces-in-the-claude-console) and [platform workspace guide](https://platform.claude.com/docs/en/manage-claude/workspaces#set-workspace-limits). For an `apiKeyHelper`, choose the workspace that owns the returned key; do not assume it uses the special Claude Code workspace created by Console sign-in.

Console limits should not be described as a guaranteed final invoice ceiling. The API limits page does not give a universal maximum overshoot. Anthropic's [Team/seat-based Enterprise help page](https://support.claude.com/en/articles/12005970-manage-usage-credits-for-team-and-seat-based-enterprise-plans) explicitly allows one-request overage because checks precede final token accounting; that page concerns subscription usage credits. Separately, [Claude Platform on AWS](https://platform.claude.com/docs/en/build-with-claude/claude-platform-on-aws#spend-limits) documents about two hours of metering delay and billed overshoot. That delay is specific to that AWS product and is not evidence of the same delay for a direct Anthropic API key.

## csa design

An account's `explicit_only = true` excludes it from automatic selection and failover. A pinned start still requires the account to be enabled and pass its limits. If all extra accounts are disabled or explicit-only, an unpinned start falls back to the primary even with `include_primary = false`.

`spend_caps` sets simultaneous per-account caps. Each entry has `amount_usd` and `window`; `$300 / 24h` and `$500 / 7d` enforce the previous 24 hours and previous 7 days together. A rolling window counts attempts settled in `(now - duration, now]`: the old boundary is excluded and the current time included. A durable ledger retains settled costs independently of job retention.

The older scalar `spend_cap_usd` and `spend_period` form remains supported for one cap; combining it with `spend_caps` is rejected. Its period defaults to `month`, with `day`, `week` and `total` also supported. Calendar boundaries use UTC, with Monday starting the week. In either form, optional `spend_since` sets a later accounting start using a quoted UTC date or datetime. A future start refuses jobs until counting begins.

Admission checks every cap, and execution rechecks and reserves under the ledger's lock. Each cap's available amount is its limit minus settled spend in its window and all active reservations. Any window with less than $1 available refuses the start and is named in the message. The attempt receives the smallest available remainder and reserves that amount against all caps until settlement, so parallel jobs cannot each receive it. All active reservations count as windows advance and across changes to `spend_since`. Neither queueing nor `override` bypasses these checks. Only Claude workers enforce spend caps initially. Direct vendor CLI calls and interactive sessions are outside this local control. Account listings, doctor and `Room:` show all caps.

For the existing `work-api` account entry, add:

```toml
explicit_only = true
spend_caps = [
  { amount_usd = 300, window = "24h" },
  { amount_usd = 500, window = "7d" },
]
# spend_since = "2026-10-23"  # optional: exclude costs settled before this UTC date
```

These keys do not change the account's enabled state. Keep `enabled` as the owner configured it; adding a cap does not authorize using the account.
