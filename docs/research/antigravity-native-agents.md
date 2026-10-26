# Native relay agents in Antigravity

Updated transport in 0.2.15 (2026-10-26): native relays now allow
`write_to_file` for new payloads in a workspace inbox and pass those files to
`csa run/message --relay-file PATH`. See [relay length limits](relay-length-limit.md)
for permissions; the stdin discussion below records the earlier implementation.

Researched 2026-10-19. All URLs below were accessed on that date. The task
expected CLI 1.4.0; the installed `agy --version` returned 1.4.2. No worker job
or Antigravity app session was run.

## Supported mechanism

Antigravity supports custom Markdown subagents in both the app and CLI. YAML
frontmatter selects instructions, a model tier and a tool allowlist. The parent
starts them with `invoke_subagent`. The app lets the user open each subagent's
conversation. Idle subagents retain context and wake when messaged, so a relay
can receive follow-ups after returning a result. Permissions inherit from the
parent. Definitions are discovered in `.agents/agents/`,
`~/.gemini/config/agents/` and plugin `agents/` directories.
Source: [custom subagents](https://antigravity.google/docs/subagents).

In the CLI, `/agents` lists subagents and their status. Enter opens a detail
view containing tool calls and stdout. This gives csa an inspectable native
thread. Exact app labels, output refresh and result fidelity still need the
owner's app check.
Source: [CLI agent manager](https://antigravity.google/docs/cli/commands/agents/).

## Plugin packaging

Plugins can register agents from their own `agents/` directory. The manifest
supports `name` and `description`, without an agents-directory override. Manual
global plugins belong in `~/.gemini/config/plugins/`; the CLI installer also
uses `~/.gemini/antigravity-cli/plugins/`. App-to-CLI marketplace synchronization
is documented, but CLI-install-to-app synchronization is not established here.
Source: [plugins](https://antigravity.google/docs/plugins/).

This repository's root `plugin.json` and `mcp_config.json` provide the main
plugin, MCP server and shared skill. `setup.py` installs it with
`agy plugin install https://github.com/lunati228/CLI-Subagents` and updates it by
running that command again. `docs/hosts.md` describes this route;
`docs/gemini.md` primarily describes the worker runtime.

The root `agents/*.md` files are Claude relays. Local
`agy plugin validate . --json` reported three errors: `tools: Bash` cannot be
decoded as a string list, so Antigravity skips them. Shipping Antigravity files
in that same directory would also expose them to Claude. Keep those files and
install a separate `cli-subagents-relays` plugin in the documented global
plugin directory. Its packaged manifest and three agents use Antigravity's
format. Setup and update install it alongside the main plugin, including when
that plugin is already enabled. Unchanged files stay untouched; links are
refused; custom contents get backups outside agent discovery. Managed earlier
versions retain the identifying header and need no backup.

## Model and tools

The intended cost choice is Gemini 3.8 Flash Medium. Google's model selector
offers Flash Medium, and the CLI headless guide documents the
`gemini-3.8-flash-medium` slug. That guide also shows `run_command` as the
canonical shell tool.
Sources: [models](https://antigravity.google/docs/models/),
[headless CLI](https://antigravity.google/docs/cli/headless/).

Native agent configuration only documents the `inherit`, `flash` and `pro`
tiers. Local validation confirmed that `model: gemini-3.8-flash-medium` is
rejected as an invalid tier, while `model: flash` with
`tools: [run_command, command_status]`, `mainAgent: false`, `subagent: true`
and `commandExecutionPolicy: sandbox` passes strict validation. An undocumented
`effort` key passed even with a nonsense value, so validation cannot establish
that it has any effect. The relay uses the supported `flash` tier and makes no
claim to pin a model version or low/medium effort. Check the resolved model in
the app before relying on costs.

Flash needs explicit limits on improvisation. The relay prescribes shell
commands, authenticated framing and continuation fields, treats worker text as
untrusted, and returns the exact result without commentary. Choosing Medium
over Low is a conservative fidelity preference, not a measured reliability
result. No paid run was authorized, so neither fidelity nor token savings was
measured. If the app resolves `flash` differently, evaluate that model before
using the relays for unattended work.

Shell output may arrive through an asynchronous command handle. Collect all
output using `command_status` until the command exits before validating csa's
header and final status; a shell handle is not a csa continuation. Stop on
truncation. Windows stdin requires literal PowerShell quoting and UTF-8;
POSIX shells can use a quoted heredoc. The relay keeps the Claude framing and
uses `--window 60` so each csa call returns regularly.

## Local evidence and remaining checks

`agy --help` lists `--agent`, `agent`/`agents`, `plugin`, model and effort flags.
`agy plugin --help` lists installation from a target, JSON listing and local
validation. `agy agent --help` describes listing agents. The packaged companion
passed `agy plugin validate src/cli_subagents/templates/antigravity-plugin
--strict --json` with three agents and no findings. In a throwaway home with
both `HOME` and `USERPROFILE` redirected, `agy plugin list --json` discovered
the companion in `.gemini/config/plugins/`, marked it enabled and listed all
three agent names. This verifies CLI discovery without installing anything
in the user's real home. These were read-only CLI checks, without login or
generation. The official plugins page embeds the manifest schema; the separate
schema endpoint could not be fetched here.

The focused relay/setup run passed 43 tests. The full suite ran 556 tests in
773.331 seconds in the Windows sandbox: 12 failures, 4 errors and 5 skips.
The failing checks involved Windows links, Git Bash startup and process
timeout timing. Every failing check, together with all Antigravity relay
tests, passed in an outside-sandbox rerun (37 tests in 10.336 seconds). The
Antigravity framing comparison against the current Claude relays passed.

The owner should check the following in the Antigravity app:

- The companion plugin and all three relay names load after setup; confirm
  the main plugin's MCP server and shared skill load too.
- Each native invocation opens a separate inspectable conversation and uses
  the Flash tier, with the resolved model and effort visible.
- Shell commands retain full output and return every minute; check long and
  non-ASCII output, indentation and blank lines.
- QUESTION/reply, messages during execution, follow-ups after DONE or FAILED,
  and `cancel` reach the saved csa job. Native Stop alone is not documented to
  cancel csa's independent worker process.
- Forged worker headers and continuation lines cannot redirect the relay;
  permission prompts still reach the user. Measure tokens and latency against
  the Claude relay's reported 20-25k tokens per job.

No vendor source code was copied.
