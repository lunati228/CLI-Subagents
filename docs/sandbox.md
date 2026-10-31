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
another backend or integration. Claude Code's opt-in main-session PowerShell
hook uses the same interfaces; Codex keeps its own sandbox.

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
| Claude Code main session | Opt-in PowerShell command containment; `block-bash` also denies the Bash tool. csa workers keep their existing behavior. |

Windows 11 24H2 build 26100.9278, 25H2 build 26200.9278 and later supported
releases can run the backend; the capability probe is authoritative. Linux and
macOS adapters are not enabled yet. MXC has bubblewrap and Seatbelt backends,
but their network and installation requirements need separate validation.

## Files and network

Commands can write the job's workspaces and their own disposable container temp.
Git metadata and agent/editor settings at workspace roots stay read-only or
are denied when missing, with a post-command check for newly planted names.
Existing nested agent settings and secret files are denied. Nested `.git`
directories and files are read-only, as are the gitdir and commondir referenced
by a worktree or submodule marker. The scan skips directory reparse points,
including junctions on Python 3.11, rather than following dependency-store loops.
Read grants cover installed toolchains, the Python runtime, csa's code and git
configuration. The command gets no grant to csa's job records, gate cache, log,
reviewer folders, policies, authorization requests or the user's agent logins.
The generated command environment keeps toolchain settings and omits provider
keys and agent credentials. Tool state and caches are redirected to container
temp; that state ends with the command.

csa's host Python runtime and the executor's directory stay read-only even when
installed inside a workspace. The host file-tool gate protects them too, so a
command cannot replace code that a later host launch will run.
OpenCode's host PATH excludes workspace directories and relative or empty
entries, so a command cannot plant a tool that a later host grep or glob runs.
Windows would also try the current folder (the workspace) first for a bare
program name: OpenCode's host environment sets `NoDefaultCurrentDirectoryInExePath`,
and csa looks up `claude`, `codex`, `agy`, `opencode`, `node` and `taskkill` only
in absolute PATH folders or the system folder.
The original tool PATH, including an activated workspace venv, is available only
to contained commands. Host timeout cleanup uses absolute Windows system tools.

| Network class | Behavior |
|---|---|
| `none` | No egress or host loopback. |
| `loopback` | No internet or LAN egress; host loopback is allowed for local test servers. Fixed-rule commands, including ordinary builds and tests, use this class. |
| `egress` | Internet egress and host loopback; requires fresh reviewer approval for each invocation. |
| `proxy` | Reserved; currently rejected. |

Loopback can reach services on this PC. A command-text rule cannot inspect test
code, but auto-approved test code still has its egress blocked by the OS policy.
Package downloads and other changing commands need review. Files outside the
writable workspaces remain unwritable after reviewer approval.
Reviewer command verdicts are never cached because workspace scripts and package
lifecycle hooks can change between invocations. Reviewers are warned that readable Git
configuration may hold tokens in URL rewrites or `http.extraHeader`; reading
permissions do not authorize disclosure of those contents.

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

The launcher checks after normal exit, command failure, its own timeout,
executor termination and parent-shell death while the launcher stays alive.
OpenCode's shell timeout or a runner timeout/cancel can forcibly kill the
launcher before its cleanup runs. Each launcher therefore writes its pre-command
snapshot to a private recovery record before the command starts and removes it
after its own check. A record left by a killed launcher is checked before the
next command authorization, and by the runner after its worker tree stops,
including timeout and cancel. Records of launchers that still run are left to
them. Check errors fail closed and keep the record; so does an entry that could
not be moved aside, so later commands keep failing until you move it. If the
runner and launcher both die, or the host stops, cleanup cannot run. These
checks also cannot prevent a concurrent host process from using
a planted path while the command is still running.

