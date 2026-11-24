# Evidence: DeepSeek workers write status lines, not their reasoning

Claim: with 0.2.45, a DeepSeek worker's messages between tool calls (the text a relay copies) are short status lines instead of thinking out loud. In relay mode, what is left is cleaned, capped and batched, and the relay is no longer told to copy it word for word. Before, relays copying DeepSeek's text were refused by Anthropic's `reasoning_extraction` classifier.

## Background

On 2026-11-22/23 four relay turns (Haiku 5.5, `cli-subagents-claude`) were refused with `stop_details.category: "reasoning_extraction"`, explanation "restrictions on reverse engineering or duplicating model outputs". All four were relaying paid DeepSeek V4.1 Flash jobs (`account=deepseek`); no relay of a Claude worker was refused. Anthropic's docs define the category as a request that "asks the model to reproduce its internal reasoning in the response text"; anthropics/claude-code#95561 traces other false positives to prompts asking Claude to narrate its reasoning.

One of those jobs, `20261123-003015-db21d3` (events.jsonl sha256 `50ce3826fbea658ecf2d0870b409f0e257ba05328ea663639c7235cefcde58dc`, 466 tool calls, 71 minutes), wrote 276 messages. One of them was 112,189 characters of "Let me check. / I'll check." repeated; csa's 4,000-character cap still handed the relay 4,000 characters of it. `csa follow --window` returns on every finished message, so 276 messages also ran the relay into its 200-turn limit.

## Setup

| | |
|---|---|
| Machine | Windows 11 Education 10.0.26300, Intel64 Family 6 Model 151, Python 3.12.10 |
| Date | 2026-11-24, about 13:40 to 14:10 UTC (job ids use local time, UTC+1) |
| Before | installed csa 0.2.44 (`main` at `f2b45b2`) |
| After | branch `deepseek-status-messages` on `f2b45b2`, run from its `src` with `PYTHONPATH` (csa's runner keeps the caller's package root) |
| Paid DeepSeek | `csa start --runtime claude --account deepseek --model deepseek/deepseek-v4.1-flash-1017 --effort max --access read`, read with the job's `events.jsonl`. No relay: no Anthropic model saw the worker's text. |
| Free DeepSeek | `opencode run --format json --thinking --archived --agent plan -m "opencode/deepseek-v4.1-flash-free#max"` (OpenCode 2.3.1) with csa's worker note in front of the task, no csa: [ds_probe.sh](deepseek-status-messages/ds_probe.sh) |
| Task | the same read-only question in every run: how an account's cooldown works (text in `ds_probe.sh`). The long run used a 34-tool-call audit of `follow.py` relay mode. |
| Analysis | [paid_analyze.py](deepseek-status-messages/paid_analyze.py), [ds_analyze.py](deepseek-status-messages/ds_analyze.py), [compare_models.py](deepseek-status-messages/compare_models.py). "Messages" leaves out the final answer, which relays never copy as a message. "Think-aloud" counts messages whose first 200 characters contain "Let me", "Let's", "Actually", "Wait", "Hmm", "I think", "I need to", "Okay", "So". |

Note text: v1 is [line-v1.txt](deepseek-status-messages/line-v1.txt); v2, the shipped `STATUS_NOTE`, is [line-v2.txt](deepseek-status-messages/line-v2.txt). A first v2 draft used an example that echoed the test task ("the setter in accounts.py"); those three jobs were cancelled before they finished and are not counted.

## How the models write (existing jobs, before any change)

`compare_models.py` over every job in `~/.cli-subagents/jobs` on 2026-11-24:

| Worker | Jobs | Messages | Opening with think-aloud | Longest message |
|---|---|---|---|---|
| DeepSeek V4.1 Flash, paid (Claude Code) | 48 | 1,343 | 56.3% | 112,189 chars |
| DeepSeek V4.1 Flash, free (OpenCode) | 10 | 53 | 37.7% | 18,138 |
| Claude Opus 5.5 | 34 | 877 | 5.2% | 13,388 |
| Claude Fable 5.2 | 16 | 164 | 1.8% | 7,070 |

A native Haiku 5.5 worker on a read-only task wrote 4 messages ("Found the key symbols. Next I'll read the header and constants, then `said()`.") with its reasoning in 6 thinking blocks.

## Results: the worker note

