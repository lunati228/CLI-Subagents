# Remote: Claude Code on your server, tools on your PC

The `remote` runtime makes an account on a server one more seat in CLI Subagents. The server runs the unmodified
Claude Code, in your own account folders there, each logged in with Claude Code's own `/login`. A job's file and shell
tools work in the project folder on your PC, through the job's own ssh connection. The worker gets the same skills,
agents, instructions and portable MCP servers as a local worker, and it can message and ask the main session.

Use it when you want jobs to run on a machine that stays on, or on accounts you keep logged in there, while the code
stays on your PC.

## What runs where

| Part | Runs on | What it does |
|---|---|---|
| Relay (`remote/relay.py`) | PC | Started by csa's runner for each attempt. Serves the project through the bridge, opens `ssh`, sends the job, passes the server's events to csa and your follow-ups to the server |
| Bridge (`remote/bridge.py`) | PC | MCP server on `127.0.0.1` with `pc_read`, `pc_write`, `pc_edit`, `pc_list`, `pc_glob`, `pc_grep` and `pc_run`, confined to the project folder. Also serves csa-parent at `/mcp/csa-parent` |
| Setup bundle (`remote/bundle.py`) | built on the PC | Your skills, agents, commands, rules, output styles, workflows, `CLAUDE.md`, settings without login settings, portable MCP servers and enabled plugins |
| Server (`remote/server.py`) | server | The ssh forced command. Checks the account belongs to you, installs the bundle, runs `claude -p` with stream-json in and out, deletes old transcripts |

The server reaches the bridge as `http://127.0.0.1:<port>/mcp` through a reverse forward of the job's ssh session,
so the bridge listens only on your PC's loopback address and needs no open port. Each job gets a new random token
for the bridge.

## Rules it keeps

These follow Anthropic's terms for Claude Code subscriptions:

- No code here reads, copies, stores or sends a Claude login. You log in on the server with Claude Code's own
  `/login`, in that account's folder. The bundle leaves out `.credentials.json`, `.claude.json`, login settings and
  login environment variables.
- A job runs only on accounts that belong to the ssh user who sent it. The server's config maps each account to its
  owner, and the server checks the account csa names.
- A job and its follow-ups stay on one account. `failover` is always `"off"` for this runtime; a job that hits a usage
  limit ends, and the account cools down like any other.
- Nothing rewrites requests or poses as another client: the server runs the official `claude` binary.

## Set up the server

You need a Linux machine with Python 3.11+, OpenSSH, Claude Code and CLI Subagents (`pipx install
git+https://github.com/lunati228/CLI-Subagents`).

1. Give each person their own Unix user (for example `csa-alice`) with its own `authorized_keys`. A job runs as that
   person's user, so their account folders, bundles, jobs and usage log belong to them alone. The server refuses an
   account folder that isn't owned by the user running the job, or that other users can read (it must be mode 0700).

2. Write `/etc/cli-subagents/remote.toml`, readable by every person's user
   ([example](../examples/remote/remote.example.toml)):

   ```toml
   [server]
   state_dir = "/var/lib/cli-subagents-remote"
   claude = "claude"            # or the full path, or a list such as ["/usr/local/bin/claude"]
   transcript_days = 7          # transcripts and job folders older than this are deleted
   max_job_minutes = 180        # a job stops after this long

   [[account]]
   name = "alice-max"           # the name csa uses for this account
   owner = "alice"              # the --user of the person whose key may use it
   home = "/home/csa-alice/accounts/alice-max"
   ```

   Create `state_dir/users/alice/` for that person's user, owned by it.

   Optional tiers set limits per group of people: `[tier.<name>]` with the models allowed and a default model, jobs at
   once across the person's accounts, the longest job and a weekly cost cap from the usage log (list prices), and
   `[users]` mapping each person to a tier. People without an entry get the tier named `default`, if there is one. The
   example config shows all keys. A refused job ends in csa as an error (`model_not_allowed`, too many jobs) or, at the
   weekly cap, with outcome `budget`. Model names must match exactly, so set `default_model` under `[remote]` on the
   PC, or leave the model out and let the tier pick its default.

3. Give the key a forced command in that user's `authorized_keys`, so it can start jobs, open the reverse forward
   and sign in, and nothing else (`pty` is for the sign-in):

   ```text
   restrict,pty,port-forwarding,permitlisten="127.0.0.1:*",command="csa remote serve --user alice" ssh-ed25519 AAAA...
   ```

   Use the full path of `csa` if sshd's `PATH` doesn't have it. `--config PATH` points at another config file.
   In `sshd_config`, let these users open reverse forwards only, so no `-L` reaches other ports on the server:

   ```text
   Match User csa-*
       AllowTcpForwarding remote
       PermitOpen none
       X11Forwarding no
   ```

4. Optional, for a server that runs only these jobs: copy
   [`examples/remote/claude-code/CLAUDE.md`](../examples/remote/claude-code/CLAUDE.md) to `/etc/claude-code/CLAUDE.md`
   and [`managed-settings.json`](../examples/remote/claude-code/managed-settings.json) to
   `/etc/claude-code/managed-settings.json`. The first is Claude Code's machine-wide instruction file: it tells every
   job where the project is, which tools to use and that refusals are final. The second denies Claude Code's own file
   and shell tools for every account, whatever a person's settings say. The job's flags turn those tools off as well;
   the managed settings keep them off even if a flag is missed. Both apply to every Claude Code on the machine.

5. Optional: a daily timer that runs `csa remote serve --cleanup`. Every job start cleans up that person's files too,
   at most once an hour.

