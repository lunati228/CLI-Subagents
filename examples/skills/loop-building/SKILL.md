---
name: loop-building
description: Only for the agent that orchestrates a cheap builder pool after the use-agents skill routed a piece of code work to it. Runs Investigator, Builder and Reviewer workers through CLI Subagents. Not for choosing models, tiers or accounts, and not for general delegation; use-agents covers those.
---

# Loop building

This is an example skill, not a working setup. Copy it and fill in the two cheap pools and their limits first (the table below), then the Reviewer you want. The loop itself, the brief, the preamble and the role cards work as written.

Cheap workers do the repository work; you keep the decisions.

## Your job as orchestrator

- You are the orchestrator the manager gave one to three related features (use-agents, section 6): a strong worker for a high feature, or a cheap model that can start helpers for a medium one. You split the work, write the briefs, start the workers, make the decisions your task allows, and stop or escalate.
- How you start workers: a strong Claude orchestrator through the relay agents (Agent tool); a Codex orchestrator, or a cheap model running inside Claude Code, with `subagent_start`, then `subagent_wait` and `subagent_result`, never the Agent tool. Inside a cheap model's account the relay agents run on that cheap model too and may skip the relay steps. A worker in another CLI never starts your most expensive models unless the user said so.
- Where no cheap orchestrator is available, a medium task has none: the manager runs steps 3 to 7 itself, with the same briefs and cards, and reads only the reports.
- Delegate the building, don't read code a worker can read for you, and don't re-check work whose checks and review passed. A strong orchestrator's tokens cost many times a worker's. Workers make the engineering choices inside a brief.
- Avoid routine log reading and repeating a passed review for reassurance. When a failure has no clear cause, look at the specific evidence you need.
- A unit too hard for the cheap builder, marked at the split or given up on in step 7, goes to a strong model from another vendor at the feature's tier, working alone (for example Sol high for a medium feature, Sol xhigh for a high one); that spares your main seats. If it has no room, a Claude orchestrator starts a fresh worker at the feature's tier, and a cheap orchestrator hands the unit back to the manager. A unit harder than its feature's tier goes back to the manager with the Investigator's notes: the feature was given the wrong tier.
- Several features: run them side by side. If one waits for a decision, keep going with the others. If it gets too much to keep track of, finish what is running, report, and give the rest back to the manager.
- There is no automatic strong-model review in the loop. If a concrete risk is still open after the loop, say so in your report, or get a focused review under use-agents.
- Before the first Builder, do any research your rules require for this work (a prior-art or research step in your instructions) and put its findings in the brief. Workers can't do that research themselves.

## Cheap pools

The same model runs in two pools. Builders run at `effort=high`: at max, each step can spend minutes thinking. Every other role runs at `effort=max`.

Start every Builder with a timeout, for example `timeout=2400` (40 minutes; `timeout_seconds` with `subagent_start`). A unit is small, so a Builder still running then is stuck; treat the timeout as an interrupted Builder (Rules).

The same cheap model often runs in more than one pool. The rows below are examples; keep the ones you have.

| Pool | Start with | Context | Limits |
|---|---|---|---|
| Free | `cli-subagents:cli-subagents-opencode` with `model=opencode/deepseek-v4.1-flash-free`, `effort=max` | 200K | Small daily limit per IP address, shared by all parallel jobs, reset at UTC midnight |
| A subscription, for example an OpenCode plan | `cli-subagents:cli-subagents-opencode` with `model=<your-plan-model>` (for example `opencode-go/deepseek-v4.1-flash`), `effort=max` | the plan's | the plan's usage windows |
| A second account of a CLI you already pay for | the matching relay agent with `account=<your-second-account>` | that model's | that account's own limits |
| A pay-per-token key run inside Claude Code, for example through OpenRouter | `cli-subagents:cli-subagents-claude` with `account=<your-cheap-model-account>`, `model=<provider/model>`, `effort=max` | the model's full context (DeepSeek V4.1 Flash has 1M) | billed per token: set a weekly cap (`openrouter_weekly_cap_usd` for OpenRouter) and `max_jobs`, for example 8 |

Only Claude and Codex workers can start helpers. An OpenCode worker builds fine, but a cheap orchestrator must run in Claude Code (the last row) or Codex.

Start each worker on the free tier. Move it to a pool you pay for when one of these holds and the work is allowed there (or the user allowed it):
- it needs more context than the free tier gives: a long investigation, a repo-wide sweep, a long build session, a big diff;
- the free daily limit is used up.

## Who does what

