# Relay quoting reproduction and stdin transport

Historical transport for 0.2.14. Version 0.2.15 moves native relays to
[inbox files](relay-length-limit.md), retaining these stdin paths for
compatibility and the shell reproductions below as regression tests.

Checked on 2026-10-23 with Git Bash 5.3.15 and Windows PowerShell 5.1.26100.9711. All commands used a `csa` capture stub that recorded argv and stdin, or the repository's fake Codex. No real worker CLI ran.

The 2026-10-18 report did not include its command or shell, so its exact invocation remains unknown. The current instructions reproduce the failure class: they ask the relay model to extract option values and shell-quote them. An apostrophe in task stdin is safe; an apostrophe in a naively quoted option fails before csa runs.

For example, this old-style Git Bash invocation fails with an unmatched quote:

```bash
csa run --follow --window 60 --runtime codex --access read --override 'user's words' - <<'CSA_TASK'
Task
CSA_TASK
```

The corresponding PowerShell option, `--override 'user's words'`, also fails in the shell parser. Bash's correct quote splicing and PowerShell's doubled apostrophes require different transformations. Even with apostrophes correctly doubled, PowerShell 5.1 removed embedded double quotes from an option such as `--override 'user''s "words"'` when passing it to the native capture process. That value arrived as `user's words`.

Both shells preserve apostrophes, double quotes, backslashes, `$`, backticks and Unicode in literal task stdin when UTF-8 is configured. The Bash task transport has a separate failure: a complete `CSA_TASK` line ends the heredoc, and later lines become shell commands. A leading `'@` line does the same to a PowerShell single-quoted here-string. The regression tests demonstrate both using harmless output markers. A PowerShell fallback that doubles payload quotes also needs to account for smart quotation marks, which PowerShell treats as quotation marks.

The fix moves options into stdin too. Every relay uses a fixed command:

```bash
csa run --follow --window 60 --runtime codex --relay -
```

Only the runtime differs between worker types. csa reads the leading option block, validates it, and passes the remaining task to the existing job admission and runner code. The first non-option line or blank separator ends option parsing. Option values are literal data; shell quoting is neither required nor removed. Invalid requests produce a token-checked FAILED frame without starting a worker. Access defaults remain `edit` for Claude and `read` for Codex and Antigravity. Account selection, explicit-only accounts, spend caps and overrides use the existing admission path.

Bash uses the literal delimiter `CSA_RELAY_PAYLOAD_8c85aaf97de2456c9a544d2c09b16eb1`, appending `_NEXT` until no payload line equals it. PowerShell prefixes every payload line with one `|` inside its here-string, then removes exactly that prefix with `-replace '(?m)^[|]', ''` before piping UTF-8 to csa. Neither transport requires escaping payload quotes. The same transport carries answers and follow-ups through `csa message ... -`; those contents remain message text and are never parsed as options. Shell stdin transports retain their usual final newline behavior.

Run the reproductions and parser checks with `PYTHONPATH=src;tests` on Windows:

```powershell
python -m unittest test_relay_shells test_relay_input
```

The shell tests skip a shell only when it is unavailable. They cover both the old failures and the new literal transports, including delimiter collisions, here-string terminators, existing `|` prefixes, blank lines and Unicode. The CLI test runs `csa run --relay` in a subprocess and verifies that fake Codex receives the exact task after its normal worker note.

Shell behavior references: [Bash heredocs](https://www.gnu.org/software/bash/manual/html_node/Redirections.html), [PowerShell quotation rules and here-strings](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_quoting_rules?view=powershell-7.5), and [PowerShell native argument passing](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_parsing?view=powershell-7.5#passing-arguments-that-contain-quote-characters).