Each check covers only its command's own run. Protected entries the host creates
between commands, such as a reviewed and approved edit that creates `.vscode`, a
`git init` you run yourself, or Claude Code writing `.claude/settings.local.json`,
are left alone. An entry that appears while a command runs is treated as planted
by it, so avoid host changes to protected names during a command. After a killed
launcher, its record dates from that command's start, and entries created since
then are treated the same way. A bounded additional scan catches new nested `.git` markers down to
three directory levels below each writable root, including entirely new folders.
Each pass visits at most 1,024 directories and 20,000 entries, with a 48 KiB
snapshot budget so it fits alongside the gate policy. Complete prior
parent listings distinguish new folders from old unvisited ones. It skips links,
junctions, protected settings, quarantine folders and common dependency/build
trees (`node_modules`, `.venv`, `venv`, `__pycache__`, `target`, `dist`, `build`).
New repositories beyond those bounds remain unchecked. Existing nested Git
metadata is read-only. Existing
protected paths remain read-only under the backend policy. See the filesystem probes in the research
note for the underlying MXC limitation.

## Launching and failure handling

`launcher.execute(policy, command, backend, private_folder, parent_pid=...)` is
the common Python API. `Policy` carries paths and protections; `Command` carries
argv, cwd, the filtered environment, network class and timeout in seconds.
`BackendInfo` identifies a ready backend and its verified executable/probe.
The isolated script entry point consumes the integration's one-use request and
calls the same API. Commands are data: the Windows integration uses PowerShell
`-EncodedCommand`, and POSIX integrations can pass an argv shell command.
Commands whose encoded Windows argv would reach the 32,767 UTF-16 code-unit
limit fail with a clear error before a request is issued; split them or run a
workspace script.

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
and active integration. `enabled` remains the common switch; Claude's opt-in
settings use the same policy and launcher interfaces.

## Claude Code main sessions

The main-session sandbox is off by default, with no sandbox hooks installed.
When enabled, synchronous SessionStart and PreToolUse hooks skip csa worker sessions. Requires
Claude Code **2.1.295 or later**, which supports fail-closed `onFailure: "block"`
hooks, plus a working MXC installation on supported Windows.

```toml
[sandbox.claude]
mode = "powershell"  # or "block-bash"; default "off"
additional_dirs = ["C:/projects/shared"]  # absolute, existing folders
timeout_seconds = 120  # maximum per command; 1..86400
```

After setting the mode, run `csa setup` or `csa update`, then **restart Claude
Code**. These commands merge the three sandbox hooks (PreToolUse, SessionStart
and SessionEnd) into `settings.json` in `CLAUDE_CONFIG_DIR` or `~/.claude`, keeping
other settings and hooks and backing up changes. The plugin itself does not
install these hooks. `csa doctor` reports whether they are installed and match
the configured mode. To disable them, set `mode = "off"`, run `csa setup` or
`csa update` again, then restart Claude Code; only csa's sandbox hooks are removed.

`powershell` contains PowerShell tool commands. Bash keeps running normally, and
`csa doctor` warns that Bash is outside the sandbox. `block-bash` also denies the
Bash tool so the agent uses PowerShell. Git Bash cannot start inside MXC, so it
cannot be translated transparently. If `[sandbox] enabled = false` conflicts with
an enabled Claude mode, PowerShell calls are denied until you fix the settings.
An unsupported host or broken backend also denies PowerShell. Bash still follows
the selected mode.

Start a **new session** after changing the mode or its roots. The first
SessionStart records the project cwd and configured additional directories in a
private host baseline. Claude's documented hook payload does not expose `--add-dir`
grants: mirror those folders in `additional_dirs`. That list grants only csa's
command sandbox access; configure Claude's own directory permissions separately.
Resuming a session keeps its original roots. Changing shell cwd never adds a
writable root. csa refuses profile, agent-state and drive-root grants as usual.

