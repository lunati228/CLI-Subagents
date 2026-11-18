# Installing CLI Subagents (for AI agents)

CLI Subagents is an MCP server with a plugin for Claude Code, Codex and Antigravity. It needs Python 3.11 or newer and has no dependencies.

1. Install the `csa` command:

   ```bash
   pipx install git+https://github.com/lunati228/CLI-Subagents
   ```

   Without pipx: `pip install --user git+https://github.com/lunati228/CLI-Subagents`, then make sure the Python scripts folder is on PATH. Check with `csa --version`.

2. Install the plugin into the installed host CLIs and register any extra account folders the user named:

   ```bash
   csa setup --dry-run --account <name>=<folder>
   csa setup --account <name>=<folder>
   ```

3. Offer the optional [delegation rules](docs/delegation.md) template. It tells the main agent which models to use per tier and keeps work projects with the home vendor. Personal projects can try another CLI first, with helper limits and a fallback to the home model when there is no room.

   Ask whether the user wants it and which home and try-first models they want for each tier. If they agree, preview with `csa rules --show` and their values, then install. Otherwise skip it. For example:

   ```bash
   csa rules --install --target claude --home-high "Opus 5.5" --try-low "Gemini 3.8 Flash high" --try-medium "Sol 6.2 medium"
   ```

   Use their host and models. Add `--work-folder` for each work folder they want listed. Nothing installs these rules without `--install`.

4. Tell the user the "Still needed from you" items it prints (browser logins, restarting the CLI).

5. Run `csa doctor --runtime <runtime>` for each runtime set up (`claude`, `codex`, `antigravity`). Plain `csa doctor` checks Claude only. To update later, run `csa update`.

For an MCP client without a plugin system (Cline, Cursor and others), add a stdio server instead of step 2:

```json
{ "mcpServers": { "cli-subagents": { "command": "csa", "args": ["mcp"] } } }
```

Then copy the whole `skills/cli-subagents/` folder into the client's skills so the agent gets `SKILL.md` and the `hosts/*.md` notes it links to. If the client accepts only rules text, inline `SKILL.md` and the relevant host note. The full steps and the manual commands per host are in the README's "For AI agents" section and in [AGENTS.md](AGENTS.md).
