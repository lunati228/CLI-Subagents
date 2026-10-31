"""`csa setup`: install the plugin into the host CLIs and register existing account folders.

Plugins use each host's own terminal commands (`claude plugin ...`,
`codex plugin ...`, `agy plugin ...`), so an agent can do the whole install
without slash commands. Codex native agents are installed separately in its
personal agents folder. Antigravity relays use a companion plugin in its global
customizations folder. When the `claude` binary can't be found, the Claude Code
plugin is declared in the primary `settings.json` instead; Claude Code installs
it on its next start. Logins are never run and credential files are never read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.resources import files
import json
import os
from pathlib import Path
import shutil
import subprocess
import sysconfig
import time
from typing import Any

from cli_subagents.config import NAME_RE, Account, Config, save_config
from cli_subagents.fsutil import (copy_exclusive, find_program, is_link, no_window_kwargs, same_path, write_json,
                                  write_private)
from cli_subagents.runtimes import get_runtime
from cli_subagents.runtimes.base import JobSpec, clean_environment

REPO = "lunati228/CLI-Subagents"
REPO_URL = f"https://github.com/{REPO}"
MARKETPLACE = "cli-subagents"
PLUGIN = "cli-subagents"
PLUGIN_ID = f"{PLUGIN}@{MARKETPLACE}"
ANTIGRAVITY_RELAYS = "cli-subagents-relays"
HOSTS = ("claude", "codex", "antigravity")
WORKER_RUNTIMES = (*HOSTS, "opencode")
LABELS = {"claude": "Claude Code", "codex": "Codex", "antigravity": "Antigravity"}
SHORT = {"claude": "claude", "codex": "codex", "antigravity": "agy"}
RELOAD = {
    "claude": "Restart Claude Code (or type /reload-plugins in an open session) so the cli-subagents plugin loads.",
    "codex": "Start a new Codex session so the cli-subagents plugin loads.",
    "antigravity": "Start a new Antigravity session so the cli-subagents plugin loads.",
}
FALLBACK = {
    "claude": "claude mcp add --scope user cli-subagents -- csa mcp",
    "codex": "codex mcp add cli-subagents -- csa mcp",
    "antigravity": 'add {"mcpServers": {"cli-subagents": {"command": "csa", "args": ["mcp"]}}} to ~/.gemini/config/mcp_config.json',
}
# Files that mark a folder as one CLI's config folder. Only their existence is checked.
MARKERS = {
    "claude": (".claude.json", "projects", "settings.json", "statsig"),
    "codex": ("config.toml", "sessions", "auth.json", "version.json"),
}


@dataclass
class Report:
    dry_run: bool = False
    done: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    todo: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add_todo(self, text: str) -> None:
        if text not in self.todo:
            self.todo.append(text)


def run_command(argv: list[str], env: dict[str, str] | None, timeout: int = 300) -> tuple[int, str, str]:
    """Run one host command without a terminal. Returns exit code, stdout and stderr."""

    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env, stdin=subprocess.DEVNULL, **no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _json(text: str) -> Any:
    """The JSON value in a command's stdout, ignoring any warning lines before it."""

    for start, char in enumerate(text):
        if char in "[{":
            try:
                return json.loads(text[start:])
            except ValueError:
                continue
    return None


def _last_line(stdout: str, stderr: str) -> str:
    lines = [line.strip() for line in (stderr + "\n" + stdout).splitlines() if line.strip() and not line.startswith("WARNING")]
    return lines[-1][:300] if lines else ""


def _items(data: Any) -> list[dict[str, Any]]:
    """Every dict in a JSON value (list outputs and {"installed": [...]} wrappers alike)."""

    found: list[dict[str, Any]] = []
    if isinstance(data, dict):
        found.append(data)
        for value in data.values():
            found += _items(value)
    elif isinstance(data, list):
        for value in data:
            found += _items(value)
    return found


def _find_plugin(data: Any, host: str) -> dict[str, Any] | None:
    for item in _items(data):
        if host == "antigravity":
            if str(item.get("name", "")).lower() == ANTIGRAVITY_RELAYS:
                continue  # The companion registers agents, not the main plugin's MCP server.
            names = [str(item.get(key) or "") for key in ("name", "id", "pluginId", "source", "url", "path", "installPath")]
            if any(PLUGIN in name.lower() for name in names) and ("name" in item or "id" in item):
                return item
        elif item.get("id") == PLUGIN_ID or item.get("pluginId") == PLUGIN_ID:
            return item
    return None


def _has_marketplace(data: Any) -> bool:
    return any(item.get("name") == MARKETPLACE for item in _items(data))


