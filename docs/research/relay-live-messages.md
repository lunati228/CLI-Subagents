# Relay: the worker's messages, one by one

Checked 2026-11-03 on the owner's PC, from existing job folders and one relay transcript. No model jobs were run.

## Goal (owner, 2026-11-03)

The relay's view in the Claude desktop app should read like the Codex desktop app: the worker's short progress
messages ("340 of 650 texts are saved. ...") appear one by one as the worker writes them, copied word for word.
The relay's first sentence says which model it is and which worker model it relays for, and that identity must
come from logs, not from the prompt or the relay's system prompt. The main session must not see any of this; it
gets only the final result, as now.

## What the CLIs emit

Every runtime's JSON stream already carries the worker's in-between messages, and `follow.py` already parses
them into `says:` lines:

| Runtime | Event | Jobs on this PC | Messages | Note |
|---|---|---|---|---|
| Codex, GPT-6.1 Sol | `item.completed` / `agent_message` | 71 | 548 | Same text the Codex app shows; also 1,924 reasoning titles |
| Codex, GPT-6 Luna | same | 6 | 14 | Luna rarely writes progress messages |
| Claude Code, Opus 5.5 | `assistant` `text` blocks | 50 | 915 | |
| Claude Code, Haiku 5.5 | same | 15 | 189 | |
| Claude Code, DeepSeek via OpenRouter | same | 36 | 296 | |
| OpenCode, DeepSeek free | `text` parts | 22 | 69 | Mostly reasoning (936 parts), few messages |
| Antigravity, Gemini Flash | `step_update` `agent_response` deltas | 3 | (streamed) | csa joins the pieces |

Message counts include each job's final answer. One Sol job (20261103-211216-73718e, 18 min) sent 14 messages of
146-286 characters, about one every 80 seconds.

## Where the real model comes from

| Worker | Source | Checked |
|---|---|---|
| Claude Code | `system`/`init` event, `model` | Yes. Job 20261031-205133-04cc4e asked for `opus` on the `deepseek` account and the init event says `deepseek/deepseek-v4.1-flash-1017` |
| Codex | rollout file `turn_context`: `model`, `effort` (csa already finds the rollout for rate limits) | Yes: `gpt-6.1-sol`, `high` |
| Antigravity | `init` event, `model` | Yes: `gemini-3.8-flash-high` |
| OpenCode | not in the `--format json` stream | Open: try `opencode export <session>` or its session store |

The relay itself: Claude Code writes the serving model into each assistant entry of the subagent transcript
(`message.model`, e.g. `claude-haiku-5-5`). `csa` runs inside the relay's Bash with `CLAUDE_CODE_SESSION_ID` and
`CLAUDE_EFFORT` set, and the relay's transcript is the one file under that session's `subagents/` folder that
contains the random inbox path. Still to check: that a subagent's Bash gets its own `CLAUDE_EFFORT` value, and
that the transcript entry is on disk when csa looks.

## The current relay, measured

Transcript `C--GitHub-CegTec/9b954809-.../subagents/agent-a95942083a6986ade.jsonl` (2026-11-03): one Haiku 5.5
relay carried four Codex Sol jobs, about 60 minutes of worker time, in 72 requests.

- It copied the PROGRESS block once in about 67 follow windows, and that once it paraphrased. This is why the
  view shows only row titles.
- Tokens: 4,818,796 cache read, 328,786 cache write, 23,349 output (about 8,800 of them re-typing the four task
  prompts into inbox files), 150 uncached input.
- At list price (Haiku 5.5 $0.10 in / $0.50 out per million; cache read 0.1x and 5-minute cache write 1.25x of
  input): $0.048 + $0.041 + $0.012 = about $0.10 for the hour. On a subscription this counts toward the plan's
  limits instead.
- Each follow window adds about 1,500 tokens to the relay's context (mostly `$ command` and `thinking:` lines),
  and every later request re-reads all of it: the context grew from 12k to 123k tokens. That re-reading is most of
  the cost, not the copying.

### How it was measured

Machine: the owner's Windows 11 PC (10.0.26300), 2026-11-03, repository at c78055b. The inputs hold work
conversations and stay on that PC; their SHA-256:

- relay transcript `agent-a95942083a6986ade.jsonl`: `3e373af7bacaa2aaf1f83e9c034627ea5ac9ed67ec755cf21c7038fe6f9fbf4e`
- Sol job `20261103-211216-73718e/events.jsonl`: `fc95ae710a943ae4eafbba1e6d8fd7ce098695da5bd19b00458eb9e81c79c46a`