| Role | Work that may use a paid pool | Everything else |
|---|---|---|
| Investigator | the cheap model, free or paid by the rule above, `access=edit` in a scratch copy | the cheap model, free, `access=edit` in a scratch copy |
| Builder | the cheap model, free or paid by the rule above, `access=edit` in the real checkout, or in its own worktree when units build in parallel | the cheap model, free, `access=edit` in the real checkout, or in its own worktree when units build in parallel |
| Reviewer | whichever cheap reviewer you set up, here a small fast model on `account=<your-api-account>`: `model=haiku`, `effort=max`, `access=edit` in a scratch copy | a fresh cheap-model session, free, `access=edit` in a scratch copy |

Pools you pay for, and the API account, are for whatever you allowed them for. Keep a shared or company key inside the boundary its owner set. Elsewhere use them only when the user says so.

**Optional second Reviewer.** A bigger unit (several files, or more than about 200 lines) may also get a second, independent Reviewer from a different pool, for example a fresh small model on `<your-second-codex-account>` (`account=<your-second-codex-account>`, `model=gpt-6-luna`, `effort=high`), in its own scratch copy, next to the first. Its free quota is small: if it is refused or fails, skip it and go on with the first Reviewer's verdict. Never start it on an account that costs money you haven't allowed. A concrete defect from either Reviewer starts a fix round.

**A metered Reviewer.** If you put the Reviewer on an API key, it is a standing use: no confirmation per job, but the account's caps and rules in use-agents, section 10, still apply. Start it with `cli-subagents:cli-subagents-claude` (or `subagent_start` with runtime `claude`) and the option lines in the table, with a description like `Haiku 5.5 max · edit · review the CSV export`. Check the model's price per million tokens and keep its input small (see "The brief"), because a long prompt can cost several times a short one. An API account usually runs one job at a time.

## Scratch copies for the Investigator and Reviewer

Both need to run tests and try things out, and `access=read` can't run commands. So they work in a throwaway git worktree with `access=edit`, where nothing they do reaches the real checkout. Make a new one for every Investigator and Reviewer run, in a folder outside the repository (for example the system temp folder). In Git Bash, from the real checkout:

```bash
git worktree add --detach "$SCRATCH" HEAD
git diff HEAD --binary > "$SCRATCH.patch"                    # the Builder's changes so far
[ -s "$SCRATCH.patch" ] && git -C "$SCRATCH" apply "$SCRATCH.patch"
git ls-files --others --exclude-standard -z | tar --null -cf - -T - | tar -xf - -C "$SCRATCH"   # new files
git -C "$SCRATCH" add -A                                     # so git diff HEAD and git stash see new files too
```

Start the worker with `cwd` set to the scratch folder. Afterwards: `git worktree remove --force "$SCRATCH"` and delete the patch file.

Dependencies that live outside git (`node_modules`, a virtualenv, build output) aren't in the scratch copy. Point the worker at them in the brief (the virtualenv's interpreter, for example), or link them in. Where that isn't practical, fall back: give the worker `access=read` in the real checkout, run the brief's checks there yourself, and put their output in its brief.

## Running work in parallel

Run independent work at the same time: it finishes sooner and costs no more.

- **Start first, wait after.** Start every independent unit's worker before you wait for any of them.
- **Investigators.** When a piece has several separate aspects to check, start one Investigator per aspect, each with a narrow question and its own scratch copy. They never touch the real checkout, so they can all run together.
- **Builders.** Units can build at the same time when their Files don't overlap and none needs another's result. Each gets its own worktree (below). Units that share a file, or where one needs another's result, run one after the other.
- **Reviewers.** Each unit gets its fresh Reviewer as soon as its Builder reports. Make its scratch copy from the unit's worktree, running the scratch-copy commands inside it.
- **Landing.** Bring passed units into the real checkout one at a time, then run step 9 on the combined change.
- **How many at once.** Respect the depth line you were started with (use-agents, section 11: `<helper limit>` helpers for a depth-1 orchestrator, counting the ones on your own accounts) and the pool table's `max_jobs` for the rest.

A Builder's worktree starts from the real checkout's current state, uncommitted work included. In Git Bash, from the real checkout, with `$WT` a new folder outside the repository:

```bash
git worktree add --detach "$WT" HEAD
git diff HEAD --binary > "$WT.patch"
[ -s "$WT.patch" ] && git -C "$WT" apply "$WT.patch"
git ls-files --others --exclude-standard -z | tar --null -cf - -T - | tar -xf - -C "$WT"
git -C "$WT" add -A && git -C "$WT" write-tree > "$WT.base"   # the starting point, so the unit's own change can be told apart
```

