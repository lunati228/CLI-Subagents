# Gemini through Antigravity CLI

CLI Subagents runs Gemini (and the other models Antigravity offers) through Google's Antigravity CLI, `agy`, with `runtime: "antigravity"`.

Check Google's terms first. The [Antigravity FAQ](https://antigravity.google/docs/faq#can-i-use-antigravity-cli-headless-mode-in-third-party-coding-agents-or-automate-account-switching) says consumer plans don't support wrapping `agy` inside third-party coding agents or automating account rotation, and points that use to a Gemini Enterprise or AI Studio API key. Use this runtime with an account route Google allows. Details are in [provider terms](research/provider-terms.md).

## Setup

1. Install Antigravity CLI from [Google's documentation](https://www.antigravity.google/docs/cli/) and run `agy` once to sign in.
2. Install `csa` (see the [Claude Code guide](claude-code.md#1-install)).
3. Check: `csa accounts --runtime antigravity` lists the binary, its version and whether it can list models.

There is nothing else to configure. Jobs run on the account you signed in with in `agy`. Extra Google accounts are optional; see [More than one Google account](#more-than-one-google-account).

## Native agents when Antigravity is the host

Run `csa setup --host antigravity` and start a new session. Setup installs the
main plugin and a companion plugin with `cli-subagents-claude`,
`cli-subagents-codex`, `cli-subagents-antigravity` and `cli-subagents-opencode`
native relay agents. The
main agent invokes them with `invoke_subagent`; each runs `csa run --follow`
in a conversation you can open in the app or the CLI's `/agents` panel. Send
later messages to the same relay to continue the worker conversation.

Each relay runs `csa relay-path --workspace`, creates the returned file with
`write_to_file` (`Overwrite=false`), then passes its path to `csa run --relay-file`.
Answers and follow-ups use fresh files with `csa message --relay-file`. The UTF-8
payload stays out of shell commands. The managed `.csa-relay-inbox/` is inside
the caller's workspace so the file tool uses inherited workspace permissions.
csa writes a `.gitignore` containing `*` there before issuing any payload path,
so leftovers stay out of Git; cleanup keeps that file.
csa validates and deletes each file, limits it to 16 MiB and cleans up stale
payloads after 24 hours on the next allocation. See the
[transport research](research/relay-length-limit.md).

The relay uses Antigravity's `flash` tier; prompt `model` and `effort` options
select the external worker. Native definitions cannot pin an exact model ID,
so check the resolved relay model and effort in the app. See
[host setup](hosts.md#antigravity) and the
[research and app checks](research/antigravity-native-agents.md).

## Using it

```bash
csa run --runtime antigravity --model gemini-3.8-flash-high --access read "Review src/auth.py for bugs"
```

From a host, pass `runtime: "antigravity"` to `subagent_start`. `agy models` lists the model IDs your account can use. `effort` maps to `--effort`, except for model IDs with an effort suffix (`-minimal`, `-low`, `-medium`, `-high`, `-xhigh`, `-max`) or a `claude-` or `gpt-` prefix. For those models, csa omits `--effort` and adds a warning.

| `access` | Antigravity mode |
|---|---|
| `read` | `--mode plan`, behind the review gate: reads, read-only commands and the parent channel |
| `edit` | `--mode accept-edits --sandbox`, behind the review gate: edits in the project, commands in agy's sandbox |
| `full` | `accept-edits` plus `--dangerously-skip-permissions`: no gate, no sandbox |

The review gate answers the permission questions a headless `agy` run can't ask you. See [Safety](#safety).

Antigravity's own auto mode (a reviewer agent for commands) isn't available on every account or on Windows. Where your account has it, set `edit_permission_mode = "auto"` under `[antigravity]` in the config; AGY falls back to its default mode with a warning when it isn't available.

Read jobs can search the web (`search_web`, Google search) and fetch pages (`read_url_content`). In a test on 2026-10-17, a Flash worker fetched a Reddit listing from `www.reddit.com` (`old.reddit.com` sent it to a login page) and found Reddit threads through search. It also found a recent YouTube video with its title, channel, date and description, but couldn't watch it or read a transcript, and presented the description as what was said in the video. Ask for source URLs and check what you rely on.

The whole generated prompt travels as a command-line argument and is limited to 24,000 characters, including injected worker, host, parent channel and safety notes. Point to files instead of pasting them. The job's `timeout_seconds` is passed to `agy` as `--print-timeout`.

## Messages

Workers get `message_parent`, `ask_parent` and `check_parent_messages` from a `csa-parent` MCP server, which CLI Subagents writes into the `config/mcp_config.json` of the folder the worker runs in, with the job's id, before each job. `agy` has no flag for an MCP server added for one run, so workers normally run in folders CLI Subagents manages: your primary account in `~/.cli-subagents/homes/antigravity/primary` (its login stays in the OS keyring, which `agy` finds from any folder), an extra account in its own folder. Your own `~/.gemini` is left alone. The parent channel requires an effective account cap of exactly 1: account `max_jobs` (or `primary_max_jobs` for the primary), then the plan preset, then `max_jobs_per_account`. A higher cap gets no channel, because two jobs could then start from the same file. The primary file-login fallback also has no parent channel; see [Limits](#limits-1).

Jobs started from Claude Code also get that session's MCP servers in the same file (`share_parent_mcp = true`, the default). Your own Gemini servers win when a name appears in both.

Follow-ups still reach an Antigravity worker. A `subagent_message` sent while the job runs is given to the worker by the runner as a follow-up turn once its current turn ends. A `subagent_message` sent to a finished job continues the same conversation as a new job.

## More than one Google account

This is at your own risk. Google's [Antigravity FAQ](https://antigravity.google/docs/faq#can-i-use-antigravity-cli-headless-mode-in-third-party-coding-agents-or-automate-account-switching) says consumer plans don't support automating account switching. Nothing changes until you add an account. Once you do, jobs switch between your accounts automatically, as they do for Claude and Codex; set `failover = "off"` under `[antigravity]` to keep them on your main account.

### How it works

`agy` has no setting for its config folder, and it keeps its login in the OS keyring (Windows Credential Manager, macOS Keychain, Secret Service on Linux), where every login of your user would share it. So each extra account gets its own folder under `~/.cli-subagents/homes/antigravity/`, and `agy` runs with `HOME` and `USERPROFILE` pointing there (on macOS and Linux the XDG folders too). CLI Subagents also sets `SSH_CONNECTION`, `SSH_CLIENT` and `SSH_TTY`. When `agy` thinks it runs over SSH, it stores its login in a file in that folder instead of the keyring, so each folder keeps its own login. Your primary account keeps its keyring login: its jobs run in `homes/antigravity/primary` without the SSH variables, and `agy` finds the keyring login from there.

This relies on `agy` behaviour that Google doesn't document. If a later `agy` stops honouring it, the login won't stick in the folder: `csa doctor --runtime antigravity` then shows the account as not logged in, and its jobs end with the outcome `auth` and the login command to run.

Your setup in `~/.gemini` is linked into each account folder, so there is still one copy of it:

| What | How it gets into the account folder |
|---|---|
| `GEMINI.md`, `AGENTS.md`, `config/GEMINI.md`, `config/AGENTS.md` | hard links |
| `config/rules/`, `antigravity-cli/rules/` | one folder link each |
| `config/skills/`, `antigravity-cli/skills/`, `antigravity-cli/plugins/` | one link per entry; the cli-subagents plugin is left out so workers can't start subagents |
| `config/config.json` | a hard link, unless it sets `enableAdc`, which can select a login of its own (you get a warning instead) |
| `config/hooks.json` | written before each job: your hooks plus the review gate |
| `antigravity-cli/settings.json` | written before each job: your settings with `toolPermission` set to `request-review`; left out when they set `modelProvider` |
| `config/mcp_config.json` | written before each job: your MCP servers (without cli-subagents), the Claude session's servers, and `csa-parent` |

The folder your primary account's jobs run in gets the same setup; there the settings are shared even when they set `modelProvider` or `enableAdc`, since it's the same account. If a folder had its own `settings.json`, `hooks.json` or `mcp_config.json` before, the first job keeps a copy next to it (`*.before-cli-subagents`).

Each account keeps its own login, conversations, project settings (`config/projects/`) and MCP OAuth tokens. Every job refreshes the links; `csa accounts sync NAME --runtime antigravity` does it by hand.

### Steps

1. Run `csa accounts add NAME --runtime antigravity`. It creates the folder, links your setup, prints the risk note and starts `agy` in that account's environment.
2. `agy` prints a Google sign-in link. Open it, sign in with the other account, paste the code back into `agy`, then exit `agy`. Odd characters may show up in the prompt because `agy` queries the terminal when it thinks it runs over SSH; ignore them. To sign in later or again, run `csa accounts login NAME --runtime antigravity`.
3. Run `csa doctor --runtime antigravity`. It lists each account with its folder, whether a login file exists, whether it's enabled, and any cooldown.
4. Pin a job to the account with `account: "NAME"` (`--account NAME` on the command line). Rotation between accounts is on by default; `failover = "off"` under `[antigravity]` turns it off.

With `failover = "on-limit"`, the default, a job goes to the least recently used account that isn't cooling down, the primary included (`include_primary = false` leaves it out). When an account runs out of quota, it cools down and the job starts again on the next account with the partial output attached. A conversation can't move between account folders, so the next account starts a new one. In rotation an account without a login file is skipped; a job pinned to it ends at once with `auth`. With `failover = "off"`, jobs without `account` run on your primary account only.

### Limits

- `agy` reports no usage figures, so rotation picks the least recently used account, not the one with the most quota left.
- MCP servers and hooks run with the account folder as their home folder, so whatever they keep under `~` (caches, their own logins) is separate for each account.
- The quota error wording of `agy` is matched loosely ("quota", "resource exhausted", "rate limit", "429"). If a limit isn't recognised, the job ends with an error instead of moving on.
- `csa launch --runtime antigravity --account NAME` opens an interactive session on an extra account; it doesn't take a job slot.

## Safety

A headless `agy` run can't ask you anything. Without help it refuses every action that needs a yes (shell commands, MCP tool calls, reads outside the project), and often stops right there with an empty answer. CLI Subagents answers those questions with a review gate, so read and edit jobs can work on their own without you approving each step.

### How the gate works

The gate is a `PreToolUse` hook in the worker's folder. `agy` calls it before every tool call, and it answers "approve" or "deny":

1. Fixed rules decide the clear cases at once, with no model call.
   - Approved: reads inside the project (`add_dirs` included), ordinary workspace edits in edit jobs, reading the instruction files and skill folders of the session that started the job (so a worker started from Claude Code can read your `CLAUDE.md`), the `csa-parent` tools and plainly read-only commands such as `git status` or `ls`. In edit jobs, sandboxed commands pass unless the rules flag something risky. Edits to instruction files, `settings.json`, `hooks.json`, `.mcp.json`, CI workflows and editor or container configuration go to review unless a deny rule applies.
   - Denied: logins, keys and tokens (`~/.ssh`, `.env`, `*.pem`, credential stores, agent login files), CLI Subagents' own state, edits to `.git` and to agent configuration (`.agents`, `.gemini`, `.claude`, `.codex`), force pushes, publishing and deploying, deletes outside the project, credential tools, and encoded or downloaded code. In read jobs: every edit.
2. A chain of reviewers judges the rest. Each is a separate model call with no tools that sees the action, a few facts about the job and a written policy, never your task or the conversation, and answers with one JSON object. By default Claude Haiku goes first and Gemini 3.8 Flash (low) through `agy` second. The next reviewer is asked only when one gives no verdict (a limit, a missing login or CLI, an error, a timeout or an answer that isn't the JSON). A denial is final, and so is an approval rated high or critical risk, which counts as a denial. If no reviewer gives a verdict, the action is denied.

A denial goes back to the worker with its reason, and the worker carries on another way. A shell command is reviewed every time it runs, because the scripts and manifests it runs may have changed since. Any other action is reviewed once per job, and a repeat with the same arguments (for an edit, the same content) reuses the answer.

Read-only shell commands use an allowlist of known options. Output files, preprocessors, external diff and textconv helpers, indirect file lists, and unknown options go to review, including attached forms such as `sort -oFILE`. Ordinary commands such as `git status`, `git log`, `git diff`, `Get-Content README.md`, `ls` and `dir` still pass. `rg pattern README.md` passes when the named file exists. Searches of directories (`rg pattern .`, `grep -r`), content globs (`cat **/*`), pipelines, and native directory-wide content searches (`grep_search`) go to review in read jobs because they can reach secret files beyond the literal paths in the call. The same shell searches go to review in edit jobs with `sandbox = false`. Each path in a PowerShell list (`a,b`) and the target of a provider path (`FileSystem::C:\x`) is checked on its own. Network and device paths (`\\server\share`, `\\?\`, `\\.\`, `\??\`) outside the job's folders are denied without being opened, because opening one connects to that server from your machine. Name individual files for reads that need no review. With the reviewer off, these broader reads are denied.

Secret-name checks include Windows stream aliases (`.env::$DATA`, `.env:stream`), case differences and trailing dots or spaces. Ambiguous DOS short names such as `CREDEN~1.JSO` are denied.

The shared shell parser also sends single-quoted commands and shell escapes it cannot interpret consistently to review. Globs for commands such as `find`, `fd`, `tree` and `sort` need review because a matching filename could be interpreted as a write or execution option.

If anything goes wrong, the gate fails closed: a missing or broken job file, unreadable input, or a crash means deny. The worker folders keep `toolPermission = "request-review"`, so if the hook doesn't load at all, `agy` falls back to refusing those actions instead of running them.

### The sandbox

Edit jobs run with `agy --sandbox`. Commands then run in an OS sandbox: they can write only to the project and the temp folders, can't read the rest of your home folder, and have no network. A command that needs more (installing packages, `git push`) asks to leave the sandbox, and the reviewer decides, as in Codex. Checked on Windows with agy 1.3.6; macOS and Linux follow Google's docs but are untested here.

The sandbox doesn't protect `.git` or agent settings inside the project. In edit jobs, the gate sends sandboxed commands that name them (`.git/hooks`, `core.hooksPath` and the like) to the reviewer, but a disguised write can get through. In read jobs, interpreter code and other commands that aren't plainly read-only always go to review, even if sandboxing is enabled. A sandbox that permits workspace writes cannot enforce read access or prevent a script from installing hooks that run later.

### Compared with Codex and Claude Code

- Codex auto-review: the OS sandbox holds every command, and a second model judges only what wants out of it, with the conversation in view. Edit jobs here work the same way, except the reviewer doesn't see the conversation.
- Claude Code auto mode: a classifier judges risky actions as they come, with your messages in view; there is no sandbox by default. Read jobs here work like that, with fixed rules in front.
- Because the reviewer doesn't see your task, it can't tell that you asked for something consequential. A push or an MCP call that sends or changes things outside your machine is denied unless it looks plainly routine. Use `access: "full"` for jobs that need those.

### Cost

Reads, ordinary workspace edits, read-only commands and sandboxed commands that pass the fixed rules cost nothing extra. Instruction, settings, CI, hook and editor configuration edits that go to review cost a reviewer call. With the default chain, each reviewed action costs one Claude Haiku call on your primary Claude account. Only when Claude gives no verdict does it also cost an `agy --mode plan` call on the job's own account with the cheapest Gemini model: about 12,000 input tokens (mostly `agy`'s own system prompt), under 100 output tokens and about 10 seconds, more when Google's service is slow. The `agy` reviewer normally runs from a bare folder without your MCP servers and user hooks (an extra account's plugins still load there). If an extra account cannot be linked into that folder, csa warns and runs the reviewer from the worker's folder. The gate denies the reviewer every tool in either folder.

### Watching it

`csa follow` and the live view of a running job print one `review:` line per decision, except routine approvals of reads and edits. The job folder keeps every decision in `review.jsonl`.

### Settings

The reviewer chain is shared with the OpenCode gate under `[review]`, or set for this gate alone with `[[antigravity.reviewers]]`. Each entry takes `provider` (`claude`, `antigravity` or `codex`), `model`, `account` and `timeout`. Claude Haiku first with Gemini Flash as the fallback, which is also the default:

```toml
[[review.reviewers]]
provider = "claude"
model = "haiku"

[[review.reviewers]]
provider = "antigravity"
model = "gemini-3.8-flash-low"
```

The older `reviewer` (`"agy"`, `"claude"`, `"codex"` or `"off"`), `reviewer_model` and `reviewer_account` keys under `[antigravity]` still work as a one-entry chain. `sandbox = false` sends every non-read-only command to the reviewers. See [Review gate](configuration.md#review-gate) for the details, including how Codex runs as a reviewer.

### Limits

- `access: "full"` skips the gate and the sandbox.
- If your primary account's login is a token file in your home folder (`agy` does that when it finds no keyring, as on many Linux servers), its jobs keep running in your real home without the gate, the parent channel or `--sandbox`. Extra accounts always use the gate for read and edit jobs.
- In edit jobs, broad content searches can still show lines from secret files. Read jobs send these searches to review; the reviewer must assess whether their scope excludes secrets.
- MCP servers that need their own OAuth login are signed in per folder, so they may ask again in a worker folder.
- `csa launch` on an extra account opens `agy` in a folder that has the gate's hook; outside a job the hook stays out of the way and `agy` asks you as usual.
- Jobs on your primary account keep their conversations in their folder, not in your own `agy` history.

## Quota pools and Argon

On Google AI Pro and Ultra, Gemini 4 Argon has a separate quota that resets weekly. Gemini 3.8 Flash uses the normal Gemini bucket, which refills every 5 hours until its weekly limit. An Argon limit should therefore leave Flash available on the same account.

agy reports no remaining quota. csa knows only recorded job starts and usage-limit failures. Configure separate `argon` and `flash` pools to cap concurrency and track a rolling weekly job budget; see the [pool example](configuration.md#model-pools). These controls are off until configured. A pooled quota failure cools down only that account's pool, using a reported reset time or its configured cooldown. With failover enabled, the job moves to another account that has room in the pool.

The example matches `gemini-4-argon-*` and budgets 3 Argon jobs per account in 7 days, with at most 1 running on each account and 2 across accounts. Flash allows 6 across accounts. These are example caps, not Google's published job allowances. The assumed Argon IDs are `gemini-4-argon-low`, `gemini-4-argon-medium` and `gemini-4-argon-high`; use the IDs your agy version lists. They have not been tested live here.

`csa accounts --runtime antigravity`, `subagent_accounts`, doctor and the `Room:` line show each pool's running jobs, weekly count and cooldown. When every eligible account has used its Argon budget, the refusal names `weekly_jobs in pool argon` and suggests an available alternative.

## The standalone adapter

[`adapters/antigravity/`](../adapters/antigravity/) is the original Windows MCP server for AGY. It runs `agy` through ConPTY (older versions printed nothing to a pipe) and routed shell commands through its own reviewer and a signed approval broker. The review gate above replaces that reviewer, and current `agy` versions print to a pipe, so the core runtime is the one to use.
