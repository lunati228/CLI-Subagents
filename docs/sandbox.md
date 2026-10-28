# Sandbox

## Design: policy, backend, launcher and integration

Commands cross four interfaces. The policy layer describes writable workspaces,
protected names, read grants, secret denials and a network class (`none`,
`loopback`, `egress`; `proxy` is reserved). It contains no agent or MXC schema.
The backend translates that policy into an enforceable request and reports its
capabilities. Its `prepare` method returns serialized policy text and executor
arguments; it owns the format and flags. MXC on supported Windows is the first
backend.

The shared launcher accepts a policy and an argv command, owns the process tree,
passes through standard I/O and exit codes, and enforces timeout and parent
lifetime. It writes the backend request privately and removes it afterwards.
It never falls back to running an enabled command without containment.

An integration authorizes the original agent action and calls that launcher.
OpenCode's `shell.create.before` integration covers tools and direct shell
routes. Fixed-rule commands get loopback-only networking; reviewed commands get
egress. The gate and reviewers stay on the host. A private one-use request keeps
the authorization, command and network class together across the shell boundary;
the original command is data, encoded for the inner PowerShell process.

To add a backend, implement its capability check and policy translation, register
it, and test that unsupported policy classes fail closed. To add an integration,
translate its hook input into the shared policy/command interfaces and register
the hook before the agent can act. It must authorize every invocation, validate
its cwd, and keep gate state outside command grants. Neither addition changes
another backend or integration. The next integration is Claude Code's main-session
PowerShell hook; Codex keeps its own sandbox.

On supported Windows, csa runs each OpenCode shell command in a fresh MXC
process container. OpenCode, its file tools, the review gate, reviewers and
`csa-parent` stay on the host with the user's normal state and logins. File tools
are limited by the gate's path rules; read jobs run no shell commands and get
no OS containment for their file reads.

## What is contained

| Integration | Protection |
|---|---|
| OpenCode `edit` | Every shell invocation goes through `shell.create.before`, authorization and the shared launcher. |
| OpenCode `read` | The gate denies shell commands, including shell-route invocations. File reads use gate rules. |
| OpenCode `full` | No gate or csa command sandbox; requires the user's explicit request. |
| Codex | Keeps its own Windows sandbox and approval modes. |
| Antigravity | Keeps `agy --sandbox` for edit jobs. |
| Claude Code | Its main-session PowerShell hook is the next integration; workers are unchanged. |

Windows 11 24H2 build 26100.9278, 25H2 build 26200.9278 and later supported
releases can run the backend; the capability probe is authoritative. Linux and
macOS adapters are not enabled yet. MXC has bubblewrap and Seatbelt backends,
but their network and installation requirements need separate validation.

## Files and network

Commands can write the job's workspaces and their own disposable container temp.
Git metadata and agent/editor settings at workspace roots stay read-only or
are denied when missing, with a post-command check for newly planted names.
Existing nested agent settings and secret files are denied.
Read grants cover installed toolchains, the Python runtime, csa's code and git
configuration. The command gets no grant to csa's job records, gate cache, log,
reviewer folders, policies, authorization requests or the user's agent logins.
The generated command environment keeps toolchain settings and omits provider
keys and agent credentials. Tool state and caches are redirected to container
temp; that state ends with the command.

csa's host Python runtime and the executor's directory stay read-only even when
installed inside a workspace. The host file-tool gate protects them too, so a
command cannot replace code that a later host launch will run.

| Network class | Behavior |
|---|---|
| `none` | No egress or host loopback. |
| `loopback` | No internet or LAN egress; host loopback is allowed for local test servers. Fixed-rule commands, including ordinary builds and tests, use this class. |
| `egress` | Internet egress and host loopback; used only after reviewer approval, including a cached reviewer verdict. |
| `proxy` | Reserved; currently rejected. |

Loopback can reach services on this PC. A command-text rule cannot inspect test
code, but auto-approved test code still has its egress blocked by the OS policy.
Package downloads and other changing commands need review. Files outside the
writable workspaces remain unwritable after reviewer approval.

Node and other tools need drive-root enumeration to resolve paths. This exposes
names and sizes, not file contents; credential paths are denied. A read grant on
a whole drive would expose its contents, so csa refuses one. Background processes
end with their command, including when the command returns before a child exits.