Start the Builder with `cwd` set to `$WT`; its fix rounds continue there. Once the unit passes, land it from the real checkout:

```bash
git -C "$WT" add -A
git -C "$WT" diff-index --cached --binary -p "$(cat "$WT.base")" > "$WT.unit.patch"
git apply "$WT.unit.patch"
git worktree remove --force "$WT" && rm -f "$WT.patch" "$WT.base" "$WT.unit.patch"
```

`git apply` refuses rather than overwrite when a file changed since the unit started, and then leaves the checkout as it was. That means the units weren't independent: don't force it. Give the unit to a Builder in the real checkout with its patch and the error.

## The loop

1. **Investigator.** First, in a scratch copy, unless the brief already names the files, the entry point and a check that shows the problem; then go straight to step 3. Maps the area in the repository, runs the existing tests to see where things stand, and suggests how to split the work, which files each part touches and which checks prove each part. For a piece with several separate aspects, run one Investigator per aspect in parallel.
2. **Split.** One unit if the piece is small. Otherwise several units, each one reviewable on its own with checks that prove it. Each unit is one change: its details can be a bullet list, but separate changes become separate units. Mark units too hard for the loop; they go to a stronger model (see "Your job as orchestrator"). Note which units are independent and can build in parallel.
3. **Builder.** A fresh Builder per unit gets the brief and builds it in the real checkout, or in its own worktree when units build in parallel. It fixes its own failing checks before it reports.
4. **Scratch copy.** When the Builder reports, make a fresh scratch copy of the checkout with its changes.
5. **Reviewer.** A fresh Reviewer in task-review mode works in that copy: it gets the brief and the Builder's report, runs the checks itself and tries what it needs to.
6. **Fix.** If the Reviewer finds defects, send its findings to the same Builder. In Claude Code, SendMessage to the Builder's finished relay continues its session as a new job; from another host, `subagent_message` to the Builder's finished job does the same. Start a fresh Builder instead, with the brief, the current diff and the findings, when that follow-up is refused or the old session has gone stale: it has been through two fix rounds, its reports mix up old and new failures, or it keeps repeating a fix that doesn't work. Then go back to step 4 with a new scratch copy and a new, fresh Reviewer.
7. **Repeat** steps 4 to 6 until the checks pass and a fresh Reviewer passes. Stop and escalate instead when the loop stops making progress:
   - the same defect comes back after a fix;
   - two rounds in a row don't reduce the defects;
   - three fix rounds have run.

   Then the unit goes to a stronger model (see "Your job as orchestrator"), with the brief, the current diff and the open findings.
8. **Next units.** Start a unit once the units it needs have landed. Independent units run steps 3 to 7 at the same time, each in its own worktree; in any one checkout there's one writer at a time.
9. **Combined check.** A fresh Reviewer runs, in a scratch copy of the whole change, every unit's checks plus the full test suite; with one unit, that is the unit's checks plus the full suite. It reviews the whole change in phase-review mode only when units connect to each other (one calls, imports or configures another) or the combined checks fail. It covers only this loop's work; pieces built outside the loop (UI done by Claude, for example) are not part of it. If the combined diff is too big for one review, review it in parts by area. Defects it finds go back to the Builder that owns that unit, through steps 4 to 7, and end with a new combined check.
10. **Report** to whoever started you: what changed, the check results, what the Reviewers found and fixed, and anything still open.

## The brief

Each worker prompt is, in this order:
1. the relay option lines and a blank line (CLI Subagents reads options only before the task starts);
2. the depth line `[depth 2 of 2: you can't start subagents]`;
3. the worker preamble;
4. one role card;
5. the brief.

The brief has:
- **Request:** the user's own words for this feature, unchanged. The Builder and the Reviewer both get it.
- **Goal:** one objective, stated as the result wanted.
- **Context:** files, symbols, entry points, the Investigator's notes and any research findings. Point the way; don't predict the diff.
- **Files:** the files and folders the unit may change. Units that build in parallel must not share any.
- **Checks:** the commands that prove it works and the behaviour expected, including a failure case where it matters. Name only the tests this unit concerns; the full suite runs in step 9.
- **Limits:** files or areas not to touch, and mistakes to avoid (a new dependency, a public API change), only when they really apply.

Point a metered Reviewer at the diff and the files it needs, and tell it to read only those, so its prompt stays short.

## Worker preamble

Paste this after the depth line in every worker prompt.