Token totals sum `message.usage` once per assistant message ID; copied text is any assistant `text` block:

```python
import json, sys
totals, seen = dict.fromkeys(("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"), 0), set()
for line in open(sys.argv[1], encoding="utf-8"):
    entry = json.loads(line)
    if entry.get("type") != "assistant":
        continue
    message = entry["message"]
    for block in message.get("content", []):
        if block.get("type") == "text":
            print("TEXT:", block["text"][:200])
    if message.get("id") not in seen:
        seen.add(message.get("id"))
        for key in totals:
            totals[key] += message.get("usage", {}).get(key) or 0
print(len(seen), "requests", totals)
```

Output: `72 requests {'input_tokens': 150, 'cache_creation_input_tokens': 328786, 'cache_read_input_tokens':
4818796, 'output_tokens': 23349}` and one `TEXT:` line. The per-runtime message counts come from the same kind of
pass over every `~/.cli-subagents/jobs/*/events.jsonl`, counting the event types in the first table. Cost is
list price times tokens; nothing was billed per token, since the relay ran on a subscription.

## Design that fits the goal

1. `csa follow` returns as soon as a new worker message is complete, or after a few minutes of silence, instead of
   every 60 seconds.
2. Its output to the relay is only the new message(s) and the control lines. Tool lines stay in `events.jsonl`
   and in `csa follow` for people.
3. The first call prints a verified identity line built from the sources above, including a mismatch note when the
   worker reports another model than the one asked for.
4. The relay copies the identity line once, then each message as its own assistant text, then runs the next
   command. Row-title handling goes; the instructions get shorter.

Estimate for the same hour (about 45 messages): each turn adds about 450 tokens instead of 1,500, so about 1.0M
cache read, 35k cache write and 19k output, roughly $0.02-0.03 instead of $0.10. The copied text itself is about
60 output tokens per message.

Risk: Haiku may still skip or paraphrase. The new output makes the copy the main thing in each result, which
should help; if it doesn't, raise the relay's effort or move to a mod.

## Built in 0.2.32

- `--window` now means relay mode (follow.py): only `says:` messages, worker notes and review denials go into a
  `===== MESSAGES =====` block; a call returns once a message is complete and the worker has moved on (1 s
  settle), after 15 s without a next step, or at the window (default 180 s). A message equal to the final result
  is dropped at DONE so the user sees it once. Row titles and the PROGRESS block are gone.
- identity.py builds the first line from the relay transcript (found under `CLAUDE_CODE_SESSION_ID` by the inbox
  file name or job ID), the Claude worker's transcript in its account folder, the Codex rollout's last
  `turn_context`, or Antigravity's init event. Shown once per job (`identity-shown` in the job folder).
- All 12 relay templates gained a "What the user sees" section that makes copying the block the first rule.
- Not yet checked live: whether Haiku at effort low now copies reliably, whether the app shows the copied text in
  the collapsed view, and that the relay's transcript already holds its inbox path when csa looks. If Haiku still
  skips, the next step is `effort: medium` on the relay agents.

## Live test of 0.2.32 (2026-11-03, 23:56 UTC+1)

Two read-only review jobs started from the Claude desktop app through the relays, each asked to write one or two
sentences before each step. Inputs are this session's relay transcripts and the job's event log on the owner's PC:

- Luna relay `agent-a6c3734e913ff9f67.jsonl`: `3fa8df98d1a21d7d3e60d013801070c3ce87ce625cbd26effda01e0600ceaf5b`
- DeepSeek relay `agent-a619802647b0f15bb.jsonl`: `7e678fb7d051e0a4e440c900d76346961d3414f4c9d42efcfa12781c866f1f33`
- Luna job `20261103-225609-5e4b83/events.jsonl`: `585adb6b513fd978f8525713b989774b1cc47e01d33f07587e92275473274865`

