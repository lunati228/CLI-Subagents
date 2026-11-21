# Evidence: stopped jobs always continue

Claim: on this branch, a stopped CLI Subagents job (usage limit, cap, cancel, timeout, error, or lost because the PC or its runner died) continues as the same worker conversation, and nothing pushes the caller into a fresh job. On `main` it did not: a continuation of an override job was refused at the pool cap, and a Codex job killed mid-run had no session to continue.

Background: on 2026-11-21 four Codex Sol jobs, started with the user's override, stopped on a usage limit. When the user asked to resume them, every continuation was refused by the 2-job Codex cap (the continuation did not carry the override), so the manager started four fresh jobs, which re-read their whole context.

## Setup

| | |
|---|---|
| Machine | Windows 11 Education 10.0.26300, Intel64 Family 6 Model 151, Python 3.12.10 (64-bit) |
| Date | 2026-11-21 (before run about 21:55 UTC, after runs from 22:08 UTC) |
| Before | `main` at `f1c31cf1bb0971953e61360b870cdf76b0d7948b` (0.2.43), exported with `git archive f1c31cf` into a scratch folder, plus the new test file |
| After | branch commit `f53d9143aa4097195dc0757aaea51bf4bf98e784` (code, docs and tests; the release commit only raises the version) |
| Test input | `tests/test_always_resume.py`, sha256 `f615b028129ce6cae6bfe4558328a9c31a680307bdb0ee6fbad459b7badf1e99` (identical in both runs) |
| CLIs | fake ones only (`tests/fake_codex.py`, `fake_opencode.py`; Antigravity through its parser). Temporary `CLI_SUBAGENTS_HOME`, `CODEX_HOME` and `CLAUDE_CONFIG_DIR`. No real CLI ran and no quota was spent. |

Commands, from the tree under test (PowerShell: `$env:PYTHONPATH="src;tests"`):

```bash
export PYTHONPATH="src;tests"
python -m unittest -v test_always_resume                     # new tests, before and after
python -m unittest discover -s tests -t tests                 # full suite, after
```

## Results

| | Before (`main`) | After (branch) |
|---|---|---|
| New tests (`test_always_resume`, 15 tests, 17 with subtests) | 0 pass: 5 failures, 12 errors | 15 of 15 pass (19.5 s) |
| Full suite | not needed for the claim | 1085 of 1086 pass, 6 skipped (1316.7 s); see the note on `test_capacity` |

### Before: raw summary (`main` + new tests)

```text
test_cli_message_override_flag_and_relay_override_line ... ERROR (csa: unrecognized arguments: --override)
test_continuation_keeps_the_original_override_at_a_full_cap ... ERROR
test_continuation_refusal_says_continue_later_never_fresh ... FAIL
test_continuation_that_never_ran_keeps_the_session_for_the_next_try ... ERROR
test_continuation_without_override_queues_at_a_full_cap ... ERROR
test_follow_up_can_carry_a_new_override ... ERROR
test_job_that_never_started_says_to_start_again ... FAIL
test_killed_runner_leaves_a_continuable_codex_job ... ERROR
test_list_marks_resumable_jobs_and_their_continuations ... ERROR
test_lost_job_recovers_its_session_from_the_event_log (runtime='codex') ... ERROR
test_lost_job_recovers_its_session_from_the_event_log (runtime='opencode') ... ERROR
test_lost_job_recovers_its_session_from_the_event_log (runtime='antigravity') ... ERROR
test_new_relay_resumes_a_finished_job_with_resume_option ... ERROR
test_override_never_bypasses_a_cooldown_on_a_continuation ... FAIL
test_resume_option_rejects_another_runtime_and_job_settings ... FAIL
test_session_start_lists_a_lost_job_with_how_to_continue_it ... ERROR
test_shipped_docs_say_to_continue_not_restart ... FAIL
Ran 15 tests in 38.838s
FAILED (failures=5, errors=12)
```

The failure reasons, with counts (from the same run):

```text
3 LimitError: Codex account 'primary' is at its limit: 1 of 1 subagents running (cap from max_jobs = 1 on the account). ... start again with override=<their words> ...
    (the 2026-11-21 incident: the override continuation is refused at the cap)
6 ValueError: job ... has no session to continue
    (includes the killed Codex runner: main never saved the thread id while the worker ran)
1 AssertionError: ... "Sol budget used up: 1 of 1 jobs this week ... Try again after the weekly budget resets. Only if the user explicitly asked fo[r more parallel jobs, start again ...]"
    (a refused continuation said "start again", not "continue this job")
1 AssertionError: ... "| unknown relay option: resume"
1 KeyError: 'continued_by'
1 AssertionError: SKILL.md does not document resume=
```