The forced command takes three requests: `job` (one job over stdin and stdout), `login ACCOUNT` (Claude Code in that
account's folder with no tools and no MCP servers, for `/login`) and `status [--json]` (your accounts with login state,
usage and running jobs). Each bundle is filtered again when it arrives, so login settings, another API address, hooks
and MCP servers that point at the server itself are dropped whatever the PC sent.

## Set up your PC

```toml
# ~/.cli-subagents/config.toml
[remote]
server = "csa-alice@ai-server"      # your server user @ host, or a Host alias from ~/.ssh/config
# ssh_key = "~/.ssh/ai_server_ed25519"
# primary = "~/.claude"             # the Claude setup that goes to the server (default: your Claude folder)
```

Then add each account under the name the server uses, and log it in once:

```bash
csa accounts add alice-max --runtime remote
```

`add` opens `ssh -t csa-alice@ai-server login alice-max`. Claude Code starts on the server: type `/login`, sign in in
your browser, then `/exit`. `csa accounts login alice-max --runtime remote` does the same later. `csa doctor --runtime
remote` shows each account's login state and usage as the server reports them.

## Using it

Start jobs as usual with `runtime: "remote"` (`subagent_start`, `csa run --runtime remote`), or in Claude Code with
the `cli-subagents:cli-subagents-remote` agent. Options work as for Claude workers: `model`, `effort`, `access`,
`account`, `max_turns`, `timeout`.

- `access=read`: the bridge offers only `pc_read`, `pc_list`, `pc_glob` and `pc_grep`.
- `access=edit` (and `full`, which means the same here): all `pc_*` tools. `pc_run` runs commands on your PC, in the
  project, with your rights. The bridge refuses
  secret files (`.env*`, keys, `.ssh`, credential files) and commands on its deny list (deleting outside the
  project, pushing, uploads, reading secret paths) at every level.
- On the server Claude Code gets only `WebSearch`, `WebFetch`, `TodoWrite` and `ToolSearch` of its own tools; its file
  and shell tools are off, so the job can't touch the server's disk.
- The worker reads your project's `CLAUDE.md`, `.claude/CLAUDE.md`, `CLAUDE.local.md` or `AGENTS.md` with `pc_read`:
  Claude Code on the server doesn't load project files from your PC by itself, so the worker's first message tells it
  which ones exist.

### Messages

The worker has `message_parent`, `ask_parent` and `check_parent_messages`, served by csa on your PC through the
bridge. A follow-up you send with `subagent_message` while the job runs goes straight into the running conversation
as a new user turn. One that arrives after the conversation ended is delivered in a follow-up turn on the same
account, as for local workers.

### Follow-ups after the job ended

`subagent_message` on a finished job starts a new job that continues the same session on the same account. The server
keeps transcripts for `transcript_days`. After that, csa sends the conversation from its own job records (up to
400 KB, oldest part cut first) and the server starts a new session with it.

## Wire format

One JSON object per line over the ssh session's stdin and stdout. See `remote/protocol.py` for the checks.

PC to server:

```json
{"type": "job", "version": 1, "job_id": "<uuid4>", "account": "alice-max", "prompt": "...",
 "model": null, "effort": null, "max_turns": null, "permission_mode": "acceptEdits",
 "workspace": {"mode": "pc", "label": "my-app", "bridge_port": 41234, "bridge_token": "<random>",
               "proxies": ["csa-parent"]},
 "bundle": {"sha256": "<hex>", "size": 12345},
 "resume": null}
{"type": "bundle", "sha256": "<hex>", "data": "<base64 zip>"}
{"type": "message", "id": "<id>", "text": "..."}
{"type": "cancel"}
```

`account` is optional: without it the server picks the sender's account with the most room. `resume`, when set, is
`{"session_id": "...", "account": "...", "history_md": "..."}`.

Server to PC:

```json
{"type": "need_bundle", "sha256": "<hex>"}
{"type": "accepted", "job_id": "...", "account": "alice-max", "session_id": "...", "resumed": false}
{"type": "event", "event": {"...one Claude Code stream-json event...": "..."}}
{"type": "message_status", "id": "<id>", "delivered": true}
{"type": "warning", "text": "..."}
{"type": "done", "job_id": "...", "exit_code": 0, "is_error": false, "result": "...", "session_id": "...",
 "account": "...", "usage": {}, "cost_usd": 0.0}
{"type": "error", "code": "bad_request|no_account|all_limited|account_not_owned|session_account_busy|claude_failed|model_not_allowed|cap_reached|internal",
 "message": "..."}
```

`done` or `error` is always the last line.

## Limits and known gaps

- MCP servers that only run on your PC (stdio servers with local paths) are left out of the bundle, with a warning in
  the job. Servers started with `npx`, `uvx`, `docker` and the like, and remote HTTP servers, go along.
- Hooks, the status line and other settings that run commands on your PC are left out.
- The server runs at most two jobs per account at once, and stops a job after `max_job_minutes`.
- csa's spend caps are for local Claude accounts only.
- Not yet checked against a real server: OpenSSH reporting the allocated port for `-R 127.0.0.1:0:...` under a forced
  command. The tests use a fake ssh; the bridge and the server's Claude Code flags were checked with the real Claude
  Code.

## Files

| Path | Contents |
|---|---|
| `jobs/<job_id>/remote/job.json` | What the relay needs for the current attempt (no login data; the bridge token is made by the relay and never written) |
| `jobs/<job_id>/remote/pc-tools.jsonl` | Every `pc_*` call (tool, path or command, allowed or refused) and the name of every csa-parent tool called |
| `state/remote/status.json` | The server's last `status` answer, kept for two minutes |
| On the server: `state_dir/users/<person>/usage.jsonl` | One line per job: person, account, model, tokens, cost and ISO week; no prompt or reply text |