# Hosts -----------------------------------------------------------------------


def detect_hosts(config: Config) -> list[str]:
    """Hosts whose CLI is installed (Claude Code also counts when only its config folder exists)."""

    found = []
    for host in HOSTS:
        if get_runtime(host).find_binary(config.runtime(host)):
            found.append(host)
        elif host == "claude" and config.runtime("claude").primary.is_dir():
            found.append(host)
    return found


def _host_env(config: Config, host: str) -> dict[str, str]:
    runtime_config = config.runtime(host)
    if host == "antigravity":
        return clean_environment()
    return get_runtime(host).account_env(runtime_config, runtime_config.primary_account())


def _plugin_state(host: str, command: list[str], env: dict[str, str]) -> dict[str, Any]:
    """What the host already has: installed (True/False/None if unknown), enabled, marketplace."""

    code, stdout, _ = run_command([*command, "plugin", "list", "--json"], env, timeout=120)
    data = _json(stdout) if code == 0 else None
    state: dict[str, Any] = {"installed": None, "enabled": None, "marketplace": None, "name": None}
    if data is not None:
        item = _find_plugin(data, host)
        state["installed"] = bool(item and item.get("installed", True))
        state["enabled"] = bool(item and item.get("enabled", True))
        state["name"] = item.get("name") if item else None
    if host in ("claude", "codex") and not state["installed"]:
        code, stdout, _ = run_command([*command, "plugin", "marketplace", "list", "--json"], env, timeout=120)
        data = _json(stdout) if code == 0 else None
        state["marketplace"] = _has_marketplace(data) if data is not None else None
    return state


def _plugin_steps(host: str, state: dict[str, Any]) -> list[list[str]]:
    if state["installed"] and state["enabled"]:
        return []
    if host == "claude":
        if state["installed"]:
            return [["plugin", "enable", PLUGIN_ID]]
        steps = [] if state["marketplace"] else [["plugin", "marketplace", "add", REPO]]
        return steps + [["plugin", "install", PLUGIN_ID]]
    if host == "codex":
        # `codex plugin add` also re-enables a disabled plugin.
        steps = [] if state["marketplace"] or state["installed"] else [["plugin", "marketplace", "add", REPO]]
        return steps + [["plugin", "add", PLUGIN_ID]]
    if state["installed"]:
        return [["plugin", "enable", str(state["name"] or PLUGIN)]]
    return [["plugin", "install", REPO_URL]]


def _install_codex_agents(primary: Path, report: Report) -> None:
    """Codex discovers personal TOML agents, but cannot register them from a plugin."""

    templates = files("cli_subagents").joinpath("templates", "codex-agents")
    for runtime in WORKER_RUNTIMES:
        name = f"cli-subagents-{runtime}.toml"
        target = primary / "agents" / name
        try:
            content = templates.joinpath(name).read_bytes()
            if target.is_symlink():
                report.failed.append(f"Codex: won't replace the relay agent symlink at {target}.")
                continue
            previous = target.read_bytes() if target.exists() else None
            if previous == content:
                report.unchanged.append(f"Codex: relay agent {target.stem} is already up to date.")
                continue
            if report.dry_run:
                report.done.append(f"Codex: would install relay agent {target.stem} at {target}")
            else:
                backup = None
                # csa's own earlier version needs no backup; anything else might hold the user's edits.
                ours = previous is not None and previous.startswith(content.split(b"\n", 1)[0])
                if previous is not None and not ours:
                    # The backup must not end in .toml: Codex discovers those recursively.
                    backup = target.with_name(f"{name}.csa-backup-{time.time_ns()}")
                    copy_exclusive(target, backup)
                write_private(target, content)
                note = f"; backup at {backup}" if backup else ""
                report.done.append(f"Codex: installed relay agent {target.stem} at {target}{note}")
            report.add_todo(RELOAD["codex"])
        except OSError as exc:
            report.failed.append(f"Codex: couldn't install relay agent {target.stem}: {exc}")


