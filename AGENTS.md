# AGENTS.md

Notes for AI agents that install CLI Subagents for a user or work on this repository.

## Installing for a user

Run these yourself; none of them is a slash command.

```bash
pipx install git+https://github.com/lunati228/CLI-Subagents   # or: pip install --user git+...
csa --version
csa setup --dry-run --account <name>=<folder> ...
csa setup --account <name>=<folder> ...
csa doctor --runtime <runtime>   # repeat for each runtime set up
```

Plain `csa doctor` checks Claude only. Run `csa doctor --runtime <runtime>` for each runtime set up (`claude`, `codex`, `antigravity`).

`csa setup` installs the plugin into every installed host (`--host claude|codex|antigravity` limits it), registers and syncs account folders that are already logged in, and prints a "Still needed from you" list. It is safe to run again. Use a `codex:` or `claude:` prefix on `--account` when the folder's runtime is ambiguous.

On Windows 11 24H2/25H2 and later, `csa setup` also installs the [sandbox](docs/sandbox.md) for OpenCode shell commands: it downloads Microsoft's MXC release once (about 540 MB), verifies it and keeps about 37 MB. Tell the user before running it; `--no-sandbox` skips it, and `[sandbox] enabled = false` turns the sandbox off.

After setup, offer the optional [delegation rules](docs/delegation.md) template. It tells the main agent which models to use per tier and keeps work projects with the home vendor. Personal projects can try another CLI first, with helper limits and a fallback to the home model when there is no room. Ask whether the user wants it and which home and try-first models they want for each tier. If they agree, run `csa rules --show` with their values, then install; otherwise skip it. Example: `csa rules --install --target claude --home-high "Opus 5.5" --try-low "Gemini 3.8 Flash high" --try-medium "Sol 6.1 high"`. Use their host and models. Nothing installs the rules without `--install`.

What it runs per host, if you need to do it by hand:

- Claude Code: `claude plugin marketplace add lunati228/CLI-Subagents`, then `claude plugin install cli-subagents@cli-subagents`. Check with `claude plugin list`.
- Codex: `codex plugin marketplace add lunati228/CLI-Subagents`, then `codex plugin add cli-subagents@cli-subagents`. Check with `codex plugin list`.
- Antigravity: `agy plugin install https://github.com/lunati228/CLI-Subagents`. Check with `agy plugin list`.

When `claude` isn't found (the Claude desktop app often has no `claude` on PATH) or its plugin commands fail, `csa setup` merges `extraKnownMarketplaces.cli-subagents` (GitHub repo `lunati228/CLI-Subagents`) and `enabledPlugins["cli-subagents@cli-subagents"] = true` into `settings.json` in `CLAUDE_CONFIG_DIR` or `~/.claude`. It keeps a backup next to the file, and Claude Code installs the plugin on its next start. `--claude-route settings` forces this route.

To update, run `csa update` (`--dry-run` first if you like, `--no-package` for plugins only). It reinstalls the package with pipx, uv or pip, whichever installed it, and runs `claude plugin marketplace update cli-subagents` then `claude plugin update cli-subagents@cli-subagents`, `codex plugin marketplace upgrade cli-subagents` then `codex plugin add cli-subagents@cli-subagents`, or `agy plugin install <repo URL>` again. On Windows it prints the package command instead of running it; run that command yourself. Then the user restarts the CLI.

If the plugin can't be installed at all, register only the MCP server: `claude mcp add --scope user cli-subagents -- csa mcp` or `codex mcp add cli-subagents -- csa mcp`. That loses the skill, `/cli-subagents:delegate` and the wake-up hooks.

Only the user can do these: browser logins (`csa accounts login <name>`, `claude` then /login, `codex login`, the first `agy` sign-in), approving permission prompts, and restarting the CLI or typing `/reload-plugins` so the plugin loads. Tell them which ones are left.

Don't read, copy or print credential files. Don't edit files in `~/.cli-subagents/homes/`; `csa` rebuilds them. Don't run `csa doctor --live` or real jobs unless the user agrees, because they use quota.

## Working on the code

Python 3.11+, standard library only. Run the tests from the repository root:

```bash
# Windows (cmd): set PYTHONPATH=src;tests    PowerShell: $env:PYTHONPATH="src;tests"
export PYTHONPATH=src:tests
python -m unittest discover -s tests -t tests
```

When you change anything the plugin ships (agents, skills, commands, hooks, the MCP server or the `csa` code it calls), raise `version` in `.claude-plugin/plugin.json`, `pyproject.toml` and `src/cli_subagents/__init__.py` together. Claude Code only updates an installed plugin when that version changes.

The tests use fake CLIs (`tests/fake_claude.py`, `fake_codex.py`, `fake_agy.py`, `fake_host.py`) and temporary folders. Keep it that way: tests must never call the real `claude`, `codex` or `agy`, or touch the real `~/.claude`, `~/.codex` or `~/.gemini`. Set `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and `CLI_SUBAGENTS_HOME` to temporary folders when a test needs them.

## Layout

| Path | Contents |
|---|---|
| `src/cli_subagents/cli.py` | The `csa` command |
| `src/cli_subagents/setup.py` | `csa setup` and `csa update`: plugin install and update per host, account registration |
| `src/cli_subagents/jobs.py`, `runner.py` | Job records and the background runner |
| `src/cli_subagents/accounts.py`, `config.py` | Account pool, cooldowns, `~/.cli-subagents/config.toml` |
| `src/cli_subagents/sync.py`, `fsutil.py` | Linking the shared setup into account folders |
| `src/cli_subagents/mcp_server.py`, `tools.py`, `messages.py`, `hooks.py` | MCP server, tools, parent messages, Claude Code hooks |
| `src/cli_subagents/runtimes/` | Claude Code, Codex and Antigravity adapters |
| `src/cli_subagents/agy_review/` | The review gate for Antigravity workers (PreToolUse hook, fixed rules, reviewer, policy). Self-contained: no imports from the rest of the package |
| `src/cli_subagents/sandbox/` | The MXC sandbox for workers on Windows: pinned release and probe (`mxc.py`), install for setup/update/doctor (`install.py`), policy generator (`policy.py`), neutral policy model (`model.py`), backend adapters (`backends/`), shared launcher (`launcher.py`), agent integrations (`integrations/`) and the in-container `taskkill` stand-in. Tests use `tests/fake_wxc.py`, never the real `wxc-exec.exe` |
| `.claude-plugin/`, `plugin.json`, `mcp_config.json`, `hooks/`, `commands/`, `skills/` | Plugin files for Claude Code, Codex and Antigravity |
| `agents/` | Claude Code relay agents (`cli-subagents:cli-subagents-claude` and so on) that run `csa run --follow` |
| `src/cli_subagents/follow.py` | `csa follow`: one readable line per worker event, with end markers for the relay agents |
| `adapters/antigravity/` | The older standalone Antigravity server |
| `docs/` | Guides, design notes and research |
