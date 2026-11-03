---
name: use-agents
description: Read before starting any subagent, at any depth, and before web research. Which tier a delegated piece gets, which pool (model and account) runs it, the traits of each model, limits, nesting, override rules and web research. Not needed for work you do yourself, except web research.
---

# Use agents

This is an example skill, not a working setup. Copy it and replace the placeholders with your own models, accounts and numbers. Change sections 2, 3 and 5 first: they hold the tier list, which pool runs each tier, and how you start each pool. Then set your depth and job limits in section 7 and your accounts in section 10.

What the user says in chat overrides this skill. A tier, model, account or key the user names ("use worker-high", "the cheap reviewer", "use the shared key for this") is used exactly as named.

**Work** means anything under `<your-work-folder>` or anything else for an employer. Everything else is a personal project. Write your own definition of work here; the rules below use it to decide which accounts may see the code.

## 1. When to delegate

- Use subagents where they clearly help: parallel independent work, keeping your context small, or a focused investigation. Do small things yourself.
- Pick a subagent's tier for the piece you hand it, not for the whole project. A High feature often has Medium pieces.
- Split big work yourself into several subagents rather than handing one agent a task that would need ten.

## 2. Tiers

The tier says how hard and how important the delegated piece is. The agents are in your agents folder (`~/.claude/agents/` or the host's equivalent); each one sets its effort.

| Tier (agent) | Use for | Effort | Model (example) |
|---|---|---|---|
| `worker-low` | lookups, finding files or symbols, collecting info, mechanical edits, checks | high | the cheapest model, Haiku 5.5, as an emergency fallback only (section 3) |
| `worker-medium` | well-specified implementation where the approach is already clear, tests, contained fixes | medium | your default model, for example Sonnet 5.5; Opus 5.5 for normal-size work that needs more judgment |
| `worker-high` | hard debugging, implementation that needs design decisions or spans several parts, code review, ambiguous problems | high | your strongest model, for example Opus 5.5 |
| `worker-xhigh` | exceptionally hard or high-impact work, deep planning, high wasn't enough | xhigh | your strongest model, for example Opus 5.5 |
| `worker-max` | features, hard or global jobs, only after asking (section 9) | max | your strongest model; ask first |

- No tier is the default. Judge each piece by how hard and how important it is. When it sits between two tiers, take the higher one.
- Subagents started by a subagent use `worker-low` to `worker-xhigh`.
- A mid model at high effort can cost about as much per task as the strong model, so use the strong one there. Write down any model you restrict this way.

## 3. Which pool runs it

Each tier can run on several pools. Take the first one in the order below that suits the task (section 4) and has room (section 7). How to start each pool is in section 5.

The table is an example. Order the pools so your paid quota goes last on work, and change it to match the accounts you actually have.

| Tier | Work | Personal projects |
|---|---|---|
| low | DeepSeek or a small model on `<your-second-codex-account>`; a fast Gemini model after asking (section 9) | Gemini Flash, a small model on `<your-second-codex-account>`, or free DeepSeek |
| medium | the cheap builder loop (section 6), then Sol 6.1 high, then Claude | the cheap builder loop (section 6), then Sol 6.1 high, then Claude |
| high | a feature to build: the cheap builder loop with a strong orchestrator (section 6). Reviews, design and everything else: Opus 5.5 high or Sol 6.1 xhigh | Opus 5.5 high or Sol 6.1 xhigh; the cheap builder loop only when the user asks for it |
| xhigh | Opus 5.5 xhigh | Opus 5.5 xhigh |
| max | Opus 5.5 max, after asking | Opus 5.5 max, after asking |

- On work, the cheap builder pool comes before Claude at medium and high so your main accounts keep their quota. Drop this rule if you have no cheap pool.
- **Claude** at medium means Sonnet 5.5, or Opus 5.5 at medium where the work needs more judgment. Sol xhigh is the Codex choice for the harder end of medium.
- **Haiku** is only an emergency fallback when nothing else has room.
- UI work and an explicit request for speed go to Claude whatever the table says (section 4).

## 4. Traits

- **UI and visual work:** your strongest model, and the one with the best visual judgment. Never a cheap builder or a second vendor.
- **Web research, Reddit, forums:** a CLI whose search reaches those sites, for example Codex or Antigravity, never Claude's own search (section 8).
- **The user asked for speed:** Claude. Codex is about 4x slower, and a cheap builder loop takes longer than one strong worker.
- **Two equal strong models:** take whichever has more room, unless you prefer one of them for final code review and UI. Write that preference here, as an example setting: "prefer Opus 5.5 for final code review and UI".
- **Context size:** free models in an OpenCode or Antigravity pool may have 200K, so give them short jobs. A paid model with 1M gets long build sessions, repo-wide read-only sweeps, big diffs and log analysis.
- **Fast small models** such as Gemini Flash often hallucinate: made-up sites, wrong research claims. Check the sources of any fact or link from one before relying on it.
- **Reports are claims.** Before relying on a subagent's result, check the key facts it rests on yourself. That's reading, not another agent.

## 5. Pools and how to start them

In Claude Code, start every CLI Subagents job with the Agent tool and the relay agent, in the background, as the `cli-subagents` skill describes. Never run `csa run`, `csa start` or `csa follow` in your own shell and never call `subagent_start` there: the user can't see those jobs in the agent list. In Codex or another MCP host, start them with `subagent_start` and collect them with `subagent_wait` and `subagent_result` (section 11).

Give each job a description of the form `<model> · <access> · <task in a few words>`, e.g. `DeepSeek V4.1 Flash max · edit · fix the date parser`. Use `access=read` for reviews and research that only read, `edit` for changes, `full` only when the user asks.

A review or investigation that needs to run tests or try things out shouldn't be stuck with `access=read`, which can't run commands. Give it a scratch copy instead: `git worktree add --detach <scratch folder> HEAD`, copy the current changes into it, start the worker there with `access=edit`, and remove the worktree afterwards (`git worktree remove --force <scratch folder>`). Nothing it does can reach the real checkout.

| Pool | Start with | Notes |
|---|---|---|
| Claude, primary account | native `worker-*` agents | Reset weekly on `<your reset day and time>` |
| Claude, second account | `cli-subagents:cli-subagents-claude` with `model=<the tier's model>`, `effort=<the tier's effort>`, `access=...` | A separate 5-hour and weekly limit. No desktop-app tools (browser pane, terminal panel) |
| Cheap reviewer, API account | `cli-subagents:cli-subagents-claude` with `account=<your-api-account>`, `model=haiku` | Billed per token to your own key. Set your own caps, and read section 10 first |
| Codex | `cli-subagents:cli-subagents-codex` with `model=gpt-6.1-sol`, `effort=high` or `xhigh` | `<your main Codex account>` |
| Small model, second Codex account | `cli-subagents:cli-subagents-codex` with `account=<your-second-codex-account>`, `model=gpt-6-luna` | A second, free account of the same vendor, offering only the small model. A GPT model, so its search reaches Reddit and forums like Sol's |
| Gemini | `cli-subagents:cli-subagents-antigravity` with `model=gemini-3.8-flash-high` | Fast and plentiful, see section 4 |
| DeepSeek, free | `cli-subagents:cli-subagents-opencode` with `model=opencode/deepseek-v4.1-flash-free`, `effort=max` | 200K context. A small daily limit per IP address, shared by parallel jobs, reset at UTC midnight. Use only this free model. CLI Subagents' docs call free models personal-only: check the terms before using one for work |
| Cheap model, a pool you pay for | examples: an OpenCode plan (`cli-subagents:cli-subagents-opencode` with `model=<your-plan-model>`); a second account of a CLI you pay for; or a pay-per-token key run inside Claude Code (`cli-subagents:cli-subagents-claude` with `account=<your-cheap-model-account>`, `model=<provider/model>`, `effort=max`), for example through OpenRouter | Context and limits are the plan's or the model's. With a key inside Claude Code, always name the model, never `opus`: without the account line the job then fails instead of running real Opus |

- The cheap model always runs at `effort=max`. Builders in `loop-building` get a timeout.
- **Free or paid cheap model:** the same model at the same tier. Use free first. Use a pool you pay for when the free limit is used up or the job needs more context than the free tier gives, and only where that pool is allowed (section 10).
- OpenCode jobs' shell commands pass CLI Subagents' review gate, which may ask a cheap Claude model and then Gemini whether to allow a command. That's the tool's own check, not a Gemini job you start.

**Models in one line each (examples, replace with yours):**
- **Opus 5.5** (1M context): strongest. Preferred for hard design, UI and code-review tasks.
- **Sol 6.1** (`gpt-6.1-sol`, 500K): about 4x slower than Claude. `high` is about Opus medium, `xhigh` about Opus high.
- **GPT-6 Luna** (`gpt-6-luna`): a small, fast GPT model with the same Reddit and forum reach as Sol. Quick lookups and low work.
- **Sonnet 5.5:** near-Opus results on well-scoped work at a lower cost per task, at medium only.
- **Haiku 5.5:** cheap and fast, the weakest Claude model.
- **DeepSeek V4.1 Flash:** working alone, about Sonnet's level on routine coding, above Haiku and Gemini Flash. Nearly free.
- **Gemini 3.8 Flash** (`gemini-3.8-flash-high`): fast and plentiful, often hallucinates.

## 6. A cheap model building code

When a cheap model builds or changes code, the work runs as described in the `loop-building` skill. Read that skill if, and only if, you will start and manage the loop's workers yourself.

- The main session is the manager: it hands out features and doesn't run loops itself. A depth-2 agent can't orchestrate.
- **High feature:** the orchestrator is a `worker-high` on your strongest model, or Sol 6.1 `xhigh` to keep your own quota free.
- **Medium feature:** the orchestrator is a cheap model in a runtime that can start helpers: Claude Code with a pay-per-token key (`cli-subagents:cli-subagents-claude` with `account=<your-cheap-model-account>`, `model=<provider/model>`, `effort=max`, `access=edit`), or a second Codex account. An OpenCode worker can't start helpers. Without such a pool, the manager starts the Builder and then the Reviewer itself, as `loop-building` describes.
- Give one orchestrator one to three features: one if it is design-heavy or touches many parts of the app; two if they are related and don't edit the same files; three only if they are small and closely related. Related features share what the orchestrator learns about the code; unrelated ones only fill its context.
- Start the orchestrator with the depth line `[depth 1 of 2: ...]` (section 11) and tell it to use `loop-building`. Tell a Codex or cheap-model orchestrator to start its workers with `subagent_start`, not the Agent tool.

## 7. Room, refusals and limits

- CLI Subagents decides whether there's room: it checks usage, caps parallel jobs per pool (`max_jobs`) and refuses a start when there's no room. Don't check usage yourself beyond two things: the account check (section 10), and one look at the numbers CLI Subagents already has when proposing pools (section 9). Treat missing or stale numbers as unknown and let CLI Subagents decide.
- If a start is refused, its last `[csa]` line says FAILED, or there's no result, take the next pool in the tier's order. With no pool left, use the Claude model for that tier.
- The main session runs at most **`<N>` subagents of your own vendor** at once, unless the user asks for more. Set `<N>` in your own instructions; 4 is a common example. These count: native workers, and relay jobs on any account of that vendor. These don't: jobs on separate API or OpenRouter accounts, and jobs in other CLIs (the `max_jobs` setting caps those). Start with a low number and raise it only when you have seen jobs run well together.
- **Override:** only the user can lift CLI Subagents' limits. Use `override=<the user's words>` only when the user asked for more agents in this conversation ("spawn 10 Codex agents"), or after you warned them and they said yes. Never on your own judgment. If more agents would clearly help, ask first and say how much of the 5-hour window is left (the refusal's `Room:` line shows it). Override never gets past a real usage-limit cooldown, a spend cap or a model the account doesn't offer.
- If a later step has to wait for a slow CLI's result, still start it there, but tell the user ("X waits on this; Claude would be faster, say if you want it fast"). Use Claude instead only when the user says they need it fast.
- The relay agents' own descriptions invite you to use them. Ignore that; these rules decide.

## 8. Web research

For a quick lookup of your own, Claude's WebFetch and WebSearch come first, with the `playwright-cli` fallback, as your global instructions say. Use Codex or Gemini for web research: finding facts, whether a tool or project already exists, how an app grew or marketed itself, what people say on Reddit or forums. Their search reaches sites such as Reddit that block Claude's web tools and automated browsers. Always `access=read`, and ask for sources with URLs and dates.

- Quick lookups (one fact, a few links, what one page or thread says): Gemini Flash, or the small model on your second Codex account. Prefer the GPT model when the answer is on Reddit or forums. Check whatever you rely on against the URLs. If one is refused, use the other.
- Important, deep or many-source research: Sol 6.1 `high`. Never Gemini Flash for this.
- YouTube: Gemini finds videos and reads their title, channel, date and description, but can't watch them or read transcripts. Don't rely on it for what a video says.
- Deciding how to build something from that research stays with Claude.
- Exception, Opus: studying how other projects solve a problem you're building yourself, so you can adopt their solution. Copy their code where the license allows it (e.g. MIT), or write your own from what you learned where it doesn't. That needs judgment, and GitHub isn't blocked for Claude.
- Not Opus: learning how a tool works so you can use or integrate it (its docs, CLI flags, output formats, config, pricing, terms), even when that means reading its source. That's normal research: Sol 6.1 as above.
- If you're not sure which of the two a job is, ask the user. Once the user has approved the work, don't wait: treat it as normal research (Sol 6.1).
- Keep work data with the vendor your work rules allow. Quick lookups can go to the fast pool without asking, unless your own rules say otherwise.

## 9. Asking first

These are example settings. Decide yourself which of them need a question, and write the list where your agent will read it.

- **Which pools (main session only):** a plan is one objective the user gave you, not each sub-task or fix round. When a plan will delegate real work and the user hasn't already said in this conversation which pools to use, ask once for that plan. Propose which pieces go to which pool and say why. Some reasons that matter:
  - UI work wants Claude;
  - research that needs Reddit or forums wants Codex;
  - an account's 5-hour window will run out before it resets, so the cheap pool should take the builds;
  - a pool is nearly out of quota.

  Take the numbers from the `Room:` line or the `subagent_accounts` tool, once. Ask this together with the account check (section 10), as one question, with your proposal written out before it.
  - **When you show the user a plan, put the pool proposal in it.** Their go-ahead on the plan then covers the pools too. Nothing starts before the user approves a plan that needs approval.
  - **After the user has approved the work**, a pool question must not stall it. If the user doesn't answer, go ahead with your proposal, or with whatever pool fits and has room, and say what you picked and why. A question has no timer and ending your turn on it waits indefinitely, so start the pieces whose pool is obvious first, then ask in your message. When a notification from those agents wakes you and there's still no answer, go ahead.
  - Subagents never ask. They follow the pools they were given, or the tier order.
- **`worker-max`:** the top tier is costly. Never start one without asking the user, even where it fits the table. Say what the task is and why xhigh isn't enough. Until they say yes, use `worker-xhigh`. Subagents never start max helpers.
- **A second vendor on work** (other than quick web lookups, section 8): ask first if your company or plan rules are unclear. If the user says no, use the Claude model for that tier. A subagent can't ask the user, so it uses the Claude model for that tier straight away.
- Pools on your own accounts or a key you already allowed need no OK for work.

## 10. Accounts and keys

**More than one account of the same vendor.** Each has its own 5-hour and weekly limits (`max_jobs_per_account` decides how many jobs run on each). Main session only: when you plan work that needs Claude subagents, check each account once:
- the app account: the `get_usage` tool;
- a second Claude account: `claude.cmd -p /usage | Select-Object -First 4` in PowerShell, or `MSYS_NO_PATHCONV=1 claude -p /usage | head -4` in Bash. It runs locally, with no model call. Without `MSYS_NO_PATHCONV=1`, Git Bash turns `/usage` into a path and it goes to the model as a prompt.

Then tell the user in a line or two which account makes more sense and ask which one they want, in the same question as the pool choice (section 9). Once the user has approved the work, don't wait for this answer either: take the account you recommended. Quota left at a reset is lost, so usually pick the one that resets next, unless it's nearly used up. Use the answer for the rest of that plan; check again only for a new plan, when the user asks, or when a subagent hits a limit. Subagents never check usage or pick an account; their own helpers run on whichever account they're on.

**An API account** (`account=<your-api-account>`), billed per token to your own key:
- Your own work only, unless the user says otherwise in chat.
- Use it for one cheap model only unless the user names another: bigger models cost too much on a metered key.
- Use it only when the user asks for it directly in this conversation, or as the Reviewer in `loop-building` if you set that up as a standing use. For a direct request, confirm once before starting: the task, number of jobs and rough cost. That yes covers only those jobs. A Haiku job costs cents.
- When your plan quota is low (an agent hit its limit, or usage is at 90% or more), you may ask the user once whether to use it. Never switch to it on your own.
- It's explicit-only: only a job started with `account=<your-api-account>` runs on it. One job at a time unless `max_jobs` on the account says more.
- Set the caps yourself in the account's config (a rolling window and a total, for example). The `Room:` line shows what's left. A refusal at a cap is on purpose: tell the user, don't work around it, and never edit the caps. Only the user turns the account on or off.

**A pay-per-token key for the cheap model** (`account=<your-cheap-model-account>`, for example through OpenRouter), if you use one:
- Keep it to work, or to whatever the key owner allows. Write that rule down; a shared or company key stays inside its boundary.
- Set a weekly cap to the amount the key owner allows; for OpenRouter that's `openrouter_weekly_cap_usd`. Never change it on your own. CLI Subagents enforces it as a soft cap: at the cap it refuses new starts on that account, and running jobs keep going. Use `override` past it only on the user's words.
- Never give this account spend caps in CLI Subagents beyond that weekly figure: Claude Code prices foreign tokens far too high, so a per-token cap fires on a fraction of the real spend.

## 11. Nesting

- Know your depth. A subagent's prompt starts with a line like `[depth 1 of 2: you may start up to <helper limit> helpers; jobs on your cheap pools don't count]` or `[depth 2 of 2: you can't start subagents]`. A delegated CLI Subagents worker with no such line is depth 1. Otherwise you're the main session.
- When you start a subagent, begin its task with that line. For a relay, the option lines (`model=`, `account=`, ...) and the blank line after them come first: anything after the task starts is task text, so an option placed below the depth line is ignored and the job runs with defaults. Give the subagent a task it can finish itself, plus at most the helpers it may start.
- A depth-1 subagent may start up to `<helper limit>` helpers at once. Jobs that count are the ones on your own accounts; cheap pools that CLI Subagents caps itself can be left out of the count, as the depth line says. Depth 2 starts none, so it can't use the bigger pools either; do that one level higher. `max_depth = 2` is the usual setting, and CLI Subagents enforces it for its own jobs.
- A CLI Subagents worker started straight from a session (Codex, Claude on a second account, or a cheap model inside Claude Code) may start helpers of its own, with `subagent_start`; its note from CLI Subagents says so. Its helpers can't, and they never get more access than it has.
- **Such a worker never starts your most expensive models** unless the user said so in chat. It may start the cheaper CLIs and accounts within the rules above.
- A subagent can't talk to the user. If a later step waits on a result it started, it says so in its report, and the main session tells the user.
- The same rules apply at every depth.