def _install_antigravity_agents(config: Config, report: Report) -> None:
    """Use Antigravity's manual global plugin discovery, separate from Claude's agents."""

    from cli_subagents.runtimes.antigravity import gemini_root

    root = gemini_root(config.runtime("antigravity"))
    plugin = root / "config" / "plugins" / ANTIGRAVITY_RELAYS
    templates = files("cli_subagents").joinpath("templates", "antigravity-plugin")
    # Publish the manifest last so a new plugin never registers an incomplete set.
    names = [f"agents/cli-subagents-{runtime}.md" for runtime in WORKER_RUNTIMES] + ["plugin.json"]
    try:
        for folder in (root / "config", plugin.parent, plugin, plugin / "agents"):
            if is_link(folder):
                report.failed.append(f"Antigravity: won't install relay agents through the folder link at {folder}.")
                return
        for name in names:
            target = plugin / name
            content = templates.joinpath(*name.split("/")).read_bytes()
            if is_link(target):
                report.failed.append(f"Antigravity: won't replace the relay agent link at {target}.")
                return
            previous = target.read_bytes() if target.exists() else None
            label = f"relay agent {target.stem}" if name.endswith(".md") else "relay plugin manifest"
            if previous == content:
                report.unchanged.append(f"Antigravity: {label} is already up to date.")
                continue
            if report.dry_run:
                report.done.append(f"Antigravity: would install {label} at {target}")
            else:
                backup = None
                # Markdown starts with ---, so include its identifying YAML comment.
                lines = 2 if name.endswith(".md") else 1
                ours = previous is not None and previous.splitlines()[:lines] == content.splitlines()[:lines]
                if previous is not None and not ours:
                    # Backups must not be Markdown files: the plugin scans those for agents.
                    backup = target.with_name(f"{target.name}.csa-backup-{time.time_ns()}")
                    copy_exclusive(target, backup)
                write_private(target, content)
                note = f"; backup at {backup}" if backup else ""
                report.done.append(f"Antigravity: installed {label} at {target}{note}")
            report.add_todo(RELOAD["antigravity"])
    except OSError as exc:
        report.failed.append(f"Antigravity: couldn't install relay agents at {plugin}: {exc}")


def install_plugin(config: Config, host: str, report: Report, claude_route: str = "auto") -> None:
    label = LABELS[host]
    runtime_config = config.runtime(host)
    command = get_runtime(host).find_binary(runtime_config)
    if host == "claude" and (claude_route == "settings" or (command is None and claude_route == "auto")):
        _declare_in_settings(runtime_config.primary, report, why=None if command else "the claude command wasn't found")
        return
    if command is None:
        report.failed.append(f"{label}: the {SHORT[host]} command wasn't found, so the plugin wasn't installed.")
        return
    env = _host_env(config, host)
    state = _plugin_state(host, command, env)
    steps = _plugin_steps(host, state)
    if not steps:
        report.unchanged.append(f"{label}: plugin {PLUGIN_ID} is already installed and enabled.")
        if host == "codex":
            _install_codex_agents(runtime_config.primary, report)
        elif host == "antigravity":
            _install_antigravity_agents(config, report)
        return
    for args in steps:
        shown = " ".join([SHORT[host], *args])
        if report.dry_run:
            report.done.append(f"{label}: would run `{shown}`")
            continue
        print(f"$ {shown}", flush=True)
        code, stdout, stderr = run_command([*command, *args], env)
        if code != 0:
            detail = _last_line(stdout, stderr)
            if host == "claude" and claude_route == "auto":
                print(f"  failed (exit {code}): {detail}. Falling back to settings.json.", flush=True)
                _declare_in_settings(runtime_config.primary, report, why=f"`{shown}` failed: {detail}")
                return
            report.failed.append(f"{label}: `{shown}` failed (exit {code}): {detail}")
            report.add_todo(f"{label}: install the plugin by hand, or register only the MCP server with `{FALLBACK[host]}`.")
            return
        report.done.append(f"{label}: ran `{shown}`")
    if report.dry_run:
        if host == "codex":
            _install_codex_agents(runtime_config.primary, report)
        elif host == "antigravity":
            _install_antigravity_agents(config, report)
        report.add_todo(RELOAD[host])
        return
    after = _plugin_state(host, command, env)
    if after["installed"] is False:
        report.failed.append(f"{label}: the commands succeeded but `{SHORT[host]} plugin list` doesn't show {PLUGIN}.")
        return
    if host == "codex":
        _install_codex_agents(runtime_config.primary, report)
    elif host == "antigravity":
        _install_antigravity_agents(config, report)
    report.add_todo(RELOAD[host])


def _read_claude_settings(primary: Path) -> dict[str, Any]:
    path = primary / "settings.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
    if not isinstance(data, dict):
        raise ValueError(f"{path} isn't a JSON object")
    return data


def _write_claude_settings(primary: Path, data: dict[str, Any]) -> Path | None:
    """Shared settings merge writer; preserve symlinks and every previous backup."""

    path = primary / "settings.json"
    target = path.resolve() if path.is_symlink() else path
    backup = None
    if target.exists():
        base = f"{target.name}.csa-backup-{time.strftime('%Y%m%d-%H%M%S')}"
        backup = target.with_name(base)
        suffix = 0
        while backup.exists():
            suffix += 1
            backup = target.with_name(f"{base}-{suffix}")
        shutil.copy2(target, backup)
    write_json(target, data)
    return backup