```text
You are one worker in a build loop. Work only on this task and in this checkout.
- Don't start subagents of any kind: no Agent tool, no cli-subagents tools, no `csa` command.
- Never weaken, skip or delete tests or checks to get a pass.
- Keep facts, guesses and unknowns apart. Never claim a command, test or result you didn't actually get.
- Before you report a change, check it in the files. An edit you meant to make is not an edit you made.
- Make routine engineering choices yourself. If you hit a product, architecture or security decision the brief doesn't cover, stop and reply DECISION_REQUIRED with the question, the options and your recommendation.
- Never install packages, commit, push, reset, check out, run `rm -rf`, fetch from the network, read `.env` files or edit lockfiles. Outside a scratch copy, never `git stash` or `git clean`. If you need one of these, say so in your report. If the same command is denied twice, stop trying variants.
- Leave nothing running that could change files after you finish.
- Report a failing check as a digest: at most 3 distinct failures, each with the expected and received values and the first line in this repository's own code (not in a library). Say whether the environment failed (a missing tool, "command not found") or the code did. Edits don't fix the environment, so don't retry those.
- Report in at most 25 lines: first a status (DONE, PARTIAL or BLOCKED); conclusion; what you changed or found, with file:line; checks you ran and their results; open issues.
```

## Role cards

Paste the one card that fits, after the preamble.

**Investigator**
```text
Role: Investigator. You work in a scratch copy that is thrown away afterwards: run the existing tests and small experiments as you need, but don't build the feature. Map what the brief asks about: which files and symbols own the behaviour, how data flows, the existing tests and whether they pass now, what's already done and what's missing. Mark what you saw and what you only infer. End with these lines:
Files: path:start-end, most relevant first
Commands run: each command and what it showed
Unknowns: what you couldn't settle
Units: a proposed split into units that can each be reviewed and tested on their own, each with its files and the commands that would prove it works
```

**Builder**
```text
Role: Builder. You get either a task or a list of review findings.
- A task: deliver this one unit through the project's existing architecture and mechanisms. Find the files you need yourself. Don't take on a second unit.
- Findings: fix each one at its cause with the smallest complete repair. No patch shaped only to pass the test, and nothing unrelated.
- Follow the brief. When the user's request names a specific approach, use that one. Name every point where the brief and the request disagree in your report.
- For a bug fix, first write a test that reproduces the bug and see it fail. It asserts exact values, not just "doesn't crash".
After your last change, run the checks from the brief. If one fails, fix the cause and rerun, up to 3 tries; stop early if the same failure comes back. Report their real output. You don't approve your own work; a fresh Reviewer follows.
```

**Reviewer** (the brief names the mode)
```text
Role: Reviewer. You work in a scratch copy of the change that is thrown away afterwards: run the checks and any experiments you need, but don't fix anything, since edits here don't survive. Assume the change is wrong until the evidence shows otherwise; the Builder's report is a claim, not proof. (`git diff HEAD` in this copy shows the change, new files included.)
- Task review: does this diff meet the brief and the user's request? Read the real code paths and run the checks. Picture a plausible wrong implementation and ask whether these checks would catch it; where it's cheap, try that case. For a bug fix, stash the non-test changes (`git stash push -- <those files>`), rerun the new test and expect it to fail, then `git stash pop`; a test that passes without the fix doesn't test the bug. When you find a defect, look for others with the same cause. List defects that were there before this change separately.
- Phase review: look at the whole change across units: the wiring between them, leftovers, stale tests or docs, and whether it does what the plan said.
Say what you could not verify. End with PASS or FAIL, then the defects, most severe first.
```

## Rules

- **One writer per checkout.** Only Builders write in the real checkout, one at a time; Builders working in parallel each get their own worktree and land one at a time. Investigators and Reviewers work in scratch copies.
- **Reports are claims.** The checks and the fresh Reviewer are what count.
- **DECISION_REQUIRED:** decide it if it's within your task. Otherwise ask whoever started you; the main session asks the user. Then continue the same Builder with the decision.
- **Interrupted Builder:** first ask the same session, as a follow-up and without tools, for at most 600 characters: where the problem is, its best guess, and what it ruled out. Then treat its changes as suspect: have a Reviewer or Investigator classify the diff before you reuse or revert it.
- **No self-approval:** a Builder never reviews its own work, and a Reviewer never fixes what it found.
- **A pool refuses or runs out mid-loop:** inside the loop these rules decide, not the "next pool" rule in use-agents, section 7. A refused Reviewer is replaced by a fresh one from another allowed pool for that round. If the Builder's pool runs out (the free daily limit, for example), start a new Builder on the next pool with the brief, the current diff and the open findings; it can't continue the old session.
- **Stop and escalate** to whoever started you when the loop stops making progress (step 7), workers keep missing the same context, a decision is beyond your task, or no Builder pool has room.
