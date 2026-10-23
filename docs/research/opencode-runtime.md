# OpenCode as a fourth runtime

Accessed 2026-10-23. Repository: github.com/anomalyco/opencode (github.com/sst/opencode redirects there), MIT, default branch `dev`. Two tags were shallow-cloned into a scratch folder and read, then deleted: `v1.19.4` (commit c0c7bae, GitHub "Latest" release, 2026-10-22) and `v2.1.4` (2026-10-23). Nothing was installed or run, and no model was called. OpenCode is not installed on this machine.

Labels used below:

* [src v1] / [src v2]: read in that tag's source, file path given.
* [docs]: stated on opencode.ai or in OpenCode's own pages, not checked in code.
* [third party]: Reddit, Hacker News, blogs, GitHub issue comments.

The web facts (free models, terms, community reports) came from a Codex research job (Sol 6.1, read only) on the same day. I spot-checked the parts that touch code (the anonymous `public` key, the free-limit error) in source.

## Summary

Feasible, and simpler than the Antigravity adapter. `opencode run --format json` prints one JSON object per line, tags every line with the session id, reports tokens and cost per step, gives errors a typed `error.type` such as `provider.free-limit`, and continues or creates a session with `-s <id>`. Config can be passed whole through `OPENCODE_CONFIG_CONTENT`, and all global state moves with the XDG variables, so a per-account home needs no file syncing.

Two things limit the payoff:

1. The free models are anonymous and limited per IP address per day. Extra csa "accounts" add no free quota; parallel jobs share one allowance. Rotation only helps for accounts that carry a key (Zen balance, OpenCode Go, or a provider key).
2. OpenCode is mid-migration from v1 to v2. Both are maintained, the JSON output is the same shape in both, but errors, defaults, server model and instruction files differ. The adapter has to handle both, or pin one.

Recommendation: build a small first version (one account, free models, read and edit access, JSON parsing, resume), then decide on multi-account and host support once it has run real jobs. Estimate below.

## 1. What OpenCode is today

