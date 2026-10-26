# Native relay agents in Codex

Updated transport in 0.2.15 (2026-10-26): native relays now use apply_patch
to write plain-text payloads into a workspace inbox, then call `csa run/message
--relay-file PATH`. See [relay length limits](relay-length-limit.md)
for permissions and text fidelity; the stdin discussion below records the
earlier implementation.

Researched 2026-10-19. All URLs below were accessed on that date. Checked the
installed Codex CLI 0.162.1 and the source at tag `rust-v0.162.1`, commit
`092d3acd6bec3e3a14bdc7e7a2810ab628ab759d`. No Codex app or paid worker runs were
used. The app behavior below is documented, with local verification still due.

## Finding

Codex supports user-defined native subagents. A relay can run a shell command,
receive its output, and return a final message to its parent. The documented app
UI exposes individual subagent threads for inspection; the CLI has `/subagents`
for switching threads. This supplies the native thread needed for a csa relay.
Current documentation calls the desktop surface the ChatGPT desktop app, while
the installed CLI still calls its launcher `codex app`.
Sources: [subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents),
[Codex documentation entry point](https://developers.openai.com/codex/subagents).

Codex plugins do not register agent files.
Install the supported standalone TOML files through `csa setup` and `csa update`.
They belong in the main Codex home's `agents/` directory, outside the plugin
cache. A plugin-only install therefore needs `csa setup --host codex` as well.
This uses Codex's native agent mechanism; it does not create another job panel.

## Definitions and discovery

Personal agents live under `~/.codex/agents/` (`CODEX_HOME/agents/` when set);
project agents live under `.codex/agents/`. Each standalone TOML file needs
`name`, `description`, and `developer_instructions`. Codex recursively discovers
TOML files next to each active config layer. The loader also accepts older
`[agents.<role>]` entries with `config_file`; standalone files avoid modifying
the user's config. A higher-priority project definition can override a personal
agent of the same name.
Sources: [role loader](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/agent-roles/src/loader.rs),
[discovery](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/agent-roles/src/discovery.rs),
[TOML parser](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/agent-roles/src/agent_role_config.rs).

For example, a standalone relay can set:

```toml
name = "cli-subagents-codex"
description = "Relay a task to a csa Codex worker."
model = "gpt-6-luna"
model_reasoning_effort = "low"
developer_instructions = "Run the prescribed csa command and relay its result."
```

`model` and `model_reasoning_effort` apply per role, including when the parent
uses another model. The native spawn tool selects the role with `agent_type`.
Its v1 form uses `message` and optional `fork_context`; a fresh context keeps a
relay from inheriting the parent's transcript. Send follow-ups to that same
agent through native `send_input` (v1), or `followup_task`/`send_message` (v2).
The relay then runs `csa message JOB --follow --from OFFSET -`; a finished csa
job may return a new job ID for its resumed worker conversation.
Sources: [role application](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/agent/role.rs),
[spawn schema and messaging tools](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/tools/handlers/multi_agents_spec.rs),
[spawn handler](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/tools/handlers/multi_agents/spawn.rs).

## Model and tool limits

`gpt-6-luna` is a valid model ID and supports low reasoning. The 0.162.1 bundled
model catalog lists it as fast and affordable, with `low` among its supported
efforts. The official model page describes it as the cost-sensitive choice.
Use it at low effort for the relay; account availability still depends on the
signed-in account. No model was invoked to test availability or copy fidelity.
Sources: [official model catalog](https://developers.openai.com/api/docs/models),
[GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna),
[CLI model catalog](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/models-manager/models.json).

Roles can disable apps, plugins, memories and permission-request tools, and can
suppress bundled skills and the skill instruction block. Shell tools remain
available when enabled by the parent. There is no Claude-style `tools: Bash`
allowlist: the role application code accepts only selected capability
reductions. MCP servers, sandbox and approval policy remain inherited; setting
those fields in a role does not replace the parent's authority. Other tools
must be avoided through the relay instructions. Do not claim that this creates
an enforced shell-only sandbox or that role instructions remove AGENTS.md.
Source: [bounded role overrides](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/agent/role.rs).

Returning stdout unchanged is an instruction-following behavior, not a byte
passthrough API. Preserve the Claude relays' authenticated header, final status,
result delimiters, worker-line escaping and continuation validation. For Codex
shell calls, retain the whole output across `exec_command`/`write_stdin` chunks
before validating framing. A still-running shell session is not a csa
`STILL RUNNING` status. Handle PowerShell stdin separately from POSIX heredocs.

## Plugin installation

The plugin manifest resource type contains skills, onboarding skills, MCP
servers, apps and hooks. It has no agent resource. The legacy manifest parser
and plugin loader likewise provide no agent-directory discovery. Adding an
`agents` manifest key or shipping Claude Markdown agents alone would not
register Codex roles. The official packaging guide lists the same components.
Sources: [manifest resource type](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/plugin/src/manifest.rs),
[manifest parser](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core-plugins/src/manifest.rs),
[plugin loader](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core-plugins/src/loader.rs),
[plugin packaging](https://developers.openai.com/plugins/build/plugins).

Ship the three TOML templates as csa package resources, which are also present
in the repository installed by the plugin marketplace. Setup and update copy
them to the supported personal-agent directory after plugin installation
succeeds. Keep existing file contents in a backup when replacing an agent and
make repeat setup a no-op. Do not change the user's global model or feature
settings. Manual plugin users need the extra setup command, and removing the
plugin does not remove these personal agent files.

## Local evidence and remaining checks

`codex.cmd --version` returned `codex-cli 0.162.1`. `--help` lists `agents`,
`plugin`, `app-server` and `app`. `features list` reported `multi_agent`,
`plugins`, `shell_tool`, `unified_exec` and `unified_exec_tty` as stable and
enabled; `multi_agent_v2` was stable and disabled. PowerShell blocked the npm
`.ps1` shim, so the checks used its `.cmd` sibling. No login, execution or app
launch command was run.

The CLI source renders each agent's nickname and role in its picker and tool
rows. App-server spawn items carry the child thread ID, role, model and effort,
which gives clients the metadata for native thread navigation.
Sources: [CLI presentation](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/tui/src/multi_agents.rs),
[spawn events](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/core/src/tools/handlers/multi_agents/spawn.rs).

When the owner installs the app, verify all three role names are available,
each creates an inspectable child thread, the relay actually uses Luna at low
effort, and worker lines appear while a shell session is still running. Check
large output for truncation, exact result whitespace, QUESTION/reply handling,
follow-ups after DONE, cancellation, inherited permissions, and shell stdin on
Windows. Measure relay tokens and latency. Documentation does not establish
the app's exact row label or whether command output refreshes line by line.

The source repository is [Apache-2.0 licensed](https://github.com/openai/codex/blob/rust-v0.162.1/LICENSE).
No source code was copied.
