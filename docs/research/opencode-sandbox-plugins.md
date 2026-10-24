# OpenCode sandbox and permission-review plugins

Research date: 2026-10-23. A Gemini 3.8 Flash web search found the candidates; stars, licenses and last-push dates were then checked with the GitHub API (`gh api repos/...`) and npm (`npm view`). Flash got several of them wrong, so only the checked values are listed. Nothing was installed or run.

## Sandboxes

| Project | License (checked) | Stars, last push | OS | How it isolates |
|---|---|---|---|---|
| [fencesandbox/fence](https://github.com/fencesandbox/fence) with the OpenCode plugin [`@fencesandbox/opencode-fence`](https://www.npmjs.com/package/@fencesandbox/opencode-fence) 0.1.1 | Apache-2.0 | 999, 2026-10-14 | Linux, macOS; Windows only inside WSL | Hooks the bash tool, checks commands against `fence.json`, wraps them with write restrictions and a domain allowlist proxy |
| [isanchez31/opencode-sandbox-plugin](https://github.com/isanchez31/opencode-sandbox-plugin) | MIT | 66, 2026-09-19 | macOS, Linux; not Windows | `tool.execute.before` wraps bash calls with Anthropic's sandbox-runtime (Seatbelt / bubblewrap); writes only in the workspace and /tmp |
| [comsysto/opencode-sandbox](https://github.com/comsysto/opencode-sandbox) | Apache-2.0 | 22, 2026-09-11 | Windows via Docker Desktop, Linux, macOS | Runs OpenCode in a container that mounts only the project |
| Docker Sandboxes (`sbx run opencode <project>`), [docs](https://docs.docker.com/ai/sandboxes/opencode/) | Docker Desktop terms (not checked) | n/a | Windows via Docker Desktop, macOS, Linux | Whole agent in a microVM with only the project mounted (claim from Flash; docs page not checked) |
| [fmuecke/agent-win-sandbox](https://github.com/fmuecke/agent-win-sandbox) | **GPL-3.0** | 4, 2026-10-21 | native Windows | Runs the agent as a separate Windows user with ACLs around the workspace and an egress proxy (per Flash). GPL: not bundleable in csa. |
| [landstrip/landstrip](https://github.com/landstrip/landstrip) | **none on GitHub** | 88, 2026-10-23 | Windows (AppContainer/LPAC), Linux (Landlock), macOS (Seatbelt), per Flash | Cross-platform sandbox CLI. No license: not usable until one is added. |

Linux-only bubblewrap wrappers also exist (didvc/opencode-bwrap, cyunrei/opencode-bwrap); not checked.

Takeaway: nothing permissively licensed sandboxes OpenCode on native Windows today. The options are Docker/WSL based, or a csa-level wrapper (see the general sandbox research).

## Permission review (second-model judges)

| Project | License (checked) | Stars or version | What it does (per Flash, not read in source) |
|---|---|---|---|
| [hueyexe/opencode-auto-permissions](https://github.com/hueyexe/opencode-auto-permissions) | MIT | 35, 2026-09-17 | Judges `ask` requests with a second model; hard-denies catastrophic commands; falls back to asking |
| [`opencode-autoguard`](https://www.npmjs.com/package/opencode-autoguard) 0.3.0 | MIT | npm, 2026-09-03 | Rules plus an LLM judge, circuit breaker, prompt-injection defenses |
| [openguardrails/openguardrails](https://github.com/openguardrails/openguardrails) | Apache-2.0 | 59 (Flash said ~3,000), 2026-10-19 | Guardrail protocol with an OpenCode `tool.execute.before` integration |
| [jdjkelly/opencode-auto-approve](https://github.com/jdjkelly/opencode-auto-approve) | **none** | 3, 2026-09-12 | Second-model classifier for OpenCode v2; no license, so reference only |
| [`@lesquel/opencode-pilot`](https://www.npmjs.com/package/@lesquel/opencode-pilot) 1.23.1 | MIT | npm, 2026-08-24 | Remote review of permission prompts |

## Plugin hooks (per Flash; check in source before building)

- `tool.execute.before` (in `packages/plugin/src/index.ts`, anomalyco/opencode) runs before each tool call. A plugin can rewrite `output.args` (for example wrap a bash command) or throw to block the call.
- `tool.execute.after` can restore the original command text in the transcript.
- A `permission.ask` hook exists in the types, but Flash reports that core resolves permissions internally, so it may not fire reliably (issues #7006, #9229, not checked).
- Headless behaviour of `ask` rules: the source-checked note [opencode-runtime.md](opencode-runtime.md) says v2 rejects and continues and v1 stops the session. Flash's claim that it hangs is contradicted by the source reading.

## For csa

- Reviewer: csa's own Antigravity review gate (`src/cli_subagents/agy_review/`) can be reused through a small OpenCode plugin on `tool.execute.before`, or hueyexe/opencode-auto-permissions (MIT) studied for its approach.
- Sandbox: on native Windows there is no ready permissive option; see the general sandbox research for a csa-level wrapper.