* License MIT [src v2 `packages/cli/package.json`; GitHub API]. Operated by Anomaly Innovations, Inc. [docs: Terms of Use, effective 2026-08-15, https://opencode.ai/legal/terms-of-service]. About 220k stars, pushed 2026-10-23, not archived [GitHub API].
* Two release lines:
  * v1: npm package `opencode-ai`, source in `packages/opencode`. v1.19.4 is GitHub's "Latest" (2026-10-22). A repository member called v1 critical-fix-only (https://github.com/anomalyco/opencode/issues/54338, 2026-10-10) [third party, maintainer].
  * v2: npm package `@opencode/cli` (version 2.1.4), source in `packages/cli` and `packages/core` [src v2]. Tags v2.1.0 to v2.1.4 exist; the changelog shows v2.1.2, v2.1.3 and v2.1.4 on 2026-10-21, 22 and 23 (https://opencode.ai/changelog) [docs]. GitHub has no release object for v2 tags yet [GitHub API].
* Windows install:
  * v1: `npm install -g opencode-ai`, `scoop install opencode`, `choco install opencode`, or release binaries (https://opencode.ai/docs/#windows) [docs].
  * v2: `npm install -g @opencode/cli` or standalone binaries for x64, x64-baseline and ARM64; the v2 docs say Windows package managers are not supported (https://opencode.ai/v2/docs/) [docs]. The npm bin `opencode.cjs` is a Node shim that spawns the platform binary `opencode.exe` from `@opencode/cli-windows-x64` and forwards SIGINT, SIGTERM and SIGHUP [src v2 `packages/cli/bin/opencode.cjs`]. So the process csa starts is `node`, with the real CLI as its child: kill the tree on timeout, as for Codex.

## 2. Headless use

### Command line

v2 `opencode run [message..]` [src v2 `packages/cli/src/commands/commands.ts` 389-430]:

| Flag | Meaning |
|---|---|
| `-c, --continue` | continue the last session in this directory |
| `-s, --session <id>` | "Session ID to continue, or to create if it does not exist" |
| `--fork` | fork before continuing (needs `-c` or `-s`) |
| `-m, --model provider/model#variant` | model; the `#variant` suffix is the reasoning level |
| `--agent <name>` | agent (`build` default, `plan`, custom) |
| `--format default\|json` | output format |
| `-f, --file` | attach files (up to 100, 10 MiB each) |
| `--title`, `--thinking` | session title; print reasoning blocks |
| `--auto` | "Auto-approve permissions that are not explicitly denied" (hidden aliases `--yolo`, `--dangerously-skip-permissions`) |
| `--standalone` | "Run with a private server instead of the background service" |
| `--server <url>` | use a running server |

Piped stdin is appended to the message (`mergeInput`) [src v2 `run/run.ts`], so csa can send the prompt on stdin as it does for Codex. There is no `--cwd`: run uses the process working directory (`localDirectory(process.env.PWD ?? process.cwd())`).

v1 `opencode run` has the same core flags plus `--variant`, `--dir`, `--attach`, `--port`, `--command`, `--auto` and `--dangerously-skip-permissions` [src v1 `packages/opencode/src/cli/cmd/run.ts` 139-260]. v1 runs its server in-process.

v2 needs care: by default `run` connects to (or starts) a shared background service on a fixed port per channel, 0x70DE = 28894 on Windows, registered in `service.json` [src v2 `services/server-connection.ts`, `services/service-config.ts`]. Several csa workers, or a worker and the user's own TUI, would share one server. `--standalone` starts a private `opencode serve --stdio --port 0` child with a random password; the child inherits the environment and exits when its stdin lease closes [src v2 `services/standalone.ts`]. csa should always pass `--standalone` on v2.

### JSON events

`--format json` writes one line per event: `{"type", "timestamp", "sessionID", ...}` [src v2 `run/noninteractive.ts` `emit`; src v1 `run.ts` 668-676]. Same types in v1 and v2:

| type | payload | notes |
|---|---|---|
| `step_start` | `part` {id, messageID, type:"step-start", snapshot} | one per model step |
| `text` | `part` {text, time{start,end}} | emitted once per finished text block, no deltas |
| `reasoning` | `part` {text, metadata} | only with `--thinking` |
| `tool_use` | `part` {tool, state{status:"completed"\|"error", input, output\|error, title, metadata, time}} | only finished tool calls |
| `step_finish` | `part` {reason, cost, tokens} | per-step usage and cost |
| `error` | `error` | see below |

There is no final "result" event. The answer is the last `text` part (v2 also re-reads the stored messages at the end and prints any text the stream missed, `reconcile()`). Live progress is coarse: text arrives only when a block ends, and a tool appears only after it finishes.

Error shape differs:

* v2: `{"type":"error", "error":{"type":"provider.free-limit"|"provider.rate-limit"|"provider.quota"|"provider.auth"|"provider.timeout"|"provider.internal"|"provider.no-route"|"permission.rejected"|"aborted"|"unknown"|..., "message", "status"?, "retryAfterMs"?, "response"?{body}}}` [src v2 `packages/core/src/session/to-session-error.ts`]. A free-limit error has the message "Free usage limit reached. Connect a provider or subscribe to OpenCode Go to keep going, or switch models." and is detected from `FreeUsageLimitError` in the response body. Errors before a session exists come as `{"type":"error","error":{"type":"unknown","message":...}}` with an empty `sessionID` (`reportRunError`).
* v1: `{"type":"error","error":{"name":"APIError"|..., "data":{"message", "statusCode"?, ...}}}` [src v1 `run.ts` 772-780].

### Exit codes and timeouts

v2 [src v2 `run/noninteractive.ts`, `run/run.ts`]: 0 on success; 1 on any error event, a run error, or a cancelled form outside v1 mode; 130 after SIGINT. v1 sets 1 when the session reported an error and calls `process.exit(1)` for argument errors [src v1 `run.ts`].

No run timeout flag exists. v2 uses `noTimeoutFetch` so a long session is not cut at Bun's five-minute default [src v2 `run/run.ts`]. Model retries: exponential backoff from 2 s, capped at 10 s per gap, up to 10 retries (about 84 s), with server `retry-after` honored up to 15 minutes; timed-out attempts retry at most 3 times [src v2 `packages/core/src/model-retry.ts`]. csa's own `timeout_seconds` remains the outer limit.

## 3. Models and auth

### Model ids

`-m provider/model`, v2 adds `#variant` (`parseSessionTargetModel`) [src v2]. Free Zen models use the `opencode/` provider. `opencode models` lists them.

### Free models

Listed at zero price in the Zen model docs (https://opencode.ai/v2/docs/console/models/, undated) [docs]: `opencode/big-pickle`, `glm-5.3-flash-free`, `deepseek-v4.1-flash-free`, `beam-free`, `flint-40b-free`, `longcat-2.5-preview-free`, `mimo-v2.6-flash-free`, `nemotron-3-ultra-free`, `nemotron-3.5-lightning-free`, `nemotron-3.5-super-vl-free`, `jev-1.13-free`. The public endpoint https://opencode.ai/inference/v1/models returned nine of them (no `longcat-2.5-preview-free`, no `nemotron-3-ultra-free`).

How anonymous use works [src v2 `packages/core/src/plugin/provider/opencode.ts` ~465-500]: with no `OPENCODE_API_KEY`, no Console connection and no configured key, the `opencode` provider is enabled with `apiKey: "public"` and every model with a non-zero input price is disabled. `OPENCODE_DISABLE_FREE_MODELS=1` turns this off. v1 does the same in `provider.ts` (cited by the research job, https://raw.githubusercontent.com/anomalyco/opencode/v1.19.4/packages/opencode/src/provider/provider.ts). No login, account or card is needed; the homepage FAQ says the same [docs: https://opencode.ai/].

### DeepSeek V4.1 Flash terms

* Id `opencode/deepseek-v4.1-flash-free`, free "for a limited time", no end date published. Paid variants are `opencode/deepseek-v4.1-flash` (Zen) and `opencode-go/deepseek-v4.1-flash` (Go subscription) [docs: Zen models page].
* Free use is allowed only from OpenCode itself. Other clients get HTTP 403 `FreeTierError`, "OpenCode's free tier can only be used from within OpenCode" (collaborator reply, https://github.com/anomalyco/opencode/issues/49621, 2026-09-17/18) [third party, maintainer]. csa runs the official CLI, so it stays inside that rule.
* Context: 200K on the free variant versus 1M paid, per a blog (https://sobolewski.pro/en/blog/deepseek-free-opencode, 2026-10-16) [third party].
* Rate limit: counted per IP per day with a reset at UTC midnight, numbers from runtime config, not published (https://raw.githubusercontent.com/anomalyco/opencode/v1.19.4/packages/console/app/src/routes/zen/util/ipRateLimiter.ts) [src, console code, deployed values unknown]. Free models never draw on Zen balance, and adding credit does not lift the cap (member reply, https://github.com/anomalyco/opencode/issues/45324, 2026-10-19) [third party, maintainer]. Users reported the cap getting much tighter on 2026-10-22/23, some after one or two prompts (https://github.com/anomalyco/opencode/issues/58205; https://www.reddit.com/r/opencode/comments/1xd60rj/free_tier_is_dead/) [third party].

### Data use

* Per-model privacy table on the Zen models page (https://opencode.ai/v2/docs/console/models/#privacy) [docs]: DeepSeek V4.1 Flash, GLM 5.3 Flash and LongCat 2.5 Preview: the provider has a "zero-retention policy and does not use your data for model training". Big Pickle and MiMo V2.6 Flash: "collected data may be used to improve the model", retention not stated. Flint 40B: improvement allowed, 30-day retention. Beam: no training, 30-day safety retention. Nemotron: NVIDIA trial endpoints log usage; no confidential or personal data.
* OpenCode's own terms are broader. Terms of Use, "Your IP": "if you are using the Services through an unpaid account, we may use Content to further develop and improve our Services" [docs, effective 2026-08-15]. Content covers input and output. So the provider promise (DeepSeek does not train) and Anomaly's right to use free-tier content for its services both apply.
* DeepSeek's zero-retention agreement "is renewed monthly. The current agreement is valid through October 31, 2026." That note sits on the Go page (https://opencode.ai/v2/docs/console/go/#enable-restricted-models) [docs]; whether the same contract covers the free variant is not stated. The Zen page says all Zen models are hosted in the US and EU, while the Go page needs "Global" regions enabled for DeepSeek, so the inference location for free DeepSeek is unclear.
* Privacy policy (effective 2026-03-06, https://opencode.ai/legal/privacy-policy) gives no fixed prompt retention period [docs].

For csa this means: free DeepSeek is fine for personal, public code. It is not for work code that isn't cleared for outside model providers, and csa should not default to Big Pickle or MiMo, which may train on the data.

### Where state lives

v2 global paths are `<XDG_DATA_HOME|~/.local/share>/opencode`, `<XDG_CONFIG_HOME|~/.config>/opencode`, `<XDG_CACHE_HOME|~/.cache>/opencode`, `<XDG_STATE_HOME|~/.local/state>/opencode`, on Windows too [src v2 `packages/util/src/global-roots.ts`]. `opencode debug paths` prints them. Sessions and credentials live in a SQLite file `opencode.db` in the data dir (`OPENCODE_DB` overrides it) [src v2 `cli/src/database-path.ts`]; a migration imports v1's `auth.json` into it [src v2 `core/src/database/migration/20260805200742_import_legacy_credentials.ts`]. v2 `auth` has `list`, `login`, `logout`, `export`, `import` and `switch`, so one home can hold several credentials [src v2 commands.ts 157-234].

v1 uses the same XDG roots through `xdg-basedir`, with credentials in `<data>/auth.json` [src v1 `packages/core/src/global.ts`, `packages/opencode/src/auth/index.ts`].

### Several accounts side by side

Setting `XDG_DATA_HOME`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME` and `XDG_CACHE_HOME` per account gives each account its own credentials, sessions and service registration. `os.homedir()` is untouched, so `~/.claude` and `~/.agents` (skills) are still found. This is the same shape as csa's per-account homes, minus the syncing: the shared setup can arrive through `OPENCODE_CONFIG_CONTENT`. For anonymous free use, accounts are pointless (quota is per IP). They matter only for keyed accounts (Zen API key in `OPENCODE_API_KEY` or `auth login`, Go subscription, other providers).

## 4. Permissions and safety

### Rules

v2 rules are an ordered list `{action, resource, effect: allow|ask|deny}`; the last match wins; no match means `ask` [src v2 `packages/core/src/permission.ts` `evaluate`]. The default agent starts with `* * allow`, plus `ask` for `*.env` and `*.env.*` and `allow` for `*.env.example` [src v2 `packages/schema/src/agent.ts` `Info.default`]. Action names: `edit` covers the edit, write and patch tools [src v2 `core/src/tool/plugin/{edit,write,patch}.ts`]; `shell` (v1 `bash`) takes one resource per parsed command, from a tree-sitter scan of the command line [src v2 `tool/plugin/shell.ts`]; also `read`, `grep`, `glob`, `webfetch`, `websearch`, `subagent` (v1 `task`), `skill`, `todo`, `question`, `external_directory`. The `plan` agent denies `edit` except inside `~/.opencode/plan` [src v2 `core/src/plugin/plan.ts`]. Plugins get a `permission.evaluate` hook that can change the effect [src v2 `permission.ts`].

v1 uses a map: `"permission": {"edit": "deny", "bash": {"*": "ask", "git status*": "allow"}}`. Defaults are `"*": "allow"` with `doom_loop` and `external_directory` on `ask` [src v1 `agent/agent.ts` ~120]. `OPENCODE_PERMISSION` (JSON) is also read [src v1 flags]. v2 accepts the v1 format and translates it (`migratePermissions`, `bash` becomes `shell`) [src v2 `core/src/config/normalize.ts` 562-588, `v1/config/migrate.ts`].

### What happens to "ask" in headless mode

This was open in [cli-permissions-and-headless.md](cli-permissions-and-headless.md); it is now answered from source:

* v2 without `--auto`: every `permission.asked` for this session or its child sessions is answered `reject` with the message "This non-interactive run cannot ask the user for permission, so the request was rejected. Continue without this action." The tool fails, the model sees the message and carries on. Questions (the `question` tool) are cancelled the same way [src v2 `run/noninteractive.ts` 70-207]. With `--auto` it answers `once`.
* v1 without `--auto`: replies `reject` with no message [src v1 `run.ts` 791-813], and the session loop stops on a denial unless `experimental.continue_loop_on_deny` is true [src v1 `session/processor.ts` 647]. csa should set that flag on v1, or a single denied call ends the job.

### Sandbox

None. No OS sandbox exists in either line; the only "sandbox" strings in v2 core are a database column and a comment [src v2 grep]. Restrictions are permission rules only, enforced inside the agent.

### Mapping csa access levels

| csa | OpenCode |
|---|---|
| `read` | `permission: {"edit": "deny", "bash": "deny", "task": "deny"}` (v1 names; v2 translates), or a narrow shell allowlist (`git status*`, `git diff*`, `git log*`, `rg *`, ...) once tested. No `--auto`. Asks are rejected anyway. A `tool.execute.before` plugin could later run csa's read-safety checks (see [read-safety.md](read-safety.md)). |
| `edit` | defaults (all allowed, `.env` reads asked and therefore rejected) plus `external_directory: "ask"` so writes outside the project are refused; no `--auto`. |
| `full` | `--auto` (v2 and v1). |

Since there is no sandbox, `edit` here is weaker than Codex's workspace-write: shell commands can still write anywhere. Say so in the docs and in `csa doctor`.

## 5. Sessions

* Every JSON line carries `sessionID`, so the parser has it from the first event.
* Follow-up: `opencode run -s <id> --format json` with the new message [src v2 `session-target.ts`; src v1 `run.ts`]. On v2 an unknown id is created with that id, so csa could even choose the id (it must start with `ses`) [src v2 `cli/src/session-target.ts`, `schema/src/session-id.ts`]. `--fork` branches a session.
* Sessions live in the account's database, so resume works only in the account that ran the job, which csa already tracks with `resume_account`. Failover to another account could use `opencode session export <id>` and `session import <file>` [src v2 commands.ts 457-483], but that is untested.

## 6. Setup sharing

* Inline config: `OPENCODE_CONFIG_CONTENT` (JSON text) is merged above project files and below managed settings; `OPENCODE_CONFIG` names a file; `OPENCODE_CONFIG_DIR` adds a config directory [src v2 `core/src/config.ts` 225-300, `cli/src/server-process.ts` 131; src v1 flags]. `{env:VAR}` and `{file:path}` are substituted [src v2 `core/src/config/variable.ts`], so secrets can go by variable name as csa does for Codex.
* MCP format. v1: `"mcp": {"name": {"type": "local", "command": ["cmd", "arg"], "environment": {...}}}` or `{"type": "remote", "url", "headers"}`. v2 native: `"mcp": {"servers": {...}}` with the same entries plus `cwd`, `timeout`, `protocol` and `codemode` [src v2 `schema/src/mcp.ts`]; v2 also accepts the v1 layout [src v2 `normalize.ts` 266-301]. A `to_opencode()` in `mcp_translate.py` next to `to_codex()` and `to_agy()` covers it; the csa-parent server goes in the same block.
* v2 exposes MCP tools through "Code Mode" by default (`codemode` defaults to true) [src v2 `schema/src/mcp.ts`]. For `csa-parent` set `codemode: false` so the model sees `message_parent` and `ask_parent` as plain tools. Untested.
* Instructions. v1 reads project `AGENTS.md`, else `CLAUDE.md`, and global `~/.config/opencode/AGENTS.md`, else `~/.claude/CLAUDE.md` (`OPENCODE_DISABLE_CLAUDE_CODE_PROMPT` turns the Claude ones off) [src v1 `session/instruction.ts` 62-66]. v2 reads only `AGENTS.md` (global config dir and walking up from the project) plus the `instructions` array [src v2 `core/src/config/plugin/instruction.ts`; no `CLAUDE.md` in v2 core]. csa should list the parent's instruction files in `instructions` for both, which also fits its existing "parent note".
* Skills: v2 loads `skills/` from `~/.claude`, `~/.agents` and project `.claude` / `.agents` [src v2 `config/discovery.ts`, `config/plugin/compatibility.ts`], so the user's Claude skills are found with no copying. `skills` in config adds paths. v1 has the same with `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS` as the off switch.
* Agents: markdown files in `agent/`, `agents/`, `mode/` or `modes/` inside a config dir, or `agents` (v1 `agent`) in config [src v2 `config/plugin/agent.ts` 26, 84-127].
* Updates: set `"autoupdate": false` (v1 key, v2 maps it to `update`) or `OPENCODE_DISABLE_AUTOUPDATE=1` (v1) in the worker config so a job never self-updates.

## 7. Usage visibility

* Per step: `step_finish.part.tokens` and `cost` [src v1/v2]. Free models report cost 0.
* Limits: the v2 error type tells free-limit (`provider.free-limit`), provider rate limit (`provider.rate-limit`, with `retryAfterMs` when the server sends one), paid quota (`provider.quota`) and auth (`provider.auth`) apart. On v1 the research job found the 429 text "Rate limit exceeded. Please try again later." with `FreeUsageLimitError` in the body (https://github.com/anomalyco/opencode/issues/32846, 2026-06-18) [third party].
* No endpoint or CLI command shows the remaining free allowance [research job; nothing found in v2 CLI commands]. The free reset is UTC midnight (limiter code above), so csa can set `resets_at` to the next UTC midnight on `provider.free-limit` when no `retryAfterMs` is given.
* `opencode stats` exists in v2 (local usage summary) [src v2 commands.ts 322]; not read further.

## 8. OpenCode as a host

Possible, not needed for the first version.

* MCP: OpenCode runs local MCP servers, so `csa mcp` works as an `mcp` entry, and the MCP tools (`subagent_start` and so on) are callable.
* Relay agents: an OpenCode subagent is a markdown agent with `mode: subagent`, a prompt and permissions. A relay would be such an agent that runs `csa run --follow ...` through the `shell` tool, like the Codex and Antigravity relays. Its child session shows in the TUI as a subagent run. A long `csa run --follow` inside one shell call would hit the shell tool's timeout; this needs testing.
* Nesting: `experimental.subagent_depth` defaults to 1 [src v2 `schema/src/config/experimental.ts`], which matches csa's depth rules.
* Plugins (JS/TS, `tool.execute.before`, `permission.evaluate`) could replace the wake-up hooks Claude Code uses.

## 9. Porting estimate

### Adapter methods

| csa piece | OpenCode | Difficulty |
|---|---|---|
| `find_binary` | `opencode` on PATH (npm shim, scoop/choco, or binary); config `binary` override | easy |
| `version` | `opencode --version`; branch on major version | easy |
| `auth_status` | anonymous free models always "ready"; keyed accounts via `opencode auth list --format json` (v2) or presence of `auth.json` (v1) | easy for free, medium for keyed |
| `account_env` | `XDG_DATA_HOME`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME` under the account folder; strip inherited `OPENCODE_API_KEY`, `OPENCODE_PASSWORD`, `OPENCODE_SERVER_PASSWORD`, `OPENCODE_CONFIG*` from the host | easy |
| `login_command` | `opencode auth login` with the account env | easy |
| `prepare` | nothing to sync; build the config JSON | easy |
| `launch` | `opencode run --format json [--standalone] [-s id] -m ... [--thinking] [--auto]`, prompt on stdin, `cwd=spec.cwd`, `OPENCODE_CONFIG_CONTENT` with permissions, MCP (parent servers plus csa-parent), `instructions`, `autoupdate: false`, v1 `experimental.continue_loop_on_deny: true` | medium |
| effort | v2 `-m provider/model#variant`, v1 `--variant`; variant names depend on the model | easy, needs a model table |
| `add_dirs` | no flag; `external_directory` allow rules for those paths | easy |
| parser `feed` | `text` sets `last_text`, `tool_use` counts tools, `step_finish` sums tokens and cost, `reasoning` for live reasoning, `error` collected | easy |
| parser `finish` | success = exit 0 and no `error`; classify `provider.free-limit` / `provider.rate-limit` / `provider.quota` as `usage_limit`, `provider.auth` as `auth`, `provider.internal` with overload text as capacity; v1 by `error.name` and `data.message` | medium (two shapes) |
| `max_turns` | `agents.<name>.steps` in config | easy |
| relay agent | `agents/cli-subagents-opencode.md` plus follow support | easy, copies Codex |
| host install | `csa setup --host opencode` writing MCP config and agent files | later |

### Hard parts and risks

* Two versions. v1 is "Latest" on GitHub and npm `opencode-ai`; v2 is a new package with a background service. Supporting both doubles the error mapping and some flags. Pinning v2 alone is cleaner but may not match what a user has installed.
* Free-tier volatility. The DeepSeek promotion is time-limited and the cap changed sharply this week. A job may fail on the first prompt. Since every account shares the IP quota, failover must not rotate through accounts on `provider.free-limit`; it should fall back to another runtime or wait for UTC midnight.
* Data terms. Anomaly may use unpaid-account content. Make the runtime opt-in, and keep it off for work projects (the user's rules already require this).
* No sandbox. `read` relies on deny rules and asks being rejected; `edit` cannot confine shell writes.
* Coarse progress: no text deltas; tools appear only when finished.
* Windows process tree: Node shim, native binary, and on v2 a `serve` child. The serve child ends on stdin EOF, but csa should still kill the tree.
* Background service on v2: forgetting `--standalone` would mix workers into the user's TUI server and share its environment. Cover with a test that the flag is always there.
* Code Mode wrapping MCP tools on v2 may hide csa-parent tools from the model.

### Size

| Change | Files | Rough lines |
|---|---|---|
| `src/cli_subagents/runtimes/opencode.py` (runtime, parser, config builder, v1/v2 branches) | 1 | 450-550 |
| `mcp_translate.py`: `to_opencode()` | 1 | 50-80 |
| Registration: `runtimes/__init__.py`, `config.py`, `cli.py`, `setup.py`, `hosts.py`, `follow.py`, `rules.py`, `limits.py`, `plans.py`, `tools.py` | 10 | 120-200 |
| Relay agent `agents/cli-subagents-opencode.md`, skill and docs updates | 3-5 | 100-150 |
| `tests/fake_opencode.py` (emits v1 and v2 JSON, honors `-s`, exit codes, free-limit error) | 1 | 100-150 |
| `tests/test_opencode.py` (argv per access level, env isolation, config JSON, parser outcomes for both shapes, resume, MCP translation) | 1-2 | 250-350 |

Total about 1,100 to 1,500 lines across 17 to 20 files, plus a version bump in the three version files. Roughly two to four working days including a live check with one free model, about the same as the Codex adapter.

### Recommended plan

1. First version: runtime `opencode`, one account (the user's own XDG dirs, no account folders), v2 only with `--standalone` and a clear error on v1, default model `opencode/deepseek-v4.1-flash-free`, access `read` and `edit` (and `full` via `--auto` when asked), prompt on stdin, JSON parser, outcome mapping, follow-ups with `-s`, csa-parent plus parent MCP servers through `OPENCODE_CONFIG_CONTENT`, instruction files through `instructions`. Tests with a fake CLI. One live run with the free model, with the user's consent, to confirm the event stream and the free-limit error text.
2. Then: relay agent `cli-subagents-opencode` and the delegation-rules template entry, so Claude Code can start OpenCode jobs as it does Codex.
3. Then: v1 support (error shape, `continue_loop_on_deny`, `--variant`), if the user's install or users ask for it.
4. Then: keyed accounts with per-account XDG homes, `opencode auth login` in `csa accounts login`, and failover that skips rotation for free-limit errors.
5. Later: read-safety plugin (`tool.execute.before`), session export/import for cross-account failover, OpenCode as a host.

## Sources

All accessed 2026-10-23.

Source code (shallow clones of the tags, deleted after reading):

* v2.1.4: `packages/cli/src/commands/commands.ts`, `run/run.ts`, `run/noninteractive.ts`, `run/v1.ts`, `services/server-connection.ts`, `services/service-config.ts`, `services/standalone.ts`, `session-target.ts`, `database-path.ts`, `env.ts`, `bin/opencode.cjs`; `packages/core/src/permission.ts`, `plugin/agent.ts`, `plugin/plan.ts`, `config.ts`, `config/discovery.ts`, `config/normalize.ts`, `config/variable.ts`, `config/plugin/{agent,instruction,compatibility}.ts`, `plugin/provider/opencode.ts`, `model-retry.ts`, `session/to-session-error.ts`, `file-access.ts`, `tool/plugin/{shell,edit,write,patch}.ts`; `packages/schema/src/{config,agent,permission,mcp,session-id}.ts`, `config/experimental.ts`; `packages/util/src/global-roots.ts`. Base URL https://github.com/anomalyco/opencode/tree/v2.1.4
* v1.19.4: `packages/opencode/src/cli/cmd/run.ts`, `session/processor.ts`, `session/instruction.ts`, `agent/agent.ts`, `auth/index.ts`, `effect/runtime-flags.ts`; `packages/core/src/global.ts`. Base URL https://github.com/anomalyco/opencode/tree/v1.19.4
* Console limiter: https://raw.githubusercontent.com/anomalyco/opencode/v1.19.4/packages/console/app/src/routes/zen/util/ipRateLimiter.ts and `packages/console/core/src/subscription.ts` (via the research job)

OpenCode pages:

* https://opencode.ai/ (FAQ, free models without an account)
* https://opencode.ai/zen
* https://opencode.ai/v2/docs/ and https://opencode.ai/docs/#windows (install)
* https://opencode.ai/v2/docs/console/models/ and #privacy (free models, privacy table)
* https://opencode.ai/v2/docs/console/go/#enable-restricted-models (DeepSeek ZDR note)
* https://opencode.ai/v2/docs/models/#local
* https://opencode.ai/changelog
* https://opencode.ai/legal/terms-of-service (effective 2026-08-15)
* https://opencode.ai/legal/privacy-policy (effective 2026-03-06)
* https://opencode.ai/inference/v1/models
* https://github.com/anomalyco/opencode/releases/latest and https://github.com/anomalyco/opencode/releases/tag/v2.1.4

Issues and community:

* https://github.com/anomalyco/opencode/issues/49621 (free tier only inside OpenCode, 2026-09-17/18)
* https://github.com/anomalyco/opencode/issues/45324 (free allowance separate from Zen balance, 2026-10-19)
* https://github.com/anomalyco/opencode/issues/58205 (free limit hit early, 2026-10-22)
* https://github.com/anomalyco/opencode/issues/54338 (v1 critical fixes only, 2026-10-10)
* https://github.com/anomalyco/opencode/issues/32846 (`FreeUsageLimitError` 429, 2026-06-18)
* https://github.com/anomalyco/opencode/issues/24649 (custom provider agreements, 2026-04-27 / 05-21)
* https://sobolewski.pro/en/blog/deepseek-free-opencode (2026-10-16)
* https://www.reddit.com/r/opencode/comments/1x6fj9m/deepseek_v41_flash_is_free_in_opencode/ (2026-10-15)
* https://www.reddit.com/r/opencode/comments/1xcw4h2/free_usage_limited_reached_on_opencode_zen_for/ (2026-10-22/23)
* https://www.reddit.com/r/opencode/comments/1xd60rj/free_tier_is_dead/ (2026-10-23)
* https://news.ycombinator.com/item?id=50130375 and https://news.ycombinator.com/item?id=50159334 (dated "5 days ago" and "3 days ago")
* https://openrouter.ai/openrouter/free (another free route, `openrouter/openrouter/free`)