### After: raw summary (branch `f53d914`)

```text
test_cli_message_override_flag_and_relay_override_line ... ok
test_continuation_keeps_the_original_override_at_a_full_cap ... ok
test_continuation_refusal_says_continue_later_never_fresh ... ok
test_continuation_that_never_ran_keeps_the_session_for_the_next_try ... ok
test_continuation_without_override_queues_at_a_full_cap ... ok
test_follow_up_can_carry_a_new_override ... ok
test_job_that_never_started_says_to_start_again ... ok
test_killed_runner_leaves_a_continuable_codex_job ... ok
test_list_marks_resumable_jobs_and_their_continuations ... ok
test_lost_job_recovers_its_session_from_the_event_log ... ok
test_new_relay_resumes_a_finished_job_with_resume_option ... ok
test_override_never_bypasses_a_cooldown_on_a_continuation ... ok
test_resume_option_rejects_another_runtime_and_job_settings ... ok
test_session_start_lists_a_lost_job_with_how_to_continue_it ... ok
test_shipped_docs_say_to_continue_not_restart ... ok
Ran 15 tests in 19.493s
OK
```

Full suite after:

```text
FAIL: test_timeout_during_capacity_wait (test_capacity.CapacityRunnerTests.test_timeout_during_capacity_wait)
AssertionError: 'capacity retry wait' not found in 'stopped after 1 seconds'
Ran 1086 tests in 1316.696s
FAILED (failures=1, skipped=6)
```

That test gives a job a 1-second timeout and expects it to end during the capacity wait. Here the fake worker itself used the whole second while the suite ran. It does not touch the changed code (its job has no `resume_session`), it passed in an earlier full run of the same runner code (1085 tests; its 6 failures were the then-unfinished skill text, relay prompts over their length budget, and a follow test whose expected output this branch changes), and it passed 5 of 5 times alone and with its module (13 of 13) right after. It is a timing flake, not a regression.

## What each fix does

1. Override on continuations (`jobs.create_job`): a continuation without its own override takes the original job's (`spec.json` stored it). A follow-up can give a new one: `override` on `subagent_message`, `--override` on `csa message`, or a first `override=USER_WORDS` line in a relay follow-up file. Cooldowns, models, effort, login, depth and access still apply (`test_override_never_bypasses_a_cooldown_on_a_continuation`).
2. Capped continuations wait (`limits.admit(continuation=...)`): without an override, a continuation that meets only a job cap (global, runtime, account, pool, `max_jobs_by_room`) is admitted and waits in the runner for a slot, 45 minutes instead of 15. Chosen over a better refusal text because the incident showed managers improvising a fresh job after a refusal; waiting needs no decision from them. Refusals that remain (cooldown, usage ceiling, budget, spend, models) now say to continue the job later with `resume=JOB` and never to start a new job.
3. Resuming without the relay: a relay prompt beginning with `resume=JOB` continues that job (or messages it and follows it while it still runs). `subagent_list` / `csa jobs` show the task, `resumable`, `continues` and `continued_by`; `resumable_only` / `--resumable` list stopped jobs nobody continued.
4. PC switched off: the runner now saves the worker's session id within about 2 seconds of the worker announcing it. `jobs.resume_point` finds it in `inbox-closed.json`, the status, the attempts, the event log (read with the runtime's own parser, for records written before this change) or, for a continuation that never ran, the session it was meant to continue. The session-start hook lists lost jobs with how to continue them.
5. Every stopped job's summary, a relay's FAILED result and the hook say how to continue it.

## Per runtime: what a continuation resumes

| Runtime | Session id known | Resumed with | Context after a hard kill |
|---|---|---|---|
| Claude Code | at launch (`--session-id`), so always saved | `claude --resume <id>`; can move to another account (`transfer_session`) | transcript JSONL written per message |
| Codex | first event (`thread.started`), saved mid-run since this change | `codex exec resume <thread>` on the same account | rollout JSONL appended as it runs |
| OpenCode | every event (`sessionID`), saved mid-run since this change | `opencode run -s <id>` | OpenCode's local session store |
| Antigravity | `init` event (`conversation_id`), saved mid-run since this change | `agy --conversation <id>` on the same account | not verified: unknown whether agy keeps a conversation that was killed mid-turn |

Not continuable, by design: a job that never started its worker (queued, refused or cancelled first) has no conversation, and csa says to start it again. A worker killed before its first event reached the disk has no session id either; csa says so too. Only the fake CLIs were used: what each vendor CLI restores after a hard kill was not checked live (that needs real jobs and quota).
