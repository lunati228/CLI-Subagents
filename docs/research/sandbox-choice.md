# One sandbox for Claude Code, csa workers and OpenCode

Research date: 2026-10-24, by a Codex Sol 6.1 high read-only job (sources read 2026-10-24; it marked what it read in code versus docs). Key facts re-checked by the main session: microsoft/mxc exists, MIT, 2,983 stars, pushed 2026-10-24, releases v0.9.0 (2026-09-29), v1.0.0 (2026-10-07) and v1.0.1 (2026-10-17) with `mxc-release-binaries.zip`. The owner's PC runs Windows 10.0.26300.9711. The owner's Codex config already uses `[windows] sandbox = "mxc"`, the Codex adapter for the same engine.

Earlier inputs: [agent-sandboxes.md](agent-sandboxes.md), [opencode-sandbox-plugins.md](opencode-sandbox-plugins.md).

## Recommendation: Microsoft MXC

[github.com/microsoft/mxc](https://github.com/microsoft/mxc), MIT.

- Native Windows "ProcessContainer" (PSEC) backend: no sandbox user accounts, no admin prompt per launch, no driver. Also bubblewrap (Linux) and Seatbelt (macOS 15+) backends, so one policy format on all three systems.
- Standalone launcher: `wxc-exec.exe policy.json -- executable args...` ([source](https://github.com/microsoft/mxc/blob/v1.0.1/src/tools/wxc/src/main.rs)). Explicit filesystem grants and denials, per-container network policy, the whole process tree owned and cleaned up.
- Supported Windows: 11 24H2 build 26100.9278 and 25H2 26200.9278 or later ([OS matrix](https://github.com/microsoft/mxc/blob/v1.0.1/docs/backends/process-container/os-version-support.md)); a runtime capability probe is authoritative.
- With v1.0.1: set `fallback.allowDaclMutation=false` and require probe tier `base-container`, so it never silently falls back to an older AppContainer mode. Pin the tested release.

## How each part would use it

1. Claude Code (CLI and the desktop app's Code tab): a plugin with a synchronous `PreToolUse` hook on `Bash|PowerShell` that returns `hookSpecificOutput.updatedInput` with only `command` rewritten to a trusted launcher invocation ([hook contract](https://code.claude.com/docs/en/hooks#pretooluse-decision-control)). Desktop and CLI share hooks and plugins ([desktop docs](https://code.claude.com/docs/en/desktop#shared-configuration)). The hook must block on failure (`onFailure: "block"`) and pass the original command as data to a helper that builds argv, never by string concatenation. Gap: only shell commands are contained; Claude's own file tools, MCP servers and hooks are not.
2. csa workers: wrap the whole worker process between the runtime's `Launch` and `worker_process(...)` in runner.py, keeping cwd, environment, stdin, streaming, exit codes and cancellation. Covers the worker's own file writes and locally started MCP servers.
3. OpenCode: a plugin on `tool.execute.before` that rewrites `output.args.command` (v1.19.4 checked in source: the hook mutates the same args object that then executes; exceptions block). The dev branch has a different hook API, so pin and test versions.

Policy for all three: write the project folder plus a private per-job temp; read access to needed toolchains; outbound network on. Keep launcher binaries, policies and broker state outside every writable folder.

## What needs handling (real work)

- Git: installed Git and its config, worktrees whose `.git` lives outside the project, hooks, credential helpers, signing.
- Node, npm, Python, pip: read per-user installs; redirect caches and build temp into the job temp; global installs fail.
- Visual Studio and .NET: SDK reads, NuGet/MSBuild/.NET state redirects; COM, named pipes and build servers need real tests.
- Agent homes: whole workers need writable sessions, logs and token refresh. Use job-scoped state or accept extra writable roots; making the real `~/.claude`, `~/.codex` or OpenCode homes writable breaks the strict policy.
- Background processes: MXC kills the tree when the foreground command exits, so per-command wrapping stops detached dev servers; whole-worker wrapping gives them the worker's lifetime.
- Network: open outbound access allows disclosure of readable data; host loopback and LAN access must be explicit choices.

Estimate (Sol's, for an experienced maintainer): policy generator and launcher 2-4 days; whole-worker wrapper 3-6; Claude plugin 1-3; OpenCode plugin 1-2; compatibility tests and docs 3-5; about 2-4 working weeks with overlap. First milestone: prove the native backend runs, writes outside project and temp are denied, children are contained, and the owner's Git/Node/Python/VS workflows work.

## Other candidates, in short

| Candidate | Why not the pick |
|---|---|
| Codex's Windows sandbox (Apache-2.0) | Three implementations (`unelevated`, `elevated`, `mxc`); callable as `codex sandbox ... -- command` from an installed Codex, but extracting it means a large subsystem; its `mxc` mode is MXC anyway |
| Anthropic sandbox-runtime, `srt-win` (Apache-2.0) | Windows is alpha: needs a one-time elevated install, dedicated user, WFP; documented Schannel breakage (curl, PowerShell web requests, NuGet, some Git); needs Node 22.12+ |
| Landstrip (engine LGPL-2.1+, API Apache-2.0) | Native AppContainer/LPAC; Git Bash can't start under LPAC; elevation needed for setup; a valid user-installed alternative |
| Microsoft SandboxSecurityTools (MIT) | A testing launcher for AppContainers, not an agent sandbox; much more work |
| Windows Sandbox (`wsb`) | A VM: host tools absent, one instance at a time, `wsb exec` has no process I/O |
| Sandboxie (GPLv3 / custom Plus license) | Driver-based, admin install; worth testing only if MXC can't run the toolchain |
| Fence (Apache-2.0) | macOS/Linux only; good reference code for Claude and OpenCode hooks |
| agent-win-sandbox (GPLv3) | Admin setup, documented incomplete path coverage |
| Docker Sandboxes (`sbx`) | Now native on Windows via WHP without Docker Desktop, but a separate guest environment and not openly licensed |
| Firecracker, gVisor, Kata, OpenShell (NVIDIA), OpenSandbox (Alibaba/Linux Foundation), E2B, CubeSandbox, microsandbox, Wasmtime/Wasmer | Linux/VM/cloud/WASM environments; none confines native Windows processes (microsandbox has a Windows preview, but runs Linux guests) |

The "OpenSandbox" names: opensandbox-group/OpenSandbox (Alibaba contribution, Linux Foundation, Apache-2.0, Docker/Kubernetes), diggerhq/opencomputer (formerly Open Sandbox, Apache-2.0, cloud), AIDotNet/OpenSandbox (MIT, .NET + Docker), thomas3d/OpenSandbox (LGPLv3, 2013, Windows hooking), and an unrelated game SDK.

Corrections to the earlier notes: Landstrip is licensed (LGPL engine; GitHub missed the package-level files); "GPL, so not bundleable" is too strong (calling a separately installed program differs from copying or redistributing); Docker Sandboxes no longer needs Docker Desktop on Windows; Daytona is archived with core development moved private (historical AGPLv3); comsysto/opencode-sandbox only documents Linux/Docker testing.