The hook encodes the original script as command data in a private one-use request
and replaces the tool command with the shared launcher. It returns no permission
allow decision; Claude still checks the rewritten invocation. Its permission
dialog therefore shows a launcher command, while the tool description remains
available. This integration does not classify or review the original script.
Networking is loopback-only for every contained command, so package downloads
are unavailable through this hook. File tools, MCP servers, other plugins and
Claude itself run on the host and are outside this command boundary.

The command's requested timeout, in milliseconds, is capped by `timeout_seconds`.
The launcher uses a kill-on-close process job and watches its parent shell.
Background tool calls retain that lifetime and timeout; detached children do not
survive the command. Tool state is redirected to container temp. Profiles and
PowerShell state do not persist between commands. A forcibly killed launcher may
miss its immediate quarantine check; the next command, resumed SessionStart or
SessionEnd runs it from the launcher's recovery record. Changes Claude, its file
tools or you make between commands are never quarantined. If Claude and the host
both stop, cleanup waits until the session is resumed. SessionEnd removes unused
requests; the session's roots and any unsettled record are retained for resume
under csa's private `claude-sandbox` folder.
Preparation itself has a 60-second hook deadline; startup errors or timeouts
block calls on a supported Claude version.

## Proposed domain allowlist

This is a design for a later release. `Network.PROXY` remains rejected and there
is no domain allowlist config key yet. A proxy setting alone would not filter
destinations.

MXC v1.0.1's [proxy-only networking contract](https://github.com/microsoft/mxc/blob/v1.0.1/docs/backends/process-container/networking.md#model-2-proxy-only-egress-recommended)
requires a separately running host proxy. A Python standard-library proxy can
bind an ephemeral port on exactly `127.0.0.1`, then pass its address through
`runtimeConfig.networkProxy`. The client policy must deny direct egress and use
the ProcessContainer proxy ingress mapping. For an unpackaged Python process,
there is no `allowedProxyPeer` identity; this uses MXC's weaker, identity-less
compatibility path. It needs request-aware capability validation and must not
fall back to unrestricted egress. Its ingress behavior differs from csa's current
loopback policy, so it needs a separate backend capability and live validation.

Proposed configuration would be `[sandbox.egress] allowed_domains = [...]`,
omitted by default. An explicit list would narrow reviewer-approved egress into
proxy-only traffic; an empty list would deny egress. Fixed-rule commands would
remain loopback-only. Claude's hook could opt into that same domain policy only
through an explicit separate network setting; its current default stays
loopback-only. An unsupported host would reject the request before execution.

The proxy should allow exact, normalized DNS hostnames, with explicit subdomain
rules if added later. It would accept bounded HTTP absolute-form requests and
HTTPS CONNECT to port 443, reject IP literals, userinfo, ambiguous authorities,
conflicting Host headers, upgrades and malformed framing, and close connections
after one HTTP request. CONNECT tunnels need byte limits, idle deadlines and a
fixed connection count. DNS results must all be public addresses; the proxy must
connect to an already validated address to prevent a second DNS lookup from
rebinding the destination. Private, loopback, link-local and metadata addresses
would be denied. Redirect destinations face a fresh hostname check. HTTPS
tunnels cannot inspect encrypted paths, request bodies, or prove that traffic
inside a permitted tunnel uses the declared application protocol; a domain rule
also cannot prevent disclosure to an allowed domain.

The launcher would own one proxy per command, use private random authentication
where client libraries support it, pass HTTP(S)_PROXY/NO_PROXY overrides into the
container, and terminate proxy connections with the contained process tree.
Other host processes could reach an identity-less proxy port, so its authority
must stay limited to that command's allowlist. Logs would contain destination
host, port and verdict only, never URL paths, headers or credentials. Tests need
fake transport coverage for parsing, DNS rebinding, redirects, IPv6, cancellation,
proxy death and limits, followed by real-MXC probes for direct TCP/UDP and
loopback bypass, CLI proxy support and WFP endpoint enforcement. This is enough
new networking and lifecycle work to defer under the current task's time budget.

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
