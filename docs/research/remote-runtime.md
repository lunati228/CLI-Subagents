# Remote runtime: prior art and decisions (2026-11-11)

## Decision

Add a `remote` runtime: Claude Code runs unmodified on a server, in the user's own account folders there, logged in
with Claude Code's own `/login`. The job's file and shell tools act on the user's PC through an MCP server on the PC,
reached through the job's own ssh connection (a reverse port forward). The csa-parent tools travel the same way, so a
remote worker can message and ask the main session.

## Scope and constraints

- Standard library only (the rest of csa has no dependencies), Windows and Linux.
- Anthropic's Claude Code legal page: subscription logins may be used by Claude Code itself, not by other clients or
  relays. So no code may read, copy, store or send a Claude login token; logins happen only through `claude /login`
  in the account folder on the server; nothing rewrites requests or poses as another client.
- One person, own accounts: a job runs only on accounts owned by the ssh user that sent it.
- No account switch in the middle of a job or across its follow-ups.

## Prior art

| Project | What it does | License | Reuse |
|---|---|---|---|
| [sparfenyuk/mcp-proxy](https://github.com/sparfenyuk/mcp-proxy) | Bridges stdio and SSE/Streamable HTTP MCP transports both ways; named servers under `/servers/<name>/` | MIT | Idea only: named stdio servers on their own paths. A dependency doesn't fit a stdlib-only package |
| [supercorp-ai/supergateway](https://github.com/supercorp-ai/supergateway) | Runs stdio MCP servers over Streamable HTTP, SSE or WebSocket; stateless by default, one process per SSE connection | MIT | Idea only: one child per served server, POST-only JSON replies (GET and DELETE answered 405) |
| Hosting the official binary with native login (claude-code-telegram, CloudCLI, HolyClaude) | Drive `claude` through the Agent SDK or `claude -p`, inheriting the host's own login | none / AGPL-3.0 / MIT | No code; confirms the pattern of driving the real binary with `-p`, stream-json and `--resume` |

What the proxies showed: the simplest correct shape for one MCP client is one long-lived child per proxied server,
JSON-RPC requests forwarded line by line and replies matched by id. Because several HTTP connections may send
requests at once (Claude Code can call tools in parallel, and `ask_parent` blocks for minutes), the proxy here
replaces request ids with its own and restores them in the reply. Requests the child sends to the client (sampling,
roots) can't be answered over JSON responses, so they get "method not found"; notifications from the child are
dropped.

## Design

| Part | Where | File |
|---|---|---|
| Runtime adapter: ssh lookup, account status and login over ssh, bundle in `prepare`, launch of the relay | PC | `runtimes/remote.py` |
| Relay: bridge, ssh with `-R 127.0.0.1:0:127.0.0.1:<port>`, wire protocol, live follow-ups, resume transcript | PC | `remote/relay.py` |
| PC bridge: `pc_*` tools confined to the project, read-only mode, proxied stdio servers at `/mcp/<name>` | PC | `remote/bridge.py`, `remote/stdio_proxy.py` |
| Setup bundle without login data | built on the PC, installed on the server | `remote/bundle.py` |
| Server: forced command, ownership check, `claude -p` with stream-json in and out, transcript cleanup | server | `remote/server.py` |
| Wire format | both | `remote/protocol.py` |

Decisions and why:

- The runner's parser stays `ClaudeParser`: the relay prints the server's Claude Code events unchanged, plus a few
  `csa.remote` events of its own (accepted, delivered, warning, error).
- csa picks the account (its usage data, caps and leases stay right) and names it in the job; the server only checks
  that the ssh user owns it. Without a named account the server picks one itself.
- `failover` is fixed to `"off"` for `remote`, and the adapter has no `transfer_session`, so the runner keeps resumes on
  the account that holds the session.
- Follow-ups from the main session go into the running conversation as user turns (Claude Code's stream-json input).
  A follow-up is marked read only when the server confirms it went in; one that arrives after the conversation ended
  stays unread, and the runner's follow-up turn delivers it.
- Answers to `ask_parent` stay with ask_parent: the relay sends only follow-ups.
- The relay runs as `python -I relay.py <job file>`, like csa's other host helpers, so a module in the project folder
  can't shadow its imports.

## Unresolved risks and validation

- Not yet checked live: OpenSSH reporting "Allocated port N for remote forward" for `-R 127.0.0.1:0:...` with a
  forced command and `permitlisten`. The earlier standalone version's live check ran the real Claude Code against
  the bridge directly, without ssh; the tests here use a fake ssh.
- PC-only MCP servers of the user (stdio servers with local paths) are left out of the bundle. The proxy can carry them
  under their own names later.
- Live check with a real server and real accounts: pending, needs the user's go (it uses quota).
