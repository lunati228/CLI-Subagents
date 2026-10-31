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
| `edit` | Adds file edits inside the project and plainly read-only shell commands such as `git status` or `git diff`. Ordinary builds and tests inside the sandbox can pass the fixed rules. Other commands go to the [reviewer](#reviewer). |
| `full` | Passes `--auto` and omits csa's review gate. Use it only when the user explicitly requests full access. |

In `read` and `edit` jobs the gate always denies Code Mode `execute`, subagents, every MCP server except csa's own `csa-parent` channel, and any tool it has no rule for. Web fetch and web search go to the reviewer. csa doesn't start the parent session's other MCP servers for these jobs. It also turns off OpenCode's project config (`opencode.json` and `.opencode/` in the project and its parent folders), formatters and language servers, because those run project code outside the gate. Writes to `opencode.json`, `opencode.jsonc` and `.opencode/` are always denied, and so are edits to csa's own package. Your global OpenCode config still applies. Turning off project config also stops OpenCode from finding `AGENTS.md` and project skills, so csa lists them itself: every `AGENTS.md` from the working folder up to the repository root (or up to your home folder when the project is inside it), and the `.claude/skills` and `.agents/skills` folders in the working folder and its parents.

A grep over a folder is refused when any file it would search could hold secrets, such as `.env`, `*.pem` or `credentials.json` (`.git` is skipped, and so is `node_modules` when git ignores it; searches over more than 5,000 files are refused). OpenCode's grep reads hidden files and doesn't check each matching file, so csa checks first. Narrow the search with an `include` glob such as `*.py`, or search one file. In `edit` jobs, shell searches that could reach such files (`grep -r`, `rg` without file names, pipelines into a reader) go to the reviewer. Network and device paths (`\\server\share`, `\\?\`, `\\.\`, `\??\`) outside the job's folders are denied without being opened, because opening one connects to that server from your machine.

On supported Windows, OpenCode `edit` workers' shell commands run in csa's [MXC sandbox](sandbox.md). The review gate applies fixed rules, then the reviewer chain, through a per-job `tool.execute.before` plugin. Its permissions and csa's gate run inside the worker process.

The gate fails closed. OpenCode only logs a plugin that fails to load and keeps going, so csa's plugin registers the gate before it moves the session off the placeholder model. A job whose plugin didn't load or didn't finish setup ends with "the plugin did not load" and never reaches a model. A gate check that errors or runs past its time limit denies that tool call.

## Reviewer

Each reviewer is one model call with no tools. It gets the action, a few facts about the job (access level, platform, workspace folders, whether commands run in an OS sandbox, and why the fixed rules sent the action on) and the written policy shared with the Antigravity gate. It never sees your task or the conversation, and it must answer with a single JSON object.

The reviewers form a chain, shared with the Antigravity gate. By default Claude Haiku goes first, then Gemini 3.8 Flash (low) through `agy` on your primary Antigravity account. csa asks the next reviewer only when one gives no verdict: a usage limit, a missing login, CLI or account, an error, a timeout or an answer that isn't the JSON. A denial is final, and so is an approval rated high or critical risk, which counts as a denial. When no reviewer gives a verdict, the call is denied, and the reason goes back to the worker. The whole chain for one tool call gets 240 seconds, split evenly, and each reviewer gets at most 75. A shell command is reviewed every time it runs, because the scripts and manifests it runs may have changed since. Any other action is reviewed once per job, and a repeat with the same arguments (for an edit, the same content) reuses the answer. `review.jsonl` in the job folder records every decision and which reviewer made it.

If a default reviewer's CLI isn't installed or set up, it is left out quietly; `csa doctor --runtime opencode` lists it as skipped. The defaults written out, as you'd put them in `~/.cli-subagents/config.toml`:

```toml
[[review.reviewers]]          # both gates; [[opencode.reviewers]] for this gate alone
provider = "claude"           # "claude", "antigravity" or "codex"
model = "haiku"

[[review.reviewers]]
provider = "antigravity"
model = "gemini-3.8-flash-low"
```

`reviewers = []` under `[opencode]` turns the reviewers off: only the fixed rules decide, including the rule for ordinary builds and tests in sandboxed `edit` jobs. Everything the rules would send to a reviewer is denied. The older `reviewer`, `reviewer_model` and `reviewer_account` keys still work as a one-entry chain. See [Review gate](configuration.md#review-gate) for all keys and for what each provider runs, including Codex.

OpenCode can't be a reviewer. Its terms let it use unpaid-account content, reviewer calls would eat into the same per-IP daily free limit as the worker, and the worker's own model would be judging its actions. Claude goes first because a `claude -p` call without tools can't act at all, needs no prepared folder, and uses the CLI that most csa setups already run as their host. The `agy` reviewer needs Antigravity installed and logged in with a keyring login, and runs from csa's bare reviewer folder, whose gate denies it every tool.

The reviewer judges a command by its text. It sees `npm test` or `python -m pytest`, not the test files the worker may have just written, so an approved test run can execute whatever code the worker put in the project. Without an OS sandbox nothing confines that code; see [Sandbox](#sandbox).

The reviewer sees each reviewed command, file path and web request, what a reviewed edit writes, and the workspace paths. OpenCode jobs are for personal projects only. Check that your vendor rules allow sending that to each provider in your chain: Anthropic and Google by default, OpenAI with Codex. Each reviewed action usually costs one short Haiku call from your Claude plan, plus a Gemini call when Claude gives no verdict. Reads, workspace edits and read-only commands cost nothing extra.

### When commands run in a sandbox

When csa contains shell commands, the review context says `"sandbox": true`, `"sandbox_mode": "command"` and `"sandbox_network": false`. In `edit` jobs, ordinary build and test commands then pass without a reviewer call: `npm test`, `npm run build` and `pnpm`, `yarn` or `bun` scripts named test, build, lint, check, typecheck or compile (with suffixes such as `test:unit`), `pytest`, `python -m pytest` or `unittest`, `cargo build`, `test` or `check`, `go build`, `test` or `vet`, `make` with build or test targets, `mvn` compile, test, package and verify goals, `tsc`, `eslint` and similar tools. The command must be a single program run in the workspace, without chaining, pipes, redirection, variables or wildcards. Every operand must stay inside the workspace and clear of secrets files, option values must be plain words (paths go in separate arguments), and options that run other programs or load code or config (`-exec`, `--config`, `-p`, `--require`, `-f` and the like) disqualify it. So does a program file with the same name in the working folder. `gradle` and `dotnet` always go to the reviewer, because a sandboxed run can hand its work to a build daemon or compiler server that an earlier unsandboxed run left running.

Everything else follows the same rules as without a sandbox: `git push`, package installs, `npx`, secret reads and writes outside the workspace stay with the fixed rules and the reviewer. The reviewer is told that commands run sandboxed. Without the flag, build and test commands go to the reviewer as well.

Auto-approved commands use loopback-only networking. The OS blocks internet and LAN egress even when project test code tries to send data. Explicit transfers and package installs need review; approved reviewed commands get egress. Every command keeps the same workspace write limits. The gate, cache, log and reviewer calls stay outside the command container.

## Sandbox

On native Windows, csa wraps each shell invocation through `shell.create.before`, including direct shell routes. OpenCode, file tools and reviewers stay outside with normal state and provider logins. File tools rely on the gate's path checks. Read jobs cannot run commands and get no OS containment for file reads. Commands run with PowerShell, since Git Bash cannot start in MXC. Each command's descendants end with that command, its timeout or the death of its parent shell. `full` jobs omit the gate and sandbox; `[sandbox] enabled = false` turns command containment off. See [Sandbox](sandbox.md) for the layers, limits and configuration.

On macOS and Linux, csa has no sandbox yet. Install [Fence](https://github.com/fencesandbox/fence) and its OpenCode plugin, [`@fencesandbox/opencode-fence`](https://www.npmjs.com/package/@fencesandbox/opencode-fence), following Fence's own install and configuration docs. Fence is Apache-2.0 licensed and adds OS restrictions for shell commands, including write restrictions and a network allowlist. csa does not install or configure it for you. Fence requires Linux or macOS; on Windows that means running inside WSL.

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

This version uses one primary OpenCode profile, supports session follow-ups and retries clear model-capacity failures with csa's usual retry schedule. It does not support OpenCode v1, separate keyed account folders or OpenCode as a host. Before relying on an installed OpenCode version, check a small personal-project job, a denied tool call, parent messaging and a follow-up session.