MXC 1.0.1 can allow renaming an existing item onto a denied, absent protected
name. The shared launcher snapshots protected entries before each command and
checks them after the entire contained process tree has stopped. It checks every
writable root (cwd and additional directories), plus deeper protected names
explicitly named in the policy's read-only, denied or pinned paths. This uses
path checks, without scanning the workspace tree.

If a previously absent entry appears, csa fails the command and renames it into
a fresh sibling directory such as `.claude.csa-quarantine-<UTC time>-<random>/item`.
It preserves files, directories and links without deleting or overwriting them.
The error names the original and saved paths. Entries present at launch,
including `.git` files and dangling links, are left alone. If an entry cannot be
moved, csa reports that failure and tells you to move it aside before using git
or agents; it still tries to quarantine the other new entries.

The check runs after normal exit, command failure, timeout, executor termination
and parent-shell death. It cannot run if the launcher itself is forcibly killed
or the host stops. It also cannot prevent a concurrent host process from using
a planted path while the command is still running. Avoid concurrent host changes
to protected names: any newly appearing entry is treated as planted by the
command. Unmarked nested paths are not searched. Existing protected paths remain
read-only under the backend policy. See the filesystem probes in the research
note for the underlying MXC limitation.

## Launching and failure handling

`launcher.execute(policy, command, backend, private_folder, parent_pid=...)` is
the common Python API. `Policy` carries paths and protections; `Command` carries
argv, cwd, the filtered environment, network class and timeout in seconds.
`BackendInfo` identifies a ready backend and its verified executable/probe.
The isolated script entry point consumes the integration's one-use request and
calls the same API. Commands are data: the Windows integration uses PowerShell
`-EncodedCommand`, and POSIX integrations can pass an argv shell command.

The launcher starts the executor suspended, assigns its tree to a kill-on-close
Windows job, then resumes it. It checks the parent shell's process identity and
stops the container if the parent dies. Standard input/output/error and command
exit codes pass through. Exit 124 means timeout, 130 means the parent ended, and
125 means launcher setup or a protected-path check failed. A newly planted path
overrides the command's exit or timeout code with 125. Requests and backend
policies are private and removed after use; the runner removes leftovers after
a forcibly killed attempt.
An enabled sandbox never falls back to an uncontained command.

OpenCode's generated plugin registers the shell hook before unlocking the model.
Authorization uses the actual shell invocation, so it does not depend on a prior
tool event. Before awaiting authorization, the hook replaces the invocation with
a failing command; an error cannot leave the original command runnable.

## Installing and configuring

`csa setup` installs MXC and `csa update` verifies it. Tell the user before setup:
the pinned Microsoft release is about 540 MB to download once; csa verifies the
size, SHA-256 and Microsoft signatures, extracts only this architecture's
executables and DLLs (about 37 MB), and keeps them outside every writable grant.
`--no-sandbox` skips installation. csa never enables MXC telemetry or DACL
fallback. See the [research and sources](research/sandbox-choice.md).

```toml
[sandbox]
# enabled = false                    # opt out; on where Windows supports it
# path = "C:/tools/mxc/wxc-exec.exe"  # optional verified executor override
```

With `enabled = true`, unsupported systems and missing or broken executors fail
closed. With the key omitted, unsupported systems leave the integration off.
`csa doctor` shows the backend, version, signature/integrity checks, probe tier
and active integration. `enabled` remains the common switch; future per-agent
settings can sit above the same policy and launcher interfaces.

## Compatibility and stage 1 migration

Git Bash cannot start in MXC ([microsoft/mxc#1061](https://github.com/microsoft/mxc/issues/1061)),
so Windows commands use PowerShell. Windows `taskkill.exe` can hang inside a
container; commands get csa's `taskkill.cmd` stand-in on PATH. OpenCode's own
shutdown runs outside and uses the normal host tools.

Use installed/local dependencies for builds without egress. Dotnet and Gradle
still need review because build daemons can reuse processes outside the command
container. The launcher disables MSBuild server reuse; .NET projects may also
need project-local NuGet settings or explicit source-control settings when they
are nested inside an ungranted repository. Pytest needs a local config when it
would otherwise search unreadable ancestor folders.

Stage 1's whole-worker wrapper, Bun view, config/instruction copies and per-project
OpenCode state isolation are removed. Existing stage 1 state folders are not
used or deleted, and their sessions are not automatically migrated. New jobs use
normal OpenCode state and stored provider logins. No reviewer broker is needed.