csa delivered every message of the Luna job (`gpt-6-luna` on `codex-2`, 57 s) live, one block per message, at
3, 9, 13, 18, 24 and 45 s, and the final answer only as the result. The identity line read
`I'm Haiku 5.5 (effort low), the relay agent for Codex gpt-6-luna (effort high). Read from my transcript and
Codex's session log.` (no effort was requested; Codex's log shows the effort it used).

The Haiku relay at effort low copied 2 of 6 blocks word for word (the identity line and one message), skipped 3,
and once wrote its own sentence instead ("I'm writing the worker's message from this run as shown, without
changes."). The relay used 10 requests: 11,321 cache-write, 97,779 cache-read and 2,474 output tokens. The
DeepSeek job failed at once (OpenCode's free daily limit); its relay copied the identity line exactly but added a
paragraph of its own to the hand-back.

Changes in 0.2.33: the Claude Code relays run at effort medium; csa prints the block last, after the next
command and a line telling the relay to write it now; the relay rules say to write the text itself, never a
sentence about copying it, and that the hand-back holds only the status and result even when the job failed;
the relay-transcript search reads the end of the file, where the markers are.

## 0.2.33 retest and the options without a model (2026-11-04, 00:15 UTC+1)

Same Luna task with the 0.2.33 relay at effort medium (transcript `agent-ad911b539a7f8b72d.jsonl`,
`e34f35dbd45597fc7d78eb65344d0181fde505c537e1ba8eeb13fafcc46e2098`): csa again delivered 6 blocks. The relay copied
the first 3 (two with `’` typed as `'`), then only made the next calls, even with the block printed last under a
"write it now" line. 11 requests: 12,236 cache-write, 114,025 cache-read and 3,149 output tokens, 27% more output
than at effort low for no better copying. Counted with [scripts/relay_check.py](scripts/relay_check.py)
(`python -I docs/research/scripts/relay_check.py <relay transcript>`). It compares each delivered block with the
relay's next text exactly, so the two copies with straight apostrophes count as "written differently".

What else was checked, without a mod:

- Monitor or faster polling: every event wakes the agent as a new request, so watching more closely costs more and
  the model still has to write the text.
- Bash description: in the old relay (`agent-a95942083a6986ade.jsonl`, hash above) Haiku set the description to
  csa's title in 56 of 56 calls, against 1 of 67 for the free-text copy. But the app cuts a description off after
  about 45 characters in the agent view (owner's screenshot, 2026-11-04), and messages run 150 to 290.
- Plugin hooks ([hooks reference](https://code.claude.com/docs/en/hooks), read 2026-11-04): PostToolUse hooks fire
  for a subagent's tool calls with `agent_id`/`agent_type` and `tool_response.stdout`. A test hook in a
  gitignored `.claude/settings.local.json` and one Haiku subagent (`agent-af5e802dc0088ff33.jsonl`,
  `80f245b42db5ecbe856e08c3b38083e28403651ee1b2459cb252c04a5c225aac`) showed: `updatedToolOutput` (same object shape
  as `tool_response`, `stdout` replaced) changes what both the model and the app show; `systemMessage` is stored in
  the subagent transcript as a `hook_system_message` attachment but the desktop app's agent view did not draw it.
- Transcript timing: a text block is in the session's transcript before the tool call that follows it runs (a
  probe string in this session's own text was found by the command right after it).

Chosen for Claude Code (0.2.34): csa remembers the last block it gave a relay (`relay-pending.json` in the job
folder) and, on the relay's next call, looks for it after that tool result in the relay's transcript. If it is
missing, csa shows it again at the same offset with "You didn't write the block below", once per block. Matching
ignores quote style and spacing. Relays are back at effort low. Codex and Antigravity hosts have no Claude transcript
and keep the plain copy rule for now.

## 0.2.34 to 0.2.37: false re-asks, then none (2026-11-04)

- 0.2.34 to 0.2.36 re-asked blocks the relay had written. Codex Sol relay in another session, job
  20261104-171441-bbbe38, transcript `agent-a6832cede7f5e0417.jsonl`
  (`56e7e9e9c159d4825e46d9f54bf52e8faf9931f34d88c92705c769c4ad782bd7`): 5 of its 10 message blocks were shown a
  second time although the relay had written them (`relay_check.py` counts 19 blocks delivered including the
  re-asks, 17 copied exactly, 2 written differently, 0 skipped; 43 requests, 913,564 cache-read, 79,288
  cache-write, 14,608 output tokens). Cause, from the transcript timestamps: the relay wrote a block at
  16:14:45.423 UTC, its next `csa follow` started at 45.832 and re-asked at 46.297, before the relay's turn
  reached the file. (The earlier probe had checked the main session's own transcript, which behaved differently.)
- 0.2.37 (c74a7e4, other session) treated "no turn after the result yet" as unknown. That stops the false
  re-asks but also the real ones, because the newest turn is never on disk at that moment. DeepSeek relay on
  0.2.37, transcript `agent-a6c96662db4c62402.jsonl`
  (`2a9f11cac559c0b6d1909a7dfb5830c4ec68af010e6ef6953d286bcdb3668637`): 13 blocks delivered, 8 copied exactly,
  5 skipped, 0 re-asked; 17 requests, 200,104 cache-read, 14,839 cache-write, 3,988 output tokens.
- 0.2.39 judges a block only when the relay's turn after it is closed by the next tool result (the file is
  append-only and in order, so that turn is complete). A skip is re-asked one call late, a written block never.

### After 0.2.39 (2026-11-04, 18:55 UTC+1)

Gemini 3.8 Flash worker through the Antigravity relay (Haiku 5.5, effort low), 8 m 30 s, job
20261104-185546-de930e, relay transcript `agent-ae6eaecd4655bb770.jsonl`
(`cb4486318daead8aaf058961e87b352e7ba73c3ddfaeccacf2d602a335cd10be`), csa 0.2.39 at 2da9996:

- csa delivered 8 blocks (identity, 5 worker messages, 2 review-gate denials) and re-asked 5. Every re-ask
  was for a block the relay had skipped; none for a written one.
- The relay copied the identity line at once and message 1 after its re-ask, wrote message 3 one call late,
  and skipped the rest, including 3 of the 5 re-asks. 3 of the 8 blocks reached the relay view.
- `relay_check.py` counts 13 blocks delivered (with the re-asks), 2 copied exactly, 1 written differently,
  10 skipped; 17 requests, 202,181 cache-read, 18,078 cache-write, 6,041 output tokens (about $0.007 at
  Haiku 5.5 list prices).

Copy rates so far: 2 of 6 and 3 of 6 (Luna, efforts low and medium), 8 of 13 (DeepSeek, 0.2.37), 17 of 19
(Codex Sol, another session, with false re-asks on every block), 3 of 8 here. The detection is now right;
Haiku's compliance varies widely between runs and drops as the relay's run gets longer.

## 0.2.40: titles as the reliable layer, a shorter prompt for copying

Owner's call on 2026-11-04: do what makes most sense long term, without the early-access mod API. The model is the
weak point, so the design now has two layers:

- Reliable: csa prints `[csa] title: <start of the newest message>` (sanitized, at most 100 characters) and the
  relay uses it as its next Bash description, the field Haiku filled in 56 of 56 calls in the old relay. The
  rows of the relay view then name each message even when the text isn't copied.
- Best effort: the Claude Code relay prompt went from about 11,100 to 7,000 characters (frontmatter included),
  copy loop first, the safety rules kept but shorter. Every relay turn re-reads it, so this also lowers cost.
  Re-asks stay; blocks are now matched by their whole text, not their first line (a Gemini review found two
  blocks sharing a first line, such as a code fence, could be confused).

Codex and Antigravity host relays keep their longer prompts for now; tests check both families keep the same
framing rules. The live measurement after release goes below.

## Incident: relay calls moved to the background after 180 s (2026-11-10)

A Claude Code relay (Haiku, csa 0.2.39) for a paid DeepSeek builder, job 20261110-134610-29fe14, in a CegTec
session. Relay transcript `agent-a6ba1078df81f1522.jsonl`
(`baa179d5211f2c114bb4ca1d2674b03acae7d0ae5b551760906192a25af052fd`). Its first four follow calls returned within
20 s each because the worker wrote messages. Then the worker went quiet in a long step: the call started at
12:47:09 UTC came back as "Command was moved to the background (ID: b70zp0lr6) after running for 180s", although
the relay had set `timeout: 600000`. Its retry at 12:50:13 used the same window and was moved at 12:53:13. The
relay never saw a status line, wrote its own "BLOCKED" report and stopped; the worker kept running.

Cause: 0.2.32 raised the relay window from 60 to 180 s, and a silent worker makes a call last the whole window
plus start-up, just over the 180 s after which Claude Code moved this background subagent's command. My live tests
never hit it because their workers wrote often.

Fix (0.2.40): default window 100 s; csa caps every relay call at 150 s whatever `--window` says (the continuation
keeps the given value); the Claude Code relays re-run the same validated continuation when a call is moved to the
background, instead of reading the output file or giving up. Before: 2 of 6 follow calls in this relay were moved
to the background. After: to be checked in the next live run (longest relay call must stay under 150 s).

## Alternatives

- Mod-driven relay (`turn.step` answers a subagent's model requests with chunks built locally; see
  [native-subagent-display.md](native-subagent-display.md)): no model and no tokens, exact copies. Early-access
  API, and there is no Haiku to name in the identity line.
- No relay: MCP tools plus wake-up hooks. Cheapest, but the app shows no per-agent view.
