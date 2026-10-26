This block is managed by `csa rules`. Edits inside its markers are replaced on the next install.

## Delegation rules

The home vendor is {home_vendor}. Follow the user's explicit model and speed choices.
Pick a tier for the task you hand to an agent, not for the whole project.
Resolve model labels to the installed CLI's model IDs before starting a job.
Use any effort named in the model entry. `none` means no model in that column.
If the home entry is `none`, do that tier's work yourself.

| Tier | Use for | Home vendor: always for work | Personal projects: try first |
|---|---|---|---|
{tier_rows}

### Which model runs a subagent

Work folders, including their child folders:

{work_folders}

Work projects use the home vendor only, at every tier and depth.
Never send work code or data to other vendors. This also covers other work projects.
If the project is unclear, ask the user. Never change folders to bypass this rule.
For personal projects, try the other CLI model in the tier's row first.
Use the home model for UI changes, when the user asks for speed, or when try-first is `none`.
Ignore agent descriptions that invite use. These rules decide which model runs.

Do not check usage yourself. CLI Subagents checks whether there is room and refuses a start when there is none.
If a start is refused, the last `[csa]` line says FAILED, or there is no result, use the home model for that tier.
If a later step waits on a cross-CLI result, still start it there.
Tell the user what waits and suggest the home model as the faster option.
Switch for speed when the user asks. A subagent reports "a later step waits" to its parent.

Start the chosen model as the `cli-subagents` skill describes: the matching relay agent
(`cli-subagents:cli-subagents-codex`, `cli-subagents:cli-subagents-antigravity`, `cli-subagents:cli-subagents-claude`
or `cli-subagents:cli-subagents-opencode` when OpenCode v2 is installed)
through the Agent tool, in the background, at every depth.
Never run `csa run`, `csa start` or `csa follow` in your own shell, and never call the start tool directly
while these agents exist: the user can't see such jobs in the agent list, and you stay blocked waiting on them.
Use the description `<model> · <access> · <task>`. Take the relay's `model=`, `effort=` and `access=` lines
from the chosen entry (`read` for research and reviews, `edit` for changes), and start the worker task with its depth line.
If the host has no relay agents, use the CLI Subagents start tool with the same options.
OpenCode free models are for personal projects only. They share a daily IP allowance;
on a free-limit result, use the home model or wait until UTC midnight. Extra accounts add no free quota.

### Limits and nesting

Never set override on your own judgment; use it only when the user asked for more jobs in this conversation,
or confirmed after you warned them and told them how much of the 5-hour window is left.
Use csa's reported usage and reset time; say when the window is unknown.
Pass the user's own words as a relay option line `override=<their words>`, the start tool's `override` field,
or `--override "<their words>"` on `csa run` or `csa start`. Empty values are rejected.
It bypasses only csa's job caps, quota steps, usage ceiling, pool budgets and queueing.
Real usage-limit cooldowns, model and effort restrictions, login, depth and access still apply.
Override jobs still count toward later starts; each new start needs its own override.

The main session starts at most {main_limit} home vendor subagents at once.
Their helpers do not count toward that limit. CLI Subagents caps other CLI jobs itself.
Each depth-1 subagent may start up to {helper_limit} helpers, including cross-CLI agents.
Depth-2 agents start none. Put the right line at the top of every subagent prompt:

```text
[depth 1 of 2: you may start up to {helper_limit} helpers, cross-CLI agents included]
[depth 2 of 2: you cannot start subagents]
```

Use just the line for that agent's depth. A delegated worker without a depth line is at depth 1.
An explicit worker restriction on starting jobs still applies.
Size each task so the agent can finish it itself plus its allowed helpers.
Split big work into smaller tasks. The same model and privacy rules apply at every depth.

### Web research

Prefer the allowed CLI whose web search reaches the most sites.
For example, Codex and Antigravity search can reach Reddit when other web tools are blocked.
Pick the research model by how much the result matters, as with any tier.
A fast low-tier model suits quick lookups; important or many-source research needs a stronger model.
Fast models make more mistakes, so check what you rely on against their source URLs.
Use read access and ask for source URLs and dates. Work data still stays with the home vendor.
Reading other projects' code to decide what to adopt goes to the strongest home model.