def _declare_in_settings(primary: Path, report: Report, why: str | None) -> None:
    """Declare the marketplace and enable the plugin in Claude Code's user settings.

    Claude Code installs a plugin that is enabled in settings from a known marketplace
    on its next start. Every other key is kept; the old file is backed up first.
    """

    path = primary / "settings.json"
    label = LABELS["claude"]
    try:
        data = _read_claude_settings(primary)
    except (OSError, ValueError) as exc:
        report.failed.append(f"{label}: can't read {path} as JSON ({exc}); it was left unchanged.")
        report.add_todo(f"{label}: install the plugin with `claude plugin install {PLUGIN} --marketplace {REPO}` or `/plugin install {PLUGIN_ID}`.")
        return
    markets = data.get("extraKnownMarketplaces")
    plugins = data.get("enabledPlugins")
    if (markets is not None and not isinstance(markets, dict)) or (plugins is not None and not isinstance(plugins, dict)):
        report.failed.append(f"{label}: unexpected extraKnownMarketplaces or enabledPlugins in {path}; it was left unchanged.")
        return
    has_market = isinstance(markets, dict) and MARKETPLACE in markets
    enabled = isinstance(plugins, dict) and plugins.get(PLUGIN_ID) is True
    reason = f" ({why})" if why else ""
    if has_market and enabled:
        report.unchanged.append(f"{label}: {path} already declares the marketplace and enables {PLUGIN_ID}.")
        if why:
            report.add_todo(f"Restart Claude Code if it is running, so it installs the {PLUGIN} plugin declared in its settings.")
        return
    if report.dry_run:
        report.done.append(f"{label}: would add the {MARKETPLACE} marketplace and enable {PLUGIN_ID} in {path}{reason}")
        report.add_todo("Restart Claude Code so it installs the cli-subagents plugin from its settings.")
        return
    merged = dict(data)
    merged["extraKnownMarketplaces"] = dict(markets or {})
    merged["extraKnownMarketplaces"].setdefault(MARKETPLACE, {"source": {"source": "github", "repo": REPO}})
    merged["enabledPlugins"] = dict(plugins or {})
    merged["enabledPlugins"][PLUGIN_ID] = True
    backup = _write_claude_settings(primary, merged)
    note = f"; backup at {backup}" if backup else ""
    report.done.append(f"{label}: added the {MARKETPLACE} marketplace and enabled {PLUGIN_ID} in {path}{reason}{note}")
    report.add_todo("Restart Claude Code so it installs the cli-subagents plugin from its settings (a restart, not /reload-plugins).")


def claude_sandbox_hooks() -> dict[str, list[dict[str, Any]]]:
    """Opt-in user hooks. The command/args pair identifies csa's own hooks."""

    definitions = {}
    for event, action in (("PreToolUse", "claude-sandbox"), ("SessionStart", "claude-sandbox-start"),
                          ("SessionEnd", "claude-sandbox-end")):
        hook: dict[str, Any] = {"type": "command", "command": "csa", "args": ["hook", action], "timeout": 60}
        if event != "SessionEnd":
            hook["onFailure"] = "block"
        group: dict[str, Any] = {"hooks": [hook]}
        if event == "PreToolUse":
            # Monitor runs a shell command too (in Bash's shell), so block-bash must see it.
            group["matcher"] = "Bash|PowerShell|Monitor"
        definitions[event] = [group]
    return definitions


def _is_claude_sandbox_hook(hook: Any) -> bool:
    return (isinstance(hook, dict) and hook.get("command") == "csa"
            and hook.get("args") in (["hook", "claude-sandbox"], ["hook", "claude-sandbox-start"],
                                     ["hook", "claude-sandbox-end"]))


