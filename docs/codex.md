# Codex: setup and use

CLI Subagents can run `codex exec` workers the same way it runs `claude -p` workers: in the background, on one of your accounts, with one shared setup. The host can be Claude Code (through the plugin) or any MCP host, Codex included.

Every worker is the official `codex` CLI with your own ChatGPT login. See [provider terms](research/provider-terms.md) for what OpenAI publishes about this kind of use.

If Codex exits with `Selected model is at capacity`, csa retries on the same account after 30, 60 and 120 seconds, resuming its thread when available; [configure capacity retries](configuration.md#model-capacity-retries) per runtime.

## Requirements

- Codex CLI installed and signed in (`codex login status`)
- The `csa` package installed (see the [Claude Code guide](claude-code.md#1-install))

With no extra Codex accounts configured, workers run on your normal Codex login (`~/.codex`, or `CODEX_HOME`).

When Codex is the host, `csa setup --host codex` installs four native relay agents (Claude, Codex, Antigravity and OpenCode). They allocate a path with `csa relay-path --workspace`, then use the native apply_patch tool to write the complete prompt as plain text in the caller's `.csa-relay-inbox/`. `csa run --relay-file <path>` consumes and deletes that file before parsing options. Answers and follow-ups use the same file handoff with `csa message`. Quotes and Windows paths stay literal; apply_patch may normalize line endings or add a final newline, which is acceptable for prompts. Before issuing a path, csa writes an inbox `.gitignore` containing `*` so leftover payloads stay out of Git. Only the short path reaches the shell. File writes use the parent's existing workspace permissions; a read-only host must allow the inbox write or the relay reports the error. See [host setup](hosts.md#codex) and the [transport research](research/relay-length-limit.md).

## Extra accounts

```bash
csa accounts add plus-2 --runtime codex
```

This creates `~/.cli-subagents/homes/codex/plus-2`, links in your shared setup, and opens `codex login` for that folder. Every command CLI Subagents runs for an extra account passes `cli_auth_credentials_store=file`, so each login lives in that account's own `auth.json` and never in a shared keyring entry. The tool never reads it.

Check everything with:

```bash
csa accounts --runtime codex
csa doctor --runtime codex
```

## What every account shares

| From the primary `CODEX_HOME` | How it reaches each account |
|---|---|
| `config.toml` (MCP servers, model defaults, features, profiles) | Written into the account folder before every run, table by table |
| `skills/`, `plugins/` entries | Linked one by one (junctions on Windows) |
| `AGENTS.md` | A hard link to your file |

The account's own `config.toml` keeps three kinds of tables that Codex writes per home: `[projects.*]` (folder trust), `[hooks.state.*]` and `[notice.*]`. Everything else comes from your primary file. Logins, sessions, history and the state databases stay per account. If an account folder already had its own `config.toml` the first time, it is kept as `config.toml.before-cli-subagents`.

This tool's own MCP server stays in the copied config, but every Codex worker, on the primary account too, is started with `-c mcp_servers.<name>.enabled=false` for it, so workers can't start subagents of their own. Instead each worker gets the `csa-parent` tools (`message_parent`, `ask_parent`, `check_parent_messages`) through `-c` overrides.

Workers start without `OPENAI_API_KEY`, `CODEX_API_KEY` or the host's `CODEX_HOME`, so they always use the account's own ChatGPT login.

## What a worker gets from a Claude Code session

When the job comes from Claude Code, a Codex worker receives:

- a short note in its prompt naming your Claude instruction files (`CLAUDE.md`) and skill folders, since Codex doesn't read them itself;
- the MCP servers the Claude session uses for that folder: user and local servers from `~/.claude.json`, approved servers from the project's `.mcp.json`, and servers of enabled plugins (named `<plugin>_<server>`). They are passed as `-c mcp_servers.<name>...` overrides for that job only; nothing is written to your Codex config. A server with the same name in your Codex config wins. `${VAR}` references are expanded the way Claude Code does it.

Values never go on the command line. A stdio server's `env` values are put into the Codex process environment and forwarded with `env_vars`; HTTP headers go through `env_http_headers`, and `Authorization: Bearer ...` through `bearer_token_env_var`. For example, a stdio server `fs` with `env = {API_KEY = ...}` and an HTTP server `web` with a bearer token become:

```
-c mcp_servers.fs.command="npx" -c mcp_servers.fs.args=["-y", "pkg"] -c mcp_servers.fs.env_vars=["API_KEY"]
-c mcp_servers.web.url="https://example.com/mcp" -c mcp_servers.web.bearer_token_env_var="CSA_MCP_1_WEB_TOKEN"
```

with `API_KEY=***` and `CSA_MCP_1_WEB_TOKEN=***` set in the worker's environment.

Turn it off with `share_parent_mcp = false` under `[codex]`. Jobs started from Codex itself never get this; they use your Codex config.

Servers that can't be expressed are skipped, and the job's warnings say why:

- claude.ai connectors (account-bound) are never shared, and neither is this tool's own server;
- SSE and WebSocket servers (Codex supports stdio and streamable HTTP only) and `headersHelper`;
- URLs with a password or query string, and arguments that look like they carry a credential (they would show in the process list);
- an `env` variable that clashes with a different value already in the worker's environment (or starts with `CODEX_`, `OPENAI_`, `CLI_SUBAGENTS_`);
- servers whose `${VAR}` isn't set, and project `.mcp.json` servers you haven't approved in Claude Code.

Servers that sign in through a browser (OAuth) have no token to pass on; log each Codex account in with `codex mcp login <name>`. The shared variables are also visible to commands the worker runs, subject to Codex's own `shell_environment_policy`. Research and sources: the top of `src/cli_subagents/mcp_translate.py`.

## Quota

After each attempt, CLI Subagents reads the last `rate_limits` snapshot from the thread's rollout file (`sessions/YYYY/MM/DD/rollout-*-<thread>.jsonl` in the account's folder) and nothing else from it. The 5-hour and weekly percentages show in `csa accounts --runtime codex` and rank accounts like Claude ones. When a job hits the limit, the snapshot's reset time sets the cooldown unless the error carries a Retry-After value.

Set optional `max_jobs_by_room` steps under `[codex]` to lower concurrency as the remaining 5-hour or weekly quota falls. Other caps still apply, and running jobs continue. See [Configuration](configuration.md#quota-scaled-caps).

## Web search

Codex workers use OpenAI's web search. Set its mode in CLI Subagents:

```toml
[codex]
web_search = "live"
```

Allowed values are `"disabled"`, `"cached"`, `"indexed"` and `"live"`. csa passes `-c web_search="<value>"` on both new runs and resumes. Leave the key unset to keep Codex's own default (`"cached"`).

## Reasoning summaries

New runs and resumes request `-c model_reasoning_summary="detailed"` by default. When Codex emits a completed reasoning summary, `csa follow` prints one `thinking:` line: its bold title and first sentence, or just the first sentence, capped at 200 characters. It does not print the full summary.

Your own `model_reasoning_summary` setting in the account's Codex `config.toml`, project config or active profile takes precedence. To change csa's fallback:

```toml
[codex]
reasoning_summary = "detailed"  # "concise", "auto", "none" or "off" also accepted
```

`"none"` requests no summaries; `"off"` adds no override and leaves Codex's defaults alone. These are the summary levels in the [official OpenAI configuration reference](https://developers.openai.com/codex/config-reference/), plus csa's `"off"` switch. Summaries depend on the model and account: on 2026-10-19, `gpt-6.1-sol` emitted one with `"detailed"` but none with its default or `"auto"`; `gpt-6-luna` on a free account emitted none even with `"detailed"`. A quiet reasoning stream does not mean the worker has stopped.

In Claude Code, the relay returns its output about once a minute using `--window 60`. A terminal follow still streams immediately; see [follow windows](configuration.md#follow-windows).

## Using it

Pass `runtime: "codex"` to `subagent_start`, or `--runtime codex` to `csa run` and `csa start`.

| `access` | Codex flags |
|---|---|
| `read` | `-s read-only` (`sandbox_mode=read-only` when resuming) |
| `edit` | `--approve-for-me` (workspace-write sandbox, Codex's auto-review approves or denies escalations; the same settings as `-c` overrides when resuming) |
| `full` | `--dangerously-bypass-approvals-and-sandbox` |

`model` maps to `-m` and `effort` to `model_reasoning_effort` (`max` is sent as `xhigh`, Codex's highest level). `resume_job` continues a Codex thread with `codex exec resume` on the same account.

## Limits

`codex exec --json` doesn't report remaining quota. CLI Subagents reads local rollout snapshots to rank accounts by usage and get reset times, as described in [Quota](#quota). It recognizes a usage-limit failure from the error message and cools that account down until the `Retry-After` value or reported reset time, or else for `cooldown_minutes` (default 60), never more than 7 days. The job then restarts on the next account with the partial output attached; a Codex thread isn't moved between accounts. Failover is on by default. `csa accounts reset <name> --runtime codex` clears a cooldown. Set `failover = "off"` under `[codex]` to stop at the limit instead.

## Notes

- Windows sandbox: without a `[windows] sandbox` setting in `~/.codex/config.toml`, Codex 0.162.1 has no sandbox backend (`codex doctor` says `sandbox backend: disabled`) and refuses every command in a sandboxed run, even reading a file. CLI Subagents fixes this for `read` and `edit` jobs by itself: when your Codex config sets no `[windows] sandbox`, it adds `-c windows.sandbox="unelevated"`. That needs no admin rights. Commands then run, writes inside the workspace work, and writes outside it (and outside Temp) are denied. `full` jobs bypass the sandbox and get nothing added. To change it, set `windows_sandbox` under `[codex]` in the CLI Subagents config: `"unelevated"` (default), `"elevated"` (needs admin rights once) or `"off"` (add nothing). A `[windows] sandbox` in your own Codex config always wins. `csa doctor --runtime codex` shows which one jobs will use. Details and sources: [the research note](research/codex-windows-sandbox.md).
- Network in the sandbox: Codex's `workspace-write` sandbox blocks network by default. With the `mxc` Windows sandbox that includes local sockets, so `dotnet test` hangs (VSTest's test host connects to its runner on 127.0.0.1 and gets `SocketException 10013`), and escalating the command didn't help in one case. CLI Subagents therefore gives `edit` jobs `-c sandbox_workspace_write.network_access=true`, on new runs and resumes; Claude Code edit jobs have network too. `read` jobs keep Codex's read-only sandbox, and `full` jobs have no sandbox. Set `network_access = false` under `[codex]` to keep Codex's default.
- On Windows the npm `codex` command is a `.cmd` shim. CLI Subagents runs its `codex.js` launcher with Node directly, which avoids `cmd.exe` quoting problems.
- Codex prints nothing to stdout except the JSON events, and the final answer is the last `agent_message`.
