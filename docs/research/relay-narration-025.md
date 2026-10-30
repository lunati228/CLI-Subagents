# Relay narration in native subagent transcripts

Checked 2026-10-30 for 0.2.25. No model jobs or real user settings were used.

## Relay decision

Keep the native relay and copy short worker narration into its assistant messages
between follow windows. Prefer the latest narration or reasoning summary for
the next call's title. This uses the existing agent transcript instead of a new
UI API. The app still decides whether assistant text appears in a collapsed
view; a desktop check after plugin reload remains necessary. Windows can be
shortened with the existing `window` option; the default stays 60 seconds to
limit relay cost.

Prior art checked: OpenAI's [Codex relay agent](https://github.com/openai/codex-plugin-cc/blob/main/plugins/codex/agents/codex-rescue.md)
(Apache-2.0) and [Gemini Companion's relay agent](https://github.com/josephyaduvanshi/gemini-companion/blob/main/plugins/gemini/agents/gemini-rescue.md)
(Apache-2.0). Both instruct a thin agent to make one shell call and copy stdout;
neither provides native transcript injection. No source was copied. The earlier
[display research](native-subagent-display.md) records desktop observations:
shell output is inside tool results; the relay's own text belongs to its
transcript. [Claude's subagent docs](https://code.claude.com/docs/en/sub-agents)
describe native agent transcripts and completion delivery but no public API to
insert external worker events as native tool calls. A mod would depend on an
early access API and is unnecessary for the requested short narration.

Progress is display data, capped at three excerpts of up to 399 characters each
(at most 1,200 characters including their separating newlines) per window.
Only assistant narration, reasoning display summaries and new worker progress
notes enter it, never tool arguments. Exact delimiters and worker prefixes keep forged headers inside the
body. Authentication and continuation decisions remain unchanged. The relay
copies the sanitized excerpts without interpreting them. This is delayed by one
follow window, not token streaming or a promise of identical native UI. Only
completed narration enters this block; partial Antigravity text still updates
the title and waits for completion to avoid repeating it across windows.

## Validation and limits

Fake event tests cover all four runtimes, byte-offset continuation, absent
narration, multiline text, bounded excerpts and hostile delimiter/control text.
The existing final status, token, question and continuation protocol stays in
place. All twelve relay templates use the same copy-only rule. No new model
request is needed: the copy can share the relay turn that calls `csa follow`.

The docs and prior desktop observations support putting assistant text in the
opened transcript. They do not prove that every desktop version promotes it to
a collapsed row, or that Haiku follows the copy instruction on a live run. After
the main session installs this release, check one normal worker with narration
before and between tool calls. If the app still hides that text, retain the
relay and evaluate a mod-driven transcript bridge or MCP tools with wake-up hooks
with the owner; do not remove it automatically.