def _split_claude_sandbox_hooks(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Remove only managed commands, retaining other commands even in a shared group."""

    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks isn't a JSON object")
    remaining = dict(hooks)
    owned: dict[str, list[dict[str, Any]]] = {}
    for event in claude_sandbox_hooks():
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"hooks.{event} isn't a list")
        kept = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError(f"unexpected hook group in hooks.{event}")
            managed = [hook for hook in group["hooks"] if _is_claude_sandbox_hook(hook)]
            if not managed:
                kept.append(group)
                continue
            owned.setdefault(event, []).append({**group, "hooks": managed})
            others = [hook for hook in group["hooks"] if not _is_claude_sandbox_hook(hook)]
            if others:
                kept.append({**group, "hooks": others})
        if event in owned:
            if kept:
                remaining[event] = kept
            else:
                remaining.pop(event, None)
    return remaining, owned


def sync_claude_sandbox_hooks(config: Config, report: Report) -> None:
    primary = config.runtime("claude").primary
    path = primary / "settings.json"
    enabled = config.sandbox.claude.mode != "off"
    try:
        data = _read_claude_settings(primary)
        remaining, owned = _split_claude_sandbox_hooks(data)
        expected = claude_sandbox_hooks() if enabled else {}
        if owned == expected:
            report.unchanged.append(f"Claude Code sandbox hooks: {'installed' if enabled else 'not installed'}; matches mode {config.sandbox.claude.mode}.")
            return
        merged = dict(data)
        for event, groups in expected.items():
            remaining[event] = [*remaining.get(event, []), *groups]
        if remaining:
            merged["hooks"] = remaining
        else:
            merged.pop("hooks", None)
        verb = "install" if enabled else "remove"
        if report.dry_run:
            report.done.append(f"Claude Code: would {verb} csa's sandbox hooks in {path} (mode {config.sandbox.claude.mode}).")
        else:
            backup = _write_claude_settings(primary, merged)
            note = f"; backup at {backup}" if backup else ""
            report.done.append(f"Claude Code: {'installed' if enabled else 'removed'} csa's sandbox hooks in {path}{note}")
        report.add_todo("Restart Claude Code so the sandbox hook settings take effect (a restart, not /reload-plugins).")
    except (OSError, ValueError) as exc:
        report.failed.append(f"Claude Code: couldn't sync sandbox hooks in {path} ({exc}); settings were left unchanged.")


def claude_sandbox_hook_status(config: Config) -> tuple[str, bool]:
    """Read-only doctor check, including modified, duplicate or stale managed hooks."""

    path = config.runtime("claude").primary / "settings.json"
    mode = config.sandbox.claude.mode
    try:
        _, owned = _split_claude_sandbox_hooks(_read_claude_settings(path.parent))
    except (OSError, ValueError) as exc:
        return f"Claude Code sandbox hooks: can't inspect {path} ({exc}).", False
    installed = owned == claude_sandbox_hooks()
    state = "installed" if installed else "partially installed or modified" if owned else "not installed"
    matches = installed if mode != "off" else not owned
    return (f"Claude Code sandbox hooks: {state} in {path}; "
            f"{'matches' if matches else 'does not match'} mode {mode}."), matches


# Accounts --------------------------------------------------------------------


def register_account(config: Config, runtime: str, name: str, home: Path) -> tuple[str, Account]:
    """Add an account with its own folder to the config (not saved). Returns ("added" | "exists", account).

    Raises ValueError when it can't be added.
    """

    if not NAME_RE.match(name) or name == "primary":
        raise ValueError("Give the account a short name: letters, digits, dot, dash or underscore ('primary' is reserved).")
    runtime_config = config.runtime(runtime)
    if same_path(home, runtime_config.primary.expanduser().resolve()):
        raise ValueError(f"{home} is the primary {runtime} setup. Extra accounts need their own folder.")
    if runtime == "antigravity":
        # An Antigravity account folder stands in for a home folder (it gets its own .gemini).
        primary_root = runtime_config.primary.expanduser().resolve()
        if any(same_path(home, folder) for folder in (Path.home(), primary_root.parent, primary_root.parent.parent)):
            raise ValueError(f"{home} is your own home or .gemini folder. An extra Antigravity account needs its own folder.")
    existing = runtime_config.find(name)
    if existing is not None:
        if same_path(existing.home, home):
            return "exists", existing
        raise ValueError(f"{runtime} account '{name}' already exists with folder {existing.home}.")
    for account in runtime_config.accounts:
        if same_path(account.home, home):
            return "exists", account
    account = Account(name=name, home=home)
    runtime_config.accounts.append(account)
    return "added", account


def guess_runtime(folder: Path) -> str:
    """claude, codex or antigravity, from marker files in the folder, then from its name."""

    if (folder / ".gemini" / "antigravity-cli").is_dir():
        return "antigravity"
    scores = {runtime: sum((folder / marker).exists() for marker in markers) for runtime, markers in MARKERS.items()}
    if scores["codex"] > scores["claude"]:
        return "codex"
    if scores["claude"] > scores["codex"]:
        return "claude"
    return "codex" if "codex" in folder.name.lower() else "claude"


def parse_account(value: str) -> tuple[str | None, str, Path]:
    """`[runtime:]name=folder` -> (runtime or None, name, folder)."""

    left, sep, folder = value.partition("=")
    if not sep or not left or not folder:
        raise ValueError(f"--account takes NAME=FOLDER (or codex:NAME=FOLDER), got '{value}'")
    runtime, colon, name = left.rpartition(":")
    if colon and runtime not in HOSTS:
        raise ValueError(f"unknown runtime '{runtime}' in --account '{value}' (use claude, codex or antigravity)")
    return (runtime or None), name, Path(os.path.expandvars(folder)).expanduser().resolve()


def _probe_spec(runtime: str) -> JobSpec:
    return JobSpec(job_id="probe", runtime=runtime, prompt="", cwd=str(Path.cwd().resolve()))


def setup_accounts(config: Config, values: list[str], report: Report) -> None:
    changed = False
    to_check: list[tuple[str, Account]] = []
    for value in values:
        try:
            runtime, name, folder = parse_account(value)
        except ValueError as exc:
            report.failed.append(str(exc))
            continue
        runtime = runtime or guess_runtime(folder)
        if not folder.is_dir():
            report.failed.append(
                f"Account '{name}': folder {folder} doesn't exist. To make a new account, run `csa accounts add {name}`"
                + ("" if runtime == "claude" else f" --runtime {runtime}") + " and log in."
            )
            continue
        try:
            status, account = register_account(config, runtime, name, folder)
        except ValueError as exc:
            report.failed.append(f"Account '{name}': {exc}")
            continue
        alias = f" (already registered as '{account.name}')" if account.name != name else ""
        if report.dry_run:
            verb = "would register and sync" if status == "added" else "already registered; would sync"
            report.done.append(f"{runtime} account '{name}' ({folder}): {verb}{alias}")
            continue
        changed = changed or status == "added"
        to_check.append((runtime, account))
        if status == "added":
            report.done.append(f"{runtime} account '{account.name}' ({folder}): registered")
        else:
            report.unchanged.append(f"{runtime} account '{account.name}' ({folder}): already registered{alias}")
    if changed:
        save_config(config)
    for runtime, account in to_check:
        runtime_config = config.runtime(runtime)
        adapter = get_runtime(runtime)
        try:
            setup = adapter.prepare(runtime_config, account, _probe_spec(runtime))
        except Exception as exc:  # a sync problem shouldn't hide the rest of the summary
            report.failed.append(f"{runtime} account '{account.name}': syncing the shared setup failed: {exc}")
            continue
        report.done.append(f"{runtime} account '{account.name}': shared setup synced")
        if getattr(adapter, "add_note", None):
            report.notes.append(f"{runtime} account '{account.name}': {adapter.add_note}")
        for message in (setup.get("conflicts") or []) + (setup.get("warnings") or []):
            report.notes.append(f"{runtime} account '{account.name}': {message}")
        if adapter.find_binary(runtime_config) is None:
            continue
        if not (adapter.auth_status(runtime_config, account) or {}).get("loggedIn"):
            flag = "" if runtime == "claude" else f" --runtime {runtime}"
            report.add_todo(f"Log in {runtime} account '{account.name}' in a browser: `csa accounts login {account.name}{flag}`.")


# Main ------------------------------------------------------------------------


def _check_primary_login(config: Config, host: str, report: Report) -> None:
    if host == "antigravity":
        return
    runtime_config = config.runtime(host)
    adapter = get_runtime(host)
    if adapter.find_binary(runtime_config) is None:
        return
    status = adapter.auth_status(runtime_config, runtime_config.primary_account()) or {}
    if not status.get("loggedIn"):
        how = "run `claude` and use /login" if host == "claude" else "run `codex login`"
        report.add_todo(f"{LABELS[host]} reports no login for {runtime_config.primary}. If you use it there, {how}.")


def _check_csa_on_path(report: Report) -> None:
    if find_program("csa"):
        return
    scripts = sysconfig.get_path("scripts")
    report.add_todo(
        "The plugin starts `csa`, but it isn't on PATH. Run `pipx ensurepath`, or add "
        f"{scripts} to PATH, then restart your terminal and agent."
    )


def ensure_sandbox(config: Config, report: Report, sandbox: bool = True) -> None:
    """Install or check csa's MXC sandbox (Windows); an unexpected error never hides the rest of the report."""

    if not sandbox:
        report.notes.append("Sandbox: skipped (--no-sandbox); csa's MXC sandbox wasn't installed or checked.")
        return
    from cli_subagents.sandbox import install

    try:
        install.ensure(config.sandbox, report, dry_run=report.dry_run)
    except Exception as exc:
        report.failed.append(f"Sandbox: the install stopped with an unexpected error: {type(exc).__name__}: {exc}")


def run_setup(config: Config, hosts: list[str], accounts: list[str], dry_run: bool = False,
              claude_route: str = "auto", sandbox: bool = True) -> tuple[int, Report]:
    report = Report(dry_run=dry_run)
    if not hosts or "auto" in hosts:
        detected = detect_hosts(config)
        hosts = [host for host in HOSTS if host in detected or host in hosts]
        if not hosts:
            report.failed.append(
                "No Claude Code, Codex or Antigravity install was found. Install one, or register the MCP server "
                "in your client by hand: command `csa`, argument `mcp`."
            )
    for host in hosts:
        install_plugin(config, host, report, claude_route)
        if host == "claude":
            sync_claude_sandbox_hooks(config, report)
        _check_primary_login(config, host, report)
    setup_accounts(config, accounts, report)
    ensure_sandbox(config, report, sandbox)
    _check_csa_on_path(report)
    if "antigravity" in hosts:
        report.add_todo("If you haven't signed in to Antigravity yet, run `agy` once and sign in.")
    return (1 if report.failed else 0), report


def format_report(report: Report, name: str = "setup") -> str:
    title = f"CLI Subagents {name}" + (" (dry run: nothing was changed)" if report.dry_run else "")
    lines = [title, ""]
    sections = (
        ("Planned" if report.dry_run else "Done", report.done),
        ("Already in place", report.unchanged),
        ("Failed", report.failed),
        ("Notes", report.notes),
    )
    for heading, items in sections:
        if items:
            lines.append(f"{heading}:")
            lines += [f"  - {item}" for item in items]
            lines.append("")
    if report.todo:
        lines.append("Still needed from you:")
        lines += [f"  {index}. {item}" for index, item in enumerate(report.todo, 1)]
    else:
        lines.append("Nothing left for you to do.")
    lines += ["", "Check everything with `csa doctor` (add --runtime codex for Codex accounts)."]
    if name == "setup":
        lines.append("Optional: delegation rules for your agent. See `csa rules`; install with `csa rules --install`.")
    return "\n".join(lines)


# Update ----------------------------------------------------------------------

PACKAGE_SOURCE = f"git+{REPO_URL}"
MANUAL_PACKAGE = (
    f"`pipx install --force {PACKAGE_SOURCE}`, `uv tool install --force {PACKAGE_SOURCE}` "
    f"or `python -m pip install --user --force-reinstall {PACKAGE_SOURCE}`, whichever installed it"
)


def detect_install(prefix: str, location: str | None, direct_url: str | None, user_site: str | None,
                   executable: str) -> tuple[str, list[str] | None]:
    """How this package was installed, and the command that reinstalls it from GitHub.

    `prefix` is sys.prefix, `location` the folder that holds the installed package
    (None when it isn't installed, e.g. run from a checkout via PYTHONPATH).
    """

    if location is None:
        return "unknown", None
    try:
        info = json.loads(direct_url) if direct_url else {}
    except ValueError:
        info = {}
    if isinstance(info, dict) and (info.get("dir_info") or {}).get("editable"):
        return "editable", None
    parts = [part.lower() for part in Path(prefix).parts]
    if "venvs" in parts and any("pipx" in part for part in parts):
        return "pipx", [find_program("pipx") or "pipx", "install", "--force", PACKAGE_SOURCE]
    if "uv" in parts and "tools" in parts:
        return "uv", [find_program("uv") or "uv", "tool", "install", "--force", PACKAGE_SOURCE]
    if user_site and Path(location).resolve().is_relative_to(Path(user_site).resolve()):
        return "pip-user", [executable, "-m", "pip", "install", "--user", "--force-reinstall", "--no-deps", PACKAGE_SOURCE]
    return "pip", [executable, "-m", "pip", "install", "--force-reinstall", "--no-deps", PACKAGE_SOURCE]


def _install_info() -> tuple[str, list[str] | None]:
    import importlib.metadata
    import site
    import sys

    running = Path(__file__).resolve().parent
    if (running.parent.parent / "pyproject.toml").is_file() and (running.parent.parent / ".claude-plugin").is_dir():
        return "checkout", None  # src/cli_subagents in a clone of the repository
    try:
        dist = importlib.metadata.distribution("cli-subagents")
        location = str(dist.locate_file(""))
        direct_url = dist.read_text("direct_url.json")
        if not same_path(Path(dist.locate_file("cli_subagents")).resolve(), running):
            return "unknown", None  # the installed copy isn't the one running
    except importlib.metadata.PackageNotFoundError:
        location = direct_url = None
    try:
        user_site = site.getusersitepackages()
    except Exception:
        user_site = None
    return detect_install(sys.prefix, location, direct_url, user_site, sys.executable)


def _replaces_running_program(method: str) -> bool:
    """Windows can't replace a running .exe, so csa can't reinstall itself from csa.exe or a tool venv."""

    import sys

    if os.name != "nt":
        return False
    return method in ("pipx", "uv") or Path(sys.argv[0]).stem.lower() == "csa"


def update_package(report: Report) -> None:
    method, argv = _install_info()
    if method in ("editable", "checkout"):
        report.unchanged.append("csa: runs from a checkout of the repository; update it with `git pull` there.")
        return
    if argv is None:
        report.failed.append(f"csa: couldn't tell how the package was installed. Reinstall it with {MANUAL_PACKAGE}.")
        return
    shown = " ".join([Path(argv[0]).stem, *argv[1:]])
    if report.dry_run:
        report.done.append(f"csa ({method} install): would run `{shown}`")
        return
    if _replaces_running_program(method):
        report.add_todo(f"Update the csa package now with `{shown}` (Windows can't replace csa while it's running).")
        return
    print(f"$ {shown}", flush=True)
    code, stdout, stderr = run_command(argv, None, timeout=600)
    if code != 0:
        report.failed.append(f"csa: `{shown}` failed (exit {code}): {_last_line(stdout, stderr)}")
        return
    report.done.append(f"csa: reinstalled from GitHub with `{shown}`")


def _update_steps(host: str, state: dict[str, Any]) -> list[list[str]]:
    if host == "claude":
        return [["plugin", "marketplace", "update", MARKETPLACE], ["plugin", "update", PLUGIN_ID]]
    if host == "codex":
        return [["plugin", "marketplace", "upgrade", MARKETPLACE], ["plugin", "add", PLUGIN_ID]]
    # agy has no update command; installing from the URL again replaces the plugin.
    return [["plugin", "install", REPO_URL]]


def _settings_declare_plugin(primary: Path) -> bool:
    try:
        data = json.loads((primary / "settings.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and isinstance(data.get("enabledPlugins"), dict) and PLUGIN_ID in data["enabledPlugins"]


def update_plugin(config: Config, host: str, report: Report) -> bool:
    """Update the plugin in one host. Returns True when the host has it."""

    label = LABELS[host]
    runtime_config = config.runtime(host)
    command = get_runtime(host).find_binary(runtime_config)
    if command is None:
        if host == "claude" and _settings_declare_plugin(runtime_config.primary):
            report.unchanged.append(f"{label}: the plugin is declared in {runtime_config.primary / 'settings.json'}; a restart refreshes it.")
            report.add_todo("Restart Claude Code so it refreshes the cli-subagents plugin.")
            return True
        return False
    env = _host_env(config, host)
    state = _plugin_state(host, command, env)
    if not state["installed"]:
        if host == "claude" and _settings_declare_plugin(runtime_config.primary):
            report.unchanged.append(f"{label}: the plugin is declared in settings.json but not installed yet; a restart installs it.")
            report.add_todo("Restart Claude Code so it installs the cli-subagents plugin.")
            return True
        return False
    for args in _update_steps(host, state):
        shown = " ".join([SHORT[host], *args])
        if report.dry_run:
            report.done.append(f"{label}: would run `{shown}`")
            continue
        print(f"$ {shown}", flush=True)
        code, stdout, stderr = run_command([*command, *args], env)
        if code != 0:
            report.failed.append(f"{label}: `{shown}` failed (exit {code}): {_last_line(stdout, stderr)}")
            return True
        report.done.append(f"{label}: ran `{shown}`")
    if host == "codex":
        _install_codex_agents(runtime_config.primary, report)
    elif host == "antigravity":
        _install_antigravity_agents(config, report)
    report.add_todo(RELOAD[host])
    return True


def run_update(config: Config, hosts: list[str], dry_run: bool = False, package: bool = True,
               sandbox: bool = True) -> tuple[int, Report]:
    report = Report(dry_run=dry_run)
    if package:
        update_package(report)
    candidates = HOSTS if not hosts or "auto" in hosts else [host for host in HOSTS if host in hosts]
    found = [host for host in candidates if update_plugin(config, host, report)]
    if "claude" in candidates:
        sync_claude_sandbox_hooks(config, report)
    if not found:
        report.unchanged.append("No host has the cli-subagents plugin installed. Run `csa setup` to install it.")
    ensure_sandbox(config, report, sandbox)
    return (1 if report.failed else 0), report
