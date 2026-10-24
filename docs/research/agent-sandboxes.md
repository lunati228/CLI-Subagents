# Sandboxes for agent CLIs, and what csa could bundle

Research date: 2026-10-23. A Gemini 3.8 Flash web search collected the candidates; stars, licenses, last pushes and the key Claude Code issue were then checked with the GitHub API. Values below are the checked ones. Claims marked "per Flash" were not checked.

## What each CLI has (on the owner's native Windows 11)

| CLI | OS sandbox | Reviewer for risky actions |
|---|---|---|
| Codex | Yes: read-only and workspace-write; on Windows `unelevated`, `elevated` or `mxc` | Yes, auto-review (csa uses `--approve-for-me` for edit jobs) |
| Antigravity | Yes: `agy --sandbox` (csa uses it for edit jobs) | Its auto mode isn't available on Windows or every account; csa adds its own review gate (src/cli_subagents/agy_review/) |
| Claude Code | macOS (Seatbelt), Linux and WSL2 (bubblewrap) only; **not native Windows**. The feature request [anthropics/claude-code#46740](https://github.com/anthropics/claude-code/issues/46740) "Native sandbox support for Windows (non-WSL)" was closed as not planned (inactive) on 2026-06-21. Docs: [code.claude.com/docs/en/sandboxing](https://code.claude.com/docs/en/sandboxing). | Auto mode (csa uses it for edit jobs); csa's read-job command check |
| OpenCode | None (allow/ask/deny rules only) | None built in; see [opencode-sandbox-plugins.md](opencode-sandbox-plugins.md) |

So on native Windows the gaps are OpenCode and Claude Code.

## Candidates csa could bundle or call to wrap a whole worker process

| Project | License (checked) | Stars, last push | Windows | Notes |
|---|---|---|---|---|
| [openai/codex](https://github.com/openai/codex) `codex-rs/windows-sandbox-rs` (also `mxc-sandbox`, `windows-sandbox-service`) | Apache-2.0 | (repo), active | native | Restricted tokens, ACLs on the workspace, Job Objects; per Flash it needs admin once to set up sandbox users and then runs unprivileged. Rust: csa would ship a helper .exe per platform. |
| [anthropics/sandbox-runtime](https://github.com/anthropics/sandbox-runtime) (`srt`, with a Windows part `srt-win`) | Apache-2.0 | 5,837, 2026-10-23 | `srt-win` exists in the repo (14 code hits); maturity per Flash "alpha" | macOS Seatbelt, Linux bubblewrap; the Windows part uses restricted tokens, ACLs and the Windows Filtering Platform per Flash. Node CLI; the Windows binary is Rust. |
| [microsoft/SandboxSecurityTools](https://github.com/microsoft/SandboxSecurityTools) (`LaunchAppContainer`) | MIT | 196, 2025-05-05 | native AppContainer | No admin needed. AppContainers deny almost everything by default, so Git, Node and similar tools may fail without extra grants. Not updated since May 2025. |
| macOS `sandbox-exec` (Seatbelt) | OS component | n/a | n/a | Callable from Python with a generated profile: writes only in the project and /tmp, network on. |
| [containers/bubblewrap](https://github.com/containers/bubblewrap) | LGPL-2.1 (per Flash) | (not checked) | Linux only | Call the installed `bwrap`; don't vendor it. |
| [dagger/container-use](https://github.com/dagger/container-use) | Apache-2.0 | 4,079, 2026-09-21 | via Docker/WSL2 | Containers plus git worktrees, as an MCP server |
| [devcontainers/cli](https://github.com/devcontainers/cli) | MIT | 3,003, 2026-10-22 | via Docker | Whole session in a container |
| [e2b-dev/E2B](https://github.com/e2b-dev/E2B) | Apache-2.0 | 14,522, 2026-10-23 | any (cloud) | Remote microVMs: code leaves the PC |
| [daytonaio/daytona](https://github.com/daytonaio/daytona) | none detected by GitHub | 71,499, 2026-07-24 | | Workspaces; check the license before use |
| [docker/sbx-releases](https://github.com/docker/sbx-releases) (Docker Sandboxes) | not an open license (NOASSERTION) | 470, 2026-10-23 | via Docker Desktop | MicroVM per workspace |
| [yourtablecloth/WindowsSandboxMcp](https://github.com/yourtablecloth/WindowsSandboxMcp) | Apache-2.0 | 15, 2025-10-22 | Windows Sandbox (24H2) | Disposable VM; tools must be reinstalled each time |
| [fmuecke/claude-win-sandbox](https://github.com/fmuecke/claude-win-sandbox), [fmuecke/agent-win-sandbox](https://github.com/fmuecke/agent-win-sandbox) | agent-win-sandbox is GPL-3.0 (claude-win-sandbox not checked) | 4 (agent-win-sandbox) | native | Low-privilege local user plus ACLs; GPL, so reference only |
| [streamingfast/sbox](https://github.com/streamingfast/sbox) | none (Flash said Apache-2.0) | 11, 2026-05-12 | via Docker/WSL2 | Not usable without a license |
| [trekhleb/claude-pod](https://github.com/trekhleb/claude-pod) | MIT | 41, 2026-06-23 | via Docker | Docker profile for Claude Code |

Hook-based guards that aren't OS sandboxes: `hookify`, `security-guidance` and `claude-security` exist in [anthropics/claude-plugins-official](https://github.com/anthropics/claude-plugins-official) (checked: the folders exist; behaviour not read).

## Direction for csa (not decided)

- Keep each CLI's own sandbox where it has one (Codex, Antigravity, Claude Code on macOS/Linux).
- Where it's missing (OpenCode everywhere, Claude Code on native Windows), wrap the whole worker process: macOS `sandbox-exec`, Linux `bwrap`, and on Windows a helper built from Codex's Apache-2.0 Windows sandbox or `srt-win`. Writes only to the project and temp, network on.
- Open questions before building: whether the Windows helper needs admin once (and how `csa setup` asks), how agent homes and caches (npm, pip, ~/.claude) stay writable, and how to ship a native helper from a stdlib-only Python package.
