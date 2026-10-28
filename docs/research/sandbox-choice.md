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

## Stage 1: what was checked on the owner's PC (2026-10-26)

Windows 10.0.26300.9711 (26H2), x64, MXC 1.0.1 (`wxc-exec.exe`, tag v1.0.1 = commit fda29ac, 2026-10-16). The release facts and the first policy came from the main session; the rest are a stage 1 worker's experiments in a scratch folder with small Python, Node and PowerShell probes, real OpenCode 2.1.4 started without any model request, and csa's own pipeline with a fake OpenCode and with the real one. MXC's code and docs were read at tag v1.0.1; file paths below are relative to https://github.com/microsoft/mxc/blob/v1.0.1/.

### Release and probe

- One asset, `mxc-release-binaries.zip`, 542,051,775 bytes, SHA-256 `0e832239454ba7fcd338ff56218a287eef4c0a01c1484084b869f55feff8abe9` (matches GitHub's digest). `arm64/` and `x64/` folders; most of the size is `symbols/`. Each Windows binary is Authenticode "Valid", subject `CN=Microsoft Corporation, O=Microsoft Corporation, L=Redmond, S=Washington, C=US`, issuer "Microsoft Code Signing PCA 2024". csa installs the 11 top-level `.exe`/`.dll` files of `x64/` (37,224,440 bytes). `wxc-exec.exe` imports only system DLLs and runs `--probe` on its own.
- `wxc-exec.exe --probe` printed tier `base-container`, `needsDaclAugmentation: false`, and `baseContainerSupportsDenyPaths`, `baseContainerSupportsEnumeratePaths` and `baseContainerSupportsIngressHostLoopbackAllow` true. The probe also reaps orphaned DACL state from crashed runs before it prints (`src/tools/wxc/src/main.rs`).
- Supported builds (`docs/backends/process-container/os-version-support.md`): 24H2 26100.9278, 25H2 26200.9278, 26H2 26300.9550, 26H1 28000.2804.
- Telemetry needs a per-run request, the user's consent and permitting policy (`README.md` "Telemetry", `docs/telemetry.md`); csa's requests carry no `telemetry` section and csa never runs `--telemetry-consent`.

### How wxc-exec runs a command

| Finding | Evidence |
|---|---|
| Arguments after `--` become `process.commandLine` with `CommandLineToArgvW` quoting for `processcontainer`; the string goes straight to `CreateProcessW`, no shell. csa writes `commandLine` itself with `subprocess.list2cmdline`, which follows the same rules. | `src/mxc-sdk/src/core/mxc_common/cmdline.rs` |
| The child is created suspended by wxc-exec itself and joined to MXC's job object, so it stays inside the caller's job object too. csa's own wrapper (suspended start, job assignment, `TerminateJobObject` on close) works around wxc-exec, and closing it, or `taskkill /T /F` on wxc-exec from outside, ends the contained process and its grandchildren. | `base_container_runner.rs`; experiment with a heartbeat child and grandchild |
| Standard input, output and error are passed through ("STDIO mode: passthrough"); the exit code propagates (7 and 5 tested; 100 KB of stdin). | `--debug` output; experiments |
| With `inheritDefaultEnv: true`, supplied entries are layered over `CreateEnvironmentBlock(bInherit = FALSE)`: the user's profile variables, including the registry PATH, but no `SHELL`. Windows then replaces `TEMP`, `TMP` and `LOCALAPPDATA` with the container's own `%LOCALAPPDATA%\Packages\sandbox.{guid}\AC[\Temp]`, even when the request sets them, and deletes that folder after the run. So each run already has a private temp folder. | `appcontainer_runner.rs` `create_default_env_entries`, `build_inherited_entries`; experiment |
| `process.timeout` is milliseconds and 0 means none; on timeout MXC prints `{"error":{"code":"backend_error","message":"script timed out after ...ms"}}` and exits -1. csa uses 0 and its own timeout. | `docs/containment-policy/1.0.0/policy.md`; experiment |

### Filesystem grants on BaseContainer

| Finding | Evidence |
|---|---|
| Nothing in the user profile is readable unless granted; Windows and `Program Files` are, through ALL APPLICATION PACKAGES ACEs. A folder with its own ACL lacks them: `C:\Program Files\nodejs` (Users, Authenticated Users, Administrators, SYSTEM only), which is why `cmd /c node` said "not recognized" in the main session's test while `node --version` started by path. Granting the folder read-only fixes it. MXC's own tool policy grants exactly the PATH folders without that ACE. | `icacls`; `src/mxc-sdk/src/policy.rs` `available_tools_policy`; microsoft/mxc#1533 |
| A read-only grant on `C:\` makes every file on the drive readable (`~\.gitconfig`, AppData files), although `docs/schema.md` says a volume-root grant doesn't cascade. MXC's own tool policy grants the system drive when `pwsh.exe` is on PATH; microsoft/mxc#1455 reports the same exposure. csa never grants a drive root. | experiment; `policy.rs` `powershell_policy`; mxc#1455 |
| Node's JavaScript `realpathSync` (used to load every script, `npm test` included) does `lstat('C:\')` and fails with EPERM. `processContainer.filesystem.enumeratePaths: ["C:\\"]` fixes it: names, sizes and listings become visible across the drive, contents don't (`~\.gitconfig` stays unreadable). `NODE_OPTIONS=--preserve-symlinks --preserve-symlinks-main` also lets `npm test` run, but changes module resolution. `realpathSync.native` works on this build, so microsoft/mxc#1464 didn't reproduce here. A denied path still hides its listing under drive enumeration. | experiments |
| A read-only grant inside a read-write one wins: writing, creating, deleting or renaming there is denied (`.git` can't be renamed away). A denied path inside a read-write one hides it completely. | experiments |
| A junction inside a granted folder gives no access to its target; grants follow the final path. | experiment |
| Single files can be granted read-only or read-write; creating a sibling of a read-write file is denied. | experiment |
| A read-only grant on a path that doesn't exist doesn't stop its creation. A denied path that doesn't exist blocks `mkdir` and writes, but not a rename of an existing folder onto that name. csa now checks for newly planted protected names after the contained tree stops and moves them aside; the backend gap itself remains. | experiment; `sandbox/launcher.py` post-command check |
| `git status` works with `.git` read-only (no ownership warning); `git add` fails on `index.lock`. git warns about `~\.gitconfig` until that file is granted. | experiments |
| Bun can't read through read-only grants: `readdir` gives EPERM and `readFile` ENOENT, while Python, Node, git and cmd read the same grants. Inside a read-write folder, OpenCode still loaded a plugin whose `package.json` and `server.js` were read-only, through its directory import. Requests that include DELETE or FILE_WRITE_ATTRIBUTES access fail on read-only grants (checked with `CreateFileW`); Bun's Windows directory opens can ask for DELETE (`can_rename_or_delete` in https://github.com/oven-sh/bun/blob/bun-v1.4.4/src/sys/lib.rs), which is the likely cause (not confirmed). OpenCode 2.1.4 embeds Bun 1.4.4 (`BUN_BE_BUN=1 opencode.exe --version`). | experiments |

### Network, UI and processes

| Finding | Evidence |
|---|---|
| The main session's policy (`processContainer.capabilities: ["internetClient"]`, no `network` section) has no network: DNS fails (`getaddrinfo` 11001). An omitted `network` means egress deny. `network.egress.default: "allow"` works without any capability. | `docs/containment-policy/1.0.0/policy.md` "Network and UI"; `docs/backends/process-container/networking.md`; experiments |
| Two processes in the same container can talk over 127.0.0.1 only with `ingress.hostLoopback: "allow"` (connect fails with WSAEACCES otherwise; bind works). That setting opens both directions to the host's loopback address. | experiments; `networking.md` |
| `ui.disable` must be false or git and .NET fail with STATUS_DLL_INIT_FAILED (main session). | main session; `docs/backends/process-container/UIPolicy_Schema.md` |
| MSYS2 programs (Git Bash, Git's `usr\bin` tools) fail with STATUS_DLL_INIT_FAILED whatever the UI settings (`isolation` desktop, handles, atoms, everything allowed). The cause is `NtCreateDirectoryObject(\BaseNamedObjects\msys-2.0S5-...)` being denied; no policy can grant it. `captureDenials` recorded `C:\WINDOWS` and `HKCU\Console` before the failure. Windows PowerShell 5.1, cmd, git.exe, Python, Node, .NET and `claude.exe --version` work. | microsoft/mxc#1061; experiments |
| `taskkill.exe` never returns inside a container (`/PID n`, with or without `/T` and `/F`; `/?` works). `CreateToolhelp32Snapshot` (it sees only the container's processes) and `OpenProcess` plus `TerminateProcess` work. | experiments |
| Started from a detached, console-less process in the user's session, as csa's runner is, `whoami`, PowerShell, git and OpenCode work. microsoft/mxc#1465 (USER32 programs fail) applies to callers in session 0, such as services. | experiment; mxc#1465 |

### OpenCode 2.1.4 in a container

Sources at https://github.com/anomalyco/opencode/tree/v2.1.4.

- `opencode run --standalone` starts `opencode serve --stdio --port 0` with a random password and talks to it over HTTP on 127.0.0.1 (`packages/cli/src/services/standalone.ts`). Without `hostLoopback` the client fails with "Transport: EACCES".
- The server resolves the project with Bun's `realPath` and fails with HTTP 500 (`EPERM lstat 'C:\'`) without enumerate access to the drive root.
- After it finishes, the client stops its server with `cmd /d /s /c "taskkill /pid N /T /F"` (seen in the process tree) and hangs there until killed. With csa's `taskkill` stand-in first on PATH it exits normally.
- With isolated data, state and cache folders, loopback, drive enumeration and the stand-in, `run` with an unavailable model (no provider request) printed "Model unavailable" and exited in 1.2 s.
- Without a configured shell OpenCode uses `SHELL`, then pwsh, powershell, Git Bash and cmd on Windows (`packages/core/src/shell/select.ts`); csa removes `SHELL` so it picks PowerShell.
- OpenCode watches local plugin files and reloads them when they change (`packages/core/src/plugin/module.ts`, `plugin/supervisor.ts`); `OPENCODE_FILEWATCHER_DISABLE` or `OPENCODE_DISABLE_FILEWATCHER` (first one set wins, `packages/cli/src/server-process.ts`) turns all file watching off, which a run with it set confirmed.
- `~/.config/opencode/service.json` holds the background service's password; the service listens on loopback (port 28894 on Windows), so the sandbox must never read that file.
- End to end through csa (real MXC, real OpenCode, csa's generated policy and plugin, a model id whose provider doesn't exist): the plugin loaded with its files read-only and switched the session to that model, OpenCode reported "Model unavailable", and the job ended after 2.1 s with no provider request.

### Choices in stage 1

- Only OpenCode `read` and `edit` workers run in the sandbox. Claude Code workers don't, because their Bash tool and csa's worker hooks need Git Bash, Claude Code keeps sessions, settings, hooks, plugins and its login in one folder, and its native build uses Bun, which likely has the same trouble with read-only grants (not tested). Codex and Antigravity keep their own sandboxes; `full` jobs stay unsandboxed as everywhere else.
- Writable: the job's folders (with `.git` and agent and editor settings folders read-only, or denied when missing), the job's message channel and gate folder, and for OpenCode its own data, state and cache per project plus a per-attempt folder of copies. OpenCode never writes the user's own OpenCode folders: a writable `snapshot/` there would let a worker plant git config that the next unsandboxed session runs, and the database holds the user's logins and session history.
- Read-only: tool folders from PATH and tool variables (the list follows MXC's `available_tools_policy`), `%LOCALAPPDATA%\Programs`, `~\.rustup`, `~\go`, `~\.nuget\packages`, the interpreter and package csa runs from, git config. Never a drive root, the home folder, an agent folder or a credentials folder, which are also refused as writable workspaces.
- Enumerate-only: the drive roots of the granted paths. Credentials folders in the home folder are denied so their names stay hidden.
- Network: outbound and loopback open (OpenCode needs both). UI on, no DACL fallback, no MXC timeout.
- A job that should be sandboxed fails when MXC is missing, the probe fails or the policy can't be built; MXC's own error message is put into the result.

### Left for later

- Claude Code hook (stage 2): Bash tool commands can't run inside as they are, because the container can't start Git Bash. PowerShell tool commands can. The same grants apply, plus drive enumeration for Node and the `taskkill` stand-in; Claude Code itself stays outside, so its state and login aren't exposed.
- Network allowlist: MXC's proxy mode (`runtimeConfig.networkProxy`, egress deny, `ingress.default: "allow"`, and `hostLoopback: "allow"` for a proxy without package identity) could limit workers to the model provider and package registries through a local proxy run by csa.
- A reviewer started by the review gate runs inside the container too and can't reach its own login there; it would need a helper outside the sandbox.
- macOS (Seatbelt) and Linux (bubblewrap) through MXC's other backends.

## Whole worker or each command: prior art (2026-10-26)

The research favored sandboxing each shell command through OpenCode v2's
`shell.create.before` hook. OpenCode, the gate, reviewers and `csa-parent` would
stay outside, so reviewers could use their logins without a broker. OpenCode's
own file tools would then rely on the gate's path rules. The owner is deciding
between that approach and the whole-worker sandbox built in stage 1; this
recommendation does not change the implementation.

### How other projects do it

**Codex (Apache-2.0)** starts a separate MXC container for each command. Its
launcher invokes the Codex executable with `--__codex-windows-mxc`; launcher-only
environment variables carry the arguments and permissions. The helper builds a
request and starts it with the SDK's `BaseContainerRunner`, inheriting standard
input and output. See [create_command_args in lib.rs](https://github.com/openai/codex/blob/cf29971d93a6761906b9e8789f915902b5879e00/codex-rs/mxc-sandbox/src/lib.rs),
[transport.rs](https://github.com/openai/codex/blob/cf29971d93a6761906b9e8789f915902b5879e00/codex-rs/mxc-sandbox/src/transport.rs),
[policy.rs](https://github.com/openai/codex/blob/cf29971d93a6761906b9e8789f915902b5879e00/codex-rs/mxc-sandbox/src/policy.rs)
and [native.rs](https://github.com/openai/codex/blob/cf29971d93a6761906b9e8789f915902b5879e00/codex-rs/mxc-sandbox/src/native.rs).

Codex's policy protects `.git`, `.agents`, `.codex` and `.aws` inside writable
roots. Reads follow the permission profile; its default exposes the disk through
each drive's top-level folders because volume-root grants are nonrecursive in
that policy, plus Windows, Program Files and ProgramData. It requests
`registryRead`, forbids DACL mutation, and enables UI with desktop isolation.
Network on allows egress, ingress and host loopback; off denies all three.
Managed proxy mode denies egress except to `127.0.0.0/8` and `::1`. Sources:
[permissions.rs](https://github.com/openai/codex/blob/cf29971d93a6761906b9e8789f915902b5879e00/codex-rs/protocol/src/permissions.rs)
and [policy.rs](https://github.com/openai/codex/blob/cf29971d93a6761906b9e8789f915902b5879e00/codex-rs/mxc-sandbox/src/policy.rs).

Approvals stay outside the command sandbox: Codex can escalate after a sandbox
failure or bypass it after approval. MCP servers run outside; a server can opt in
to receiving the policy and a launcher through `codex/sandbox-state`.
`apply_patch` also gets a filesystem sandbox context. Sources:
[tools/sandboxing.rs](https://github.com/openai/codex/blob/cf29971d93a6761906b9e8789f915902b5879e00/codex-rs/core/src/tools/sandboxing.rs),
[sandbox_metadata.rs](https://github.com/openai/codex/blob/cf29971d93a6761906b9e8789f915902b5879e00/codex-rs/codex-mcp/src/sandbox_metadata.rs)
and [apply_patch.rs](https://github.com/openai/codex/blob/cf29971d93a6761906b9e8789f915902b5879e00/codex-rs/core/src/tools/runtimes/apply_patch.rs).

**Fence's OpenCode plugin (Apache-2.0)** intercepts `bash` in
`tool.execute.before`. It sends the command and cwd to
`fence --opencode-pre-tool-use`, with a five-second timeout. Fence answers deny,
wrap or allow; wrapping rewrites the command to a `fence ... -c` invocation.
It fails closed unless `failOpenOnRunnerError` is enabled and skips wrapping
when `FENCE_SANDBOX=1`. Fence's README recommends also running OpenCode itself
under Fence for filesystem and network policy, so its recommendation uses both
layers. Fence supports macOS and Linux; Windows requires WSL. Sources:
[plugin factory.ts](https://github.com/fencesandbox/opencode-fence/blob/47da27babed0ef54aa08a322a84bc176cf74cee7/src/factory.ts),
[fence-runner.ts](https://github.com/fencesandbox/opencode-fence/blob/47da27babed0ef54aa08a322a84bc176cf74cee7/src/fence-runner.ts),
[plugin README](https://github.com/fencesandbox/opencode-fence/blob/47da27babed0ef54aa08a322a84bc176cf74cee7/README.md),
[hooks_runtime.go](https://github.com/fencesandbox/fence/blob/737751a82b49f287b41f8a3e62ec62919d7b9224/cmd/fence/hooks_runtime.go)
and [Fence README](https://github.com/fencesandbox/fence/blob/737751a82b49f287b41f8a3e62ec62919d7b9224/README.md).

**isanchez31/opencode-sandbox-plugin (MIT)** rewrites `bash` commands in
`tool.execute.before` to run through Anthropic's sandbox-runtime, then restores
the displayed command in `tool.execute.after`. It has no Windows backend. See
[index.ts](https://github.com/isanchez31/opencode-sandbox-plugin/blob/d59f2cfdeedf995700e0e8610ecc7a735f5ccacd/src/index.ts)
and [config.ts](https://github.com/isanchez31/opencode-sandbox-plugin/blob/d59f2cfdeedf995700e0e8610ecc7a735f5ccacd/src/config.ts).

**Microsoft MXC 1.0.1 (MIT)** provides ten generic SDK samples covering transient
and persisted containers, filesystem and network containment, I/O modes,
telemetry and dry runs. None is an agent integration. Its README describes a host
application running untrusted model output, plugins and tools in containers.
Sources: [README](https://github.com/microsoft/mxc/blob/v1.0.1/README.md) and
[samples](https://github.com/microsoft/mxc/tree/v1.0.1/samples).

### OpenCode 2.1.4's integration point

`Shell.create` builds an invocation with `command`, `cwd`, `timeout`, `shell` and
`env`, passes that same object by reference to `shell.create.before`, then
spawns `invocation.shell`. The Promise plugin API exposes this as
`ctx.shell.hook("create.before", ...)`. Cmd gets `/c`; PowerShell gets
`-NoLogo -NoProfile -NonInteractive -Command`; other shells get `-c`. Sources:
[shell.ts](https://github.com/anomalyco/opencode/blob/v2.1.4/packages/core/src/shell.ts),
[shell/select.ts](https://github.com/anomalyco/opencode/blob/v2.1.4/packages/core/src/shell/select.ts),
[plugin/hooks.ts](https://github.com/anomalyco/opencode/blob/v2.1.4/packages/core/src/plugin/hooks.ts)
and [Promise shell API](https://github.com/anomalyco/opencode/blob/v2.1.4/packages/plugin/src/promise/shell.ts).

Three callers use `Shell.create`: the bash tool (after csa's tool gate), session
shell commands, and the server's `shell.create` handler. The last two do not go
through a tool hook. A shell-create wrapper would cover all three while keeping
the original command in the transcript. `ctx.mcp.transform` can change or remove
MCP server configurations. In stdio mode OpenCode removes `OPENCODE_PASSWORD`
from its environment before tools run. Sources:
[tool/plugin/shell.ts](https://github.com/anomalyco/opencode/blob/v2.1.4/packages/core/src/tool/plugin/shell.ts),
[session/shell.ts](https://github.com/anomalyco/opencode/blob/v2.1.4/packages/core/src/session/shell.ts),
[server shell handler](https://github.com/anomalyco/opencode/blob/v2.1.4/packages/server/src/handlers/shell.ts),
[Promise MCP API](https://github.com/anomalyco/opencode/blob/v2.1.4/packages/plugin/src/promise/mcp.ts)
and [server-process.ts](https://github.com/anomalyco/opencode/blob/v2.1.4/packages/cli/src/server-process.ts).

### Measured command overhead

On the owner's PC, using stage 1's policy builder, the median of seven runs was:

| Command | Outside MXC | Inside MXC |
|---|---:|---:|
| `cmd /c exit 0` | 11 ms | 83 ms |
| PowerShell `exit 0` | 134 ms | 228 ms |
| `python -I -c pass` | 22 ms | 104 ms |

These local measurements imply about 70 to 95 ms of overhead per command. They
exclude the extra helper that would write the policy for each command.

### Tradeoffs and remaining work

| Concern | Each command | Whole worker (stage 1) |
|---|---|---|
| Builds and tests | Contained; could use a separate network policy per command | Contained, with outbound network open for the agent |
| File tools | OpenCode's read, write, edit, patch, grep and glob stay outside; the gate alone limits them | File tools are contained too |
| MCP servers | Outside; guarded jobs could remove them or wrap them through `ctx.mcp.transform` | Locally started servers are contained |
| Reviewers and login | Outside, using their normal login folders | Need a broker outside to reach their logins |
| OpenCode state and auth | Normal folders; stored provider logins work | Per-project state and copied config; stored provider logins unavailable |
| Gate log and cache | Outside the commands' reach | Writable by the commands the gate judges |
| Background processes | End with the command | Can last until the worker ends |
| Read jobs | No commands, so no OS containment | File reads are contained |

A command sandbox could deny egress while allowing host loopback for builds and
tests, and permit egress for reviewer-approved installs. That network split is a
proposal, not a tested policy. It would also avoid OpenCode's Bun read-only
issues and its in-container taskkill problem. Whole-worker containment keeps
OpenCode, the gate and commands under one container identity. Sibling-process
memory access, including access to OpenCode's keys in memory, is a concern
inferred from that identity; it was not tested.

The gate has a gap in either design: `agy_review/paths.py`'s `HOME_SECRETS` list
does not name `~/.local/share/opencode/auth.json` or
`~/.config/opencode/service.json`. Reads outside the workspace go to a reviewer,
which could approve them. This needs attention whichever design the owner picks.

The research worker estimated two to four days for command containment: the
plugin hook, a Python policy/launch helper with a kill-on-close job, container
cleanup when the parent shell dies, `powershell -EncodedCommand` for the inner
command, network classes in the gate, and removal of the copied view and isolated
state. OpenCode kills only its direct child on timeout, so the helper must own
the process tree. Finishing whole-worker containment was estimated at three to
five days for authenticated broker requests, gate state outside the container
and login handling. The shared identity and open network would remain.

Codex's Rust code supplies reference patterns, particularly `registryRead`,
desktop UI isolation, protected names and proxy policy. A code port would require
Apache-2.0 attribution. Fence and isanchez31 provide integration patterns; no code
was copied in this integration.

Still unchecked in the recorded research: Codex's read/list/grep handlers and
`sandboxing/src/windows_mxc.rs` helper selection; a live OpenCode shell-hook
rewrite without a model request; wrapper-child cleanup on Windows timeout;
egress denied with host loopback for dotnet, npm and pytest; and memory access
between processes in one container. The source revisions linked above were read
on 2026-10-26; these findings do not claim verification against newer versions.

## Per-command design checks (2026-10-28)

The owner approved per-command containment. The implementation reuses MXC's
installed executor and policy format, with csa's own small launcher and OpenCode
integration. No upstream code is copied. Codex and Fence provide the separation
of responsibilities; their Rust and platform-specific code are not dependencies.

### OpenCode hook and cleanup, checked without a model

An isolated OpenCode 2.1.4 server, using scratch HOME/XDG folders, loaded a local
Promise plugin. A POST to `/api/shell` with `echo ORIGINAL` ran
`Write-Output 'HOOK_REWRITTEN'` after the plugin changed both `command` and
`shell` in `ctx.shell.hook("create.before", ...)`. The output was
`HOOK_REWRITTEN`. No prompt or provider request was sent.

Plugin activation is asynchronous. An immediate shell-route request can precede
activation; the experiment waited for the plugin's active state. csa's normal
worker must register its shell hook before unlocking the placeholder model, just
as it already does for the tool gate. The shell hook must authorize the command
it actually receives, including direct shell-route calls, rather than relying on
a preceding tool hook. Shell API records show the rewritten invocation; the
original tool arguments remain unchanged.

The callback has no denial response. csa replaces the invocation with a trusted
failing command before awaiting authorization, and leaves it in place on errors.
The callback resolves after denial rather than risking removal of a rejected
plugin hook. A production-hook probe and fake-server regressions checked this
behavior.

Sources at v2.1.4: [shell.create](https://github.com/anomalyco/opencode/blob/v2.1.4/packages/core/src/shell.ts),
[shell API handler](https://github.com/anomalyco/opencode/blob/v2.1.4/packages/server/src/handlers/shell.ts),
[plugin supervisor](https://github.com/anomalyco/opencode/blob/v2.1.4/packages/core/src/plugin/supervisor.ts)
and [Promise shell API](https://github.com/anomalyco/opencode/blob/v2.1.4/packages/plugin/src/promise/shell.ts).

Correction to the earlier handoff: OpenCode's Windows spawner tries
`taskkill /pid PID /T /F`; it falls back to the direct child's `kill()` when
that fails. In the real-server probe a PowerShell command ran a Python heartbeat
child. A one-second timeout produced `status: timeout`, and the child's counter
stopped at 10 across later samples. This proves tree cleanup for that case, not
for every detached descendant or fallback. csa's launcher therefore owns a
kill-on-close job and monitors its parent independently. Source:
[cross-spawn-spawner.ts, killGroup and stop](https://github.com/anomalyco/opencode/blob/v2.1.4/packages/util/src/cross-spawn-spawner.ts).

### Builds with egress denied and host loopback allowed

Small real-MXC 1.0.1 probes on Windows 26300.9711 used stage 1's file grants,
`egress.default: deny`, and `ingress.hostLoopback: allow`:

| Probe | Result |
|---|---|
| npm test running a Node TCP loopback test | Passed, exit 0 |
| pytest 9.2.0 running a Python TCP loopback test | Passed, exit 0 |
| .NET SDK 10.0.401 local restore and console application | Passed, exit 0 |

The pytest fixture needed its own `pytest.ini` to stop searching unreadable
ancestor configuration. .NET initially failed while reading the user's denied
NuGet configuration. Redirecting APPDATA, HOME/USERPROFILE and NuGet settings to
scratch folders fixed restore; the scratch project also disabled SourceLink
repository discovery because it was nested inside a larger, ungranted checkout.
Build servers and shared compilation were disabled. These tests used local
dependencies only; downloading packages requires a reviewed command with egress.
The temporary pytest wheel installation and all probe files are disposable.

MXC's [Windows networking contract](https://github.com/microsoft/mxc/blob/v1.0.1/docs/backends/process-container/networking.md)
keeps host loopback separate from internet and LAN access. Loopback still exposes
host services, so it is a distinct policy class, not a claim of zero networking.

### Codex's current file service and helper path

At commit `cf29971d93a6761906b9e8789f915902b5879e00`, the old read_file,
list_dir and grep_files handler files no longer exist. `LocalFileSystem` chooses
the sandboxed service for reads when `should_read_from_sandbox()` is true and
otherwise uses direct host reads. The sandboxed implementation runs filesystem
requests through a helper, and refuses a requested sandbox when none can be
enforced. Consequently command containment alone should not be described as
containment of every file tool. Sources: [local_file_system.rs](https://github.com/openai/codex/blob/cf29971d93a6761906b9e8789f915902b5879e00/codex-rs/exec-server/src/local_file_system.rs),
[sandboxed_file_system.rs](https://github.com/openai/codex/blob/cf29971d93a6761906b9e8789f915902b5879e00/codex-rs/exec-server/src/sandboxed_file_system.rs)
and [fs_sandbox.rs](https://github.com/openai/codex/blob/cf29971d93a6761906b9e8789f915902b5879e00/codex-rs/exec-server/src/fs_sandbox.rs).

`sandboxing/src/windows_mxc.rs` only records capability availability. Helper
selection is in `SandboxManager`'s WindowsMxc branch: it requires the caller's
explicit `sandbox_exe`, then adds `--__codex-windows-mxc`. Filesystem helpers
supply `runtime_paths.codex_self_exe`; no executable is selected through an
untrusted workspace PATH. Sources: [windows_mxc.rs](https://github.com/openai/codex/blob/cf29971d93a6761906b9e8789f915902b5879e00/codex-rs/sandboxing/src/windows_mxc.rs),
[manager.rs](https://github.com/openai/codex/blob/cf29971d93a6761906b9e8789f915902b5879e00/codex-rs/sandboxing/src/manager.rs)
and [MXC helper transport](https://github.com/openai/codex/blob/cf29971d93a6761906b9e8789f915902b5879e00/codex-rs/mxc-sandbox/src/lib.rs).

### MXC on other operating systems

MXC 1.0.1 has a stable bubblewrap backend on Linux and Seatbelt on macOS 15+,
with the same directional policy schema. This makes them plausible future
adapters, but not interchangeable without validation. Linux needs bwrap 0.5+
and, for network-enabled namespaces, slirp4netns and firewall tools. Seatbelt
rejects `ingress.default: deny` with `hostLoopback: allow`; inbound filtering
cannot express that Windows policy. Its executable is `mxc-exec-mac`.

Keep csa's default backend Windows-only for this release. A future adapter must
prove each requested network class or reject it; it must never silently weaken
loopback-only into unrestricted ingress or egress. Sources:
[bubblewrap backend](https://github.com/microsoft/mxc/blob/v1.0.1/docs/backends/bwrap/bubblewrap-backend.md),
[Seatbelt backend](https://github.com/microsoft/mxc/blob/v1.0.1/docs/backends/seatbelt/seatbelt-backend.md)
and [MXC MIT license](https://github.com/microsoft/mxc/blob/v1.0.1/LICENSE.md).

The implemented hook-to-launcher path was then checked with the same isolated
real OpenCode server and real MXC: `python -m pytest -q test_probe.py` passed,
exit 0, with no provider request and no egress. A filtered environment requires
the real `LOCALAPPDATA` value: MXC uses it to derive its disposable package/temp
tree even with `inheritDefaultEnv: false`. No other user environment variables
are inherited implicitly. PSEC also failed with HRESULT 0x80070003 when a deny
path had a missing parent; the adapter now denies its first missing component,
preserving the denial while making the request enforceable. These are measured
Windows 26300.9711 / MXC 1.0.1 results, covered by fake-backend regressions.

### Protected-name rename check (2026-10-28)

The shared launcher records which protected names are absent immediately before
launch. After stopping the entire contained tree, it checks those entries again
and renames any new item into a unique sibling quarantine directory, under the
name `item`. It never deletes the item or replaces an earlier quarantine. The
command fails with exit 125 and reports both paths, including when its original
result was failure, timeout or parent-shell death. A killed executor also gets
this cleanup while the launcher is alive.

The check covers the top-level protected names in each writable workspace,
policy-specific names, and deeper protected components explicitly marked by
read-only, denied or pinned policy entries. It uses `lstat` so files, directories
and dangling links count as existing; a moved link keeps its target untouched.
Before moving a nested entry, the launcher checks that its parent still resolves
inside the original workspace. A check or rename error fails closed and is
reported; other new entries still get a quarantine attempt.

This is a post-command check. A concurrent host process could use the new path
before cleanup, and concurrent host changes to protected names can be mistaken
for command output. Previously absent, unmarked nested paths are not scanned;
this includes newly planted nested repositories. Existing nested `.git` files,
directories and referenced gitdir/commondir paths are now pinned read-only.
Killing the launcher itself skips its check (including OpenCode's host shell
timeout). A separate job-start baseline is checked before every command
authorization and in the runner after worker-tree cleanup, including timeout
and cancel. Both checks fail closed on errors. Killing both the runner and
launcher, or stopping the host, still prevents recovery. The backend still
enforces existing protected entries. Fake-wxc tests cover each protected name,
additional roots, explicit deeper paths, existing entries, links and junctions,
command failure, timeout, executor termination, parent death and cleanup errors.