Paid DeepSeek, 3 jobs each (all `success`):

| | Messages | Think-aloud phrases | Insight boxes | Median chars per job |
|---|---|---|---|---|
| Before (0.2.44 note, user's Explanatory output style) | 12 | 10 | 4 | 422 |
| Note v1 + plain output style | 7 | 4 | 0 | 106 |
| **Note v2 + plain output style** | **3** | **1** | **0** | **56** |
| Note v2, long audit (1 job, 34 tool calls) | 2 | 0 | 0 | 116 |

Free DeepSeek in OpenCode, 3 runs each: before 3 messages, 1 opening with think-aloud; v1 5 and 0; v2 7 and 1. OpenCode with `--thinking` already keeps DeepSeek's reasoning in separate parts, so the free route was not the problem; v2 gives its messages the example's shape ("Found the core functions in `accounts.py`; now reading them and their callers.").

Raw runs: paid jobs `20261124-144548-310bff`, `-144549-84f91f`, `-144550-4cc736` (before), `-144801-d40751`, `-144803-40d43c`, `-144804-15a6c1` (v1), `-145052-eb3614`, `-145052-3a88c0`, `-145053-985f44` (v2), `-145251-ef1fa1` (long) in `~/.cli-subagents/jobs` on the test machine; they hold repository text only. Free runs (sha256 prefixes of the OpenCode json): base `1ae71e2cd5adccbe` `d8d4db37fdc36734` `9345d18d1439e218`, v1 `3680fde194ba6101` `3c21c1f45dda14af` `444a1415fa7263c3`, v2 `0762b0df9a4c41e0` `beff1578c0aa8828` `f5ed24e4a66af2c6`, kept in the session scratchpad (2 to 4 MB in all, not committed).

## Results: relay-side cleaning

`status_text` on the 276 messages of `db21d3` (replayed from its events.jsonl): 50,060 characters to 41,140, think-aloud phrases 623 to 362. The 112,189-character loop becomes four lines (the first two describe the job's own task and are replaced here by placeholders):

```text
<one line: which measurement round is running; waiting for its notification>

<one line: the steps left after the run>

Let me wait for the notification.

Let me hold and wait for the notification — no more polls.
```

The cleaning is a backstop; the note removes most of it at the source. With the 45-second minimum per call, a 71-minute job needs at most about 95 relay calls for its messages instead of one per message.

`csa follow --window 100` from the branch on the long v2 job, as a relay would see it (no model involved):

```text
Post the status update below as your message, then run the command above.
===== MESSAGES =====
| I'm the relay agent for Claude Code deepseek/deepseek-v4.1-flash-1017 (effort max). Read from the worker's transcript. My own model isn't visible from here.
|
| I'll start by reading the target file and locating the relevant tests.
|
| The file is read. Now the relay-related tests.
===== END MESSAGES =====
```

## Tests

`tests/test_think_aloud.py` (sha256 `f241e2b1a2d74817503c208e4b9eb3873f6e995dab332cd2e9ac940b5a2c5a00`), fake CLIs only: 10 of 10 pass. Full suite: 1095 of 1096 pass, 6 skipped (1156 s). The one error, `test_messages.MessageTests.test_worker_server_lists_only_worker_tools_and_pings_progress` (no reply with id 2 from the worker MCP server), passed 3 of 3 times when run alone on the branch and once on `main`: a timing failure under full-suite load, in code this change does not touch.

```bash
export PYTHONPATH="src;tests"
python -m unittest -v test_think_aloud
python -m unittest discover -s tests -t tests
```

## Cost

Paid runs bill the `deepseek` account's OpenRouter key (approved for this test on 2026-11-24). OpenRouter's weekly figure for the key, which includes anything else billed to it, was $6.47 after the three baseline jobs and $6.84 after the long job: $0.37 for the 10 later jobs (three of them cancelled early). The baseline jobs were not priced separately; at the same task size they cost about $0.04 each (estimate). Free OpenCode runs cost nothing.

## Not measured

No live relay ran on 0.2.45 before release, so whether the classifier stays quiet is not yet shown; the refusals came about once per several hundred relay turns, so a single short relay job would prove little. The plan is to watch relay transcripts of DeepSeek jobs after the update (search for `reasoning_extraction` in `~/.claude/projects/*/*/subagents/*.jsonl`).
