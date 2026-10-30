# Main-session hooks and remaining sandbox checks

Checked 2026-10-30 against Claude Code's current hook documentation, OpenCode
v2.1.4 and MXC v1.0.1. No model requests or real account state were used.

## Decision and prior art

Reuse csa's existing policy, request format and launcher for Claude's PowerShell
tool. The earlier [sandbox research](sandbox-choice.md) already compared Fence,
Codex and isanchez31's integration. Fence's
[hook implementation](https://github.com/fencesandbox/fence/blob/737751a82b49f287b41f8a3e62ec62919d7b9224/cmd/fence/hooks_runtime.go)
shows the command-rewrite pattern; it is Apache-2.0 and targets other platforms.
Its platform launcher is not a fit for native Windows. We use that pattern with
our existing MXC launcher; no third-party code is copied or dependency added.
OpenCode is MIT; MXC is MIT. Research involved their source and documentation,
without incorporating their implementation code.

The trust boundary is the tool-generated command and cwd crossing from Claude
into a host hook. The host hook validates the cwd against a SessionStart baseline,
filters the environment and writes a private, one-use request. Only that request's
path enters the rewritten shell command. The original script is UTF-16LE Base64
data passed to PowerShell inside MXC. The hook does not return an allow decision.
Claude's permission system still handles the rewritten call.

Claude's [PreToolUse contract](https://code.claude.com/docs/en/hooks#pretooluse-decision-control)
supports `updatedInput`. Its documented common input contains `session_id` and
`cwd`, but does not enumerate `--add-dir` grants. Therefore the first SessionStart
binds the initial cwd and explicit `[sandbox.claude] additional_dirs`; later
shell cwd values and resumed sessions cannot widen those roots. Users must mirror
their add-dir grants in that list. Guessing roots from a changing tool cwd would
grant writes outside the original session workspace.

The [hook failure contract](https://code.claude.com/docs/en/hooks#block-the-action-when-a-hook-fails)
requires `onFailure: "block"` for fail-closed execution on hook crashes and
timeouts. This feature needs Claude Code 2.1.295 or later. The command hook is
synchronous, uses exec-form arguments, and gives preparation 60 seconds. It
does not run the workload while Claude is waiting for the hook. The launcher
owns the actual command's timeout and child tree. Host file tools and MCP remain
outside this boundary, as in OpenCode's per-command design.

## B5: OpenCode hook deadline

There is no independent `tool.execute.before` timeout in OpenCode v2.1.4's
dispatcher or Promise adapter. This is a source finding for that exact version:

- [`core/src/tool.ts`](https://github.com/anomalyco/opencode/blob/v2.1.4/packages/core/src/tool.ts),
  `beforeExecute` and `Snapshot.execute`, await the hook before executing the tool.
- [`core/src/plugin/hooks.ts`](https://github.com/anomalyco/opencode/blob/v2.1.4/packages/core/src/plugin/hooks.ts),
  `trigger`, awaits each registered callback without a timeout wrapper.
- [`plugin/src/promise/adapter.ts`](https://github.com/anomalyco/opencode/blob/v2.1.4/packages/plugin/src/promise/adapter.ts),
  `toolHook["execute.before"]`, uses `Effect.tryPromise` to translate rejection
  into `Tool.Error`, without adding a deadline.
- [`plugin/src/promise/registration.ts`](https://github.com/anomalyco/opencode/blob/v2.1.4/packages/plugin/src/promise/registration.ts)
  exposes no per-hook timeout option.

csa already supplies its own `reviewMs` timer in the generated plugin's `check`.
It kills the helper tree and rejects on expiration; the tool hook denies the
call. Shell authorization uses the same bounded helper. A session cancellation,
job timeout or host shutdown can still interrupt a pending hook. There is no
upstream deadline to extend or disable, so no behavioral change is needed for B5.
The existing fake-plugin timeout tests exercise csa's deadline.

## B6: bounded nested repository detection

The snapshot now records complete child-directory listings and `.git` existence
for at most 1,024 directories and 20,000 entries per pass, down to three directory
levels below each writable root. A 48 KiB snapshot budget, including indentation
allowance, leaves room in the gate's bounded context. Each post-command pass checks those folders and
newly created subtrees within the same bounds. A folder omitted from the initial
scan is not assumed new: only a complete prior parent listing can prove that.
Partial listings are discarded, which avoids moving an old repository when scan
limits or directory order change. The check handles Git directories and worktree
marker files without following their contents.

Directory symlinks, junctions, protected agent folders, quarantine folders and
common dependency/build trees (`node_modules`, `.venv`, `venv`, `__pycache__`,
`target`, `dist`, `build`) are skipped. This is bounded extra detection, not a
recursive guarantee. Deeper or skipped new repositories retain the documented
limit. The same snapshot is checked after commands, before later authorization
and by the runner after termination. Existing Git metadata remains protected by
the initial full policy scan. New markers are preserved in sibling quarantine
directories and the command fails closed.

## B7: proxy decision

Defer implementation. MXC's proxy endpoint is a transport restriction, not a
domain filter. A safe allowlist also needs a host proxy with bounded parsing,
DNS and destination validation, cancellation and tests against bypasses. It
would add a new network service and a different ingress contract. The proposed
design and required validation are in [the sandbox guide](../sandbox.md#proposed-domain-allowlist).
`Network.PROXY` remains rejected; no partial allowlist setting is exposed.

Sources: MXC v1.0.1
[networking contract](https://github.com/microsoft/mxc/blob/v1.0.1/docs/backends/process-container/networking.md),
[shared schema](https://github.com/microsoft/mxc/blob/v1.0.1/docs/schema.md), and
[MIT license](https://github.com/microsoft/mxc/blob/v1.0.1/LICENSE.md).
