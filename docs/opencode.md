# OpenCode workers

CLI Subagents can run work in [OpenCode v2](https://opencode.ai/v2/docs/) (`@opencode/cli`). OpenCode is a worker runtime; Claude Code, Codex and Antigravity remain the supported hosts.

## Setup

Install OpenCode v2 using its own [installation instructions](https://opencode.ai/v2/docs/), then make sure `opencode` is on PATH. If you use npm, the v2 package is `@opencode/cli`; `opencode-ai` is v1. csa requires v2 and reports this when it finds v1.

There is no csa account setup. csa offers OpenCode when it finds the binary, skips it when it is missing, and finds it on later starts after you install it. It uses your own OpenCode configuration and XDG directories. Free models need no login or card. Separate keyed account folders are left for a later version.

To turn discovery off, add this to `~/.cli-subagents/config.toml`:

```toml
[opencode]
enabled = false
```

Leave `enabled` unset, or set it to `true`, to allow discovery. If detection needs help, set `binary = "C:/path/to/opencode.cmd"` under `[opencode]`.

## Choosing a model and starting a job

An explicit job `model` wins, followed by csa's `[opencode] default_model`. Otherwise csa keeps the model set in your OpenCode configuration (the `build` agent's model, then `model`). When none is set, it uses `opencode/deepseek-v4.1-flash-free`.

csa never lets a job fall back to another model. Left alone, OpenCode v2.1.4 replaces a configured default it can't use with the first available model, which for anonymous free use is Big Pickle. csa instead selects the model explicitly for the session, so an unavailable model fails the job with a message naming it. Each run starts on a placeholder model that doesn't exist, and only csa's plugin switches the session to the real one. If that plugin fails to load, the job stops before any model request. As a second check, the plugin blocks requests to Big Pickle, MiMo, Flint and the Nemotron free endpoints unless that model is the one you chose.

`effort` selects the model's variant (`provider/model#variant` in OpenCode). csa passes the effort word unchanged: `effort=max` on the default model becomes `opencode/deepseek-v4.1-flash-free#max`. Variants depend on the model; OpenCode's catalog lists `low`, `high` and `max` for DeepSeek V4.1 Flash Free. An effort the model doesn't offer fails the job with a clear message. A `#variant` in `model` works too; `effort` wins when both are given. Effort applies to your configured model as well.

From Claude Code, start `cli-subagents:cli-subagents-opencode` with the Agent tool in the background. Codex and Antigravity have matching native `cli-subagents-opencode` relays after `csa setup` for that host. The relay prompt uses the usual option lines:

```text
access=read

Review this personal project's parser and report bugs with file references.
```

Omit `model` to keep your OpenCode default. To select a model explicitly, add a leading line such as `model=opencode/deepseek-v4.1-flash-free`. Use free models for personal projects only. Follow the user's vendor and data rules before sending a task to any provider.

Other MCP clients can use `subagent_start` with `runtime: "opencode"`. From a terminal:

```bash
csa run --runtime opencode --access read "Review the parser and report bugs"
```

csa runs `opencode run --format json --standalone`, sends the task on stdin and records the session ID. Follow-ups resume that session with `-s`. The private server keeps each worker separate from your interactive OpenCode server. Text and tool progress arrive after each block or tool finishes.

## Access levels

| Access | Protection |
|---|---|
| `read` | Reads files, lists folders and searches (glob, grep) inside the project and any extra directories, except where a search could reach secrets files (see below). No edits, shell commands or subagents. |
| `edit` | Adds file edits inside the project and plainly read-only shell commands such as `git status` or `git diff`. Commands that change anything, including `npm test` and builds, are denied until OpenCode workers get a reviewer model or an OS sandbox. |
| `full` | Passes `--auto` and omits csa's review gate. Use it only when the user explicitly requests full access. |

In `read` and `edit` jobs the gate also denies web fetch and web search, Code Mode `execute`, subagents and every MCP server except csa's own `csa-parent` channel. csa doesn't start the parent's other MCP servers for these jobs. It also turns off OpenCode's project config (`opencode.json` and `.opencode/` in the project and its parent folders), formatters and language servers, because those run project code outside the gate. Writes to `opencode.json`, `opencode.jsonc` and `.opencode/` are always denied, and so are edits to csa's own package. Your global OpenCode config still applies. Turning off project config also stops OpenCode from finding `AGENTS.md` and project skills, so csa lists them itself: every `AGENTS.md` from the working folder up to the repository root (or up to your home folder when the project is inside it), and the `.claude/skills` and `.agents/skills` folders in the working folder and its parents.

A grep over a folder is refused when any file it would search could hold secrets, such as `.env`, `*.pem` or `credentials.json` (`.git` and `node_modules` are skipped, and searches over more than 5,000 files are refused). OpenCode's grep reads hidden files and doesn't check each matching file, so csa checks first. Narrow the search with an `include` glob such as `*.py`, or search one file.

OpenCode has no OS sandbox. Its permissions and csa's review gate run inside the worker process. The gate uses the same fixed rules as csa's Antigravity gate through a per-job `tool.execute.before` plugin. Actions the fixed rules cannot settle are denied. A separate reviewer model is not wired up for OpenCode in this version.

The gate fails closed. OpenCode only logs a plugin that fails to load and keeps going, so csa's plugin registers the gate before it moves the session off the placeholder model. A job whose plugin didn't load or didn't finish setup ends with "the plugin did not load" and never reaches a model. A gate check that errors denies that tool call.

## Sandbox

On macOS and Linux, install [Fence](https://github.com/fencesandbox/fence) and its OpenCode plugin, [`@fencesandbox/opencode-fence`](https://www.npmjs.com/package/@fencesandbox/opencode-fence), following Fence's own install and configuration docs. Fence is Apache-2.0 licensed and adds OS restrictions for shell commands, including write restrictions and a network allowlist. csa does not install or configure it for you.

On native Windows, no OpenCode sandbox is available in this integration yet. csa's review gate is the protection for `read` and `edit` jobs. Fence requires Linux or macOS; on Windows that means running inside WSL. A native Windows sandbox remains future work.

## Free-model limits and data terms

Free models are for personal projects only. Avoid confidential, employer or personal data. A model provider's promise about training does not remove OpenCode's own rights over unpaid content.

On 2026-10-26, `opencode models` listed these free models: `beam-free`, `big-pickle`, `deepseek-v4.1-flash-free`, `flint-40b-free`, `glm-5.3-flash-free`, `mimo-v2.6-flash-free`, `nemotron-3-ultra-free`, `nemotron-3.5-lightning-free` and `nemotron-3.5-super-vl-free` (all under the `opencode/` provider).

The [Zen privacy table](https://opencode.ai/v2/docs/console/models/#privacy), read on 2026-10-26, says the providers keep no data and don't train on it, with these exceptions:

- Big Pickle and MiMo-V2.6-Flash Free: collected data may be used to improve the model.
- Flint 40B Free: data may be used to improve the model and is kept for 30 days.
- The three Nemotron free endpoints are NVIDIA trial endpoints. NVIDIA logs use to improve its products; send no personal or confidential data.

csa never defaults to any of these, and its plugin blocks requests to them unless you chose that model.

OpenCode's own terms apply on top of the providers' promises:

- [Anomaly's Terms of Use](https://opencode.ai/legal/terms-of-service), effective 2026-08-15, allow it to use unpaid-account input and output to develop and improve its services. The [privacy policy](https://opencode.ai/legal/privacy-policy), effective 2026-03-06, gives no fixed prompt-retention period.
- The [Go documentation](https://opencode.ai/v2/docs/console/go/#enable-restricted-models) says DeepSeek's zero-retention agreement is renewed monthly and was valid through 2026-10-31. It does not establish whether that agreement also covers the free variant. Check the current terms before relying on them.

The free DeepSeek offer is temporary, with no published end date. Its daily cap is shared by IP address and resets at UTC midnight. Parallel jobs share that allowance, and extra accounts do not add quota. When OpenCode reports `provider.free-limit`, csa returns `usage_limit` with the next reset time and does not rotate accounts. It doesn't put the account on cooldown either, so jobs on a paid or keyed model keep running. Wait for the reset or choose another runtime or a paid model yourself. See the [limit research and sources](research/opencode-runtime.md#deepseek-v41-flash-terms).

## Shared setup and current limits

csa passes the parent's instruction files and skill folders through generated `OPENCODE_CONFIG_CONTENT`, and the parent's MCP servers too for `full` jobs. The `csa-parent` server has `codemode: false`, so `message_parent`, `ask_parent` and `check_parent_messages` stay visible as individual tools. This does not translate the parent's plugins into OpenCode plugins.

This version uses one primary OpenCode profile, supports session follow-ups and retries clear model-capacity failures with csa's usual retry schedule. It does not support OpenCode v1, separate keyed account folders, a reviewer model or OpenCode as a host. Before relying on an installed OpenCode version, check a small personal-project job, a denied tool call, parent messaging and a follow-up session.
