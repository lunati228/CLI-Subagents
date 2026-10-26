# Relay payload files and host shell limits

Checked 2026-10-26. This extends the [0.2.14 stdin research](relay-literal-stdin.md).
No real worker CLI, login or paid model call was used for this change.

## Why files

The owner's 2026-10-24 Claude Code Windows reproduction passed 5,350 payload
characters but failed with about 10,000 plain ASCII characters in a quoted
heredoc. Bash reported an unexpected EOF while looking for a matching quote.
Real relay prompts of about 9,870 ASCII characters failed too. This is an
observed host transport limit, not evidence that Bash itself has an 8,000
character limit. The earlier apostrophe fix did not remove it.

Microsoft documents a limit of 8,191 characters for
[cmd.exe commands and batch processing](https://learn.microsoft.com/en-us/troubleshoot/windows-client/shell-experience/command-line-string-limitation).
[CreateProcessW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw)
allows 32,767 characters, including the terminating NUL. These limits apply to
different launch paths; neither establishes an exact limit for every host.

Codex's Apache-2.0 source at `rust-v0.162.1`, previously used for the
[native-agent integration](codex-native-agents.md), builds `bash/zsh/sh -c`
or `-lc`, PowerShell `-Command`, and cmd `/c` arguments containing the command
text. See [shell argument construction](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/shell.rs).
Inference: a whole-prompt command remains subject to process and selected-shell
limits on Windows. No separate Codex 8,000-character limit was established or
measured. The native
[apply_patch handler](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/tools/handlers/apply_patch.rs)
accepts the patch as tool input and verifies it against the selected filesystem
sandbox. Use that tool directly, without putting the patch in a shell command.

Antigravity documents `run_command` and `write_to_file` in its
[supported tool list](https://antigravity.google/docs/hooks#supported-tools).
The checked tool, [subagent](https://antigravity.google/docs/subagents) and
[sandbox](https://antigravity.google/docs/sandbox?tab=cli) documentation gives
no numeric command-length limit. Its exact command launch path and practical
limit remain unverified. Files remove the dependency on a large command string
for this host as well.

## Transport and permissions in 0.2.15

`csa relay-path` creates the home inbox if necessary and prints an unused
absolute `relay-<32 lowercase hex>.txt` path. Claude's relay uses Write to put
the complete UTF-8 prompt there, then calls
`csa run --follow --window 60 --runtime RUNTIME --relay-file PATH`. Answers
and follow-ups allocate fresh paths and use `csa message --relay-file PATH`,
retaining reply IDs, offsets, windows and authenticated framing.

The [Claude hooks reference](https://code.claude.com/docs/en/hooks#common-input-fields)
documents `agent_type` for subagent tool events. Write input uses
`tool_input.file_path` and `tool_input.content`; PreToolUse can return
`hookSpecificOutput.permissionDecision = "allow"`. The plugin hook checks the
event, Write tool, exact relay agent name, new filename, home inbox and size.
It emits no decision for anything else. Older hosts that omit `agent_type`
receive no automatic grant and keep their normal permission behavior.

The documented [per-handler `if` filter](https://code.claude.com/docs/en/hooks#common-fields)
uses permission-rule syntax. `Write(**/relay-inbox/**)` avoids Python startup
for unrelated writes and covers custom csa home paths. The handler still
checks the exact root and agent. Its shell command is
`csa hook relay-write; exit 0`, which works in Bash and PowerShell and prevents
an older csa's unknown-action exit code 2 from blocking Write. A newer plugin
can therefore coexist with an older installed csa without disabling writes.

Codex roles inherit their parent's sandbox; see the
[bounded role overrides](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/agent/role.rs).
Antigravity's [permission defaults](https://antigravity.google/docs/permissions)
allow workspace file creation and can ask outside it. These relays therefore
use `csa relay-path --workspace`, which manages `.csa-relay-inbox/` directly in
the caller's workspace, independently of the payload's worker `cwd`. Codex
uses native apply_patch; Antigravity uses `write_to_file` with `Overwrite=false`.
Before issuing a workspace payload path, csa atomically writes a `.gitignore`
containing `*` in that inbox. It repairs existing ignore files without writing
through links, and stale-payload cleanup preserves the ignore file.
No sandbox or approval setting is relaxed. A read-only host or an explicit
denial can still prevent a write; the relay reports that error. Existing
permissions to run csa and access its job/account state remain necessary.

Codex writes the prompt as plain text with Add File. Its patch parser
[normalizes CRLF](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/apply-patch/src/parser.rs)
and the [streaming parser](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/apply-patch/src/streaming_parser.rs)
appends LF to Add File lines. Normalized line endings and an added final
newline are acceptable for prompts. Quotes, backslashes and Windows paths
remain literal content, with no encoding layer that could reinterpret a path
such as `C:\new` as an escape sequence. All three hosts pass plain UTF-8 files.

csa accepts only absolute, direct children of the home inbox or the current
caller's workspace inbox with its filename pattern. It rejects `..`, nested
paths, symlinks, junctions and other reparse points, hard links, nonregular
files and files over 16 MiB before reading. It checks opened-file identity and
directory identity before reading, bounds the read and deletes the file before
decoding or processing it. Valid-path decoding, option and admission failures
also produce authenticated FAILED output. Rejected paths remain untouched.
Allocation removes ordinary matching payloads older than 24 hours, leaving
unrelated files and subdirectories alone. These checks restrict relay file
access; they do not isolate processes sharing the same OS user.

`--relay -` and message `-` remain compatible. No payload is embedded in shell
syntax in the nine shipped native relays. No vendor code was copied.

## Verification

The fake-CLI tests cover prompts above 10,000 characters, literal plain text,
answers and continuation jobs, authenticated failures, path escapes, hard
links, Windows junctions, replacement during open, size limits, cleanup and
the narrow Write hook, Git exclusion and cleanup of workspace payloads.
Shell capture stubs check the old-csa exit-code guard and short commands with large
payload files in Bash and PowerShell, including apostrophes and Unicode in
paths. Native app loading, model copy fidelity and live UI behavior still
need an owner check after updating; no paid smoke test was authorized.

Verification uses Windows with Python 3.12. The sandbox blocked temporary
directory creation, so the fake-CLI tests run outside it.
