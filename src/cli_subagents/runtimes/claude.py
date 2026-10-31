"""Claude Code adapter: `claude -p` workers with a shared setup across accounts.

Each account keeps its own config folder (`CLAUDE_CONFIG_DIR`) with its own
login, state file and session history. Everything else comes from the primary
setup at launch time:

* user skills, agents, commands, output styles and rules are linked (folders)
  or copied (files) into the account folder, and so is the user CLAUDE.md;
* user settings are passed with `--settings`, minus anything that picks an
  auth method or installs plugins;
* user and local-scope MCP servers are passed with `--mcp-config`;
* installed, enabled plugins are passed with `--plugin-dir`.

The adapter never reads or copies credential files.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
import uuid

from cli_subagents import sync
from cli_subagents.hosts import parent_note
from cli_subagents.config import Account, RuntimeConfig, cache_dir, home, jobs_dir, state_dir
from cli_subagents.fsutil import (
    file_lock,
    find_program,
    is_link,
    link_target,
    locate_program,
    make_dir_link,
    no_window_kwargs,
    read_json,
    replace_with_copy,
    same_path,
    write_json,
)
from cli_subagents.runtimes.base import (
    PARENT_SERVER,
    WORKER_TOOLS_NOTE,
    parent_server,
    AUTH,
    BUDGET,
    ERROR,
    MAX_TURNS,
    SUCCESS,
    USAGE_LIMIT,
    AttemptResult,
    JobSpec,
    Launch,
    clean_environment,
)

SYNC_PLAN = sync.SyncPlan(
    whole=("agents", "commands", "output-styles", "rules", "workflows"),
    # Claude Code fills skills/synced per account, so skills are shared one by one.
    folders=("skills",),
    files=("CLAUDE.md",),
    skip=frozenset({("skills", "synced")}),
)

# Settings that choose an auth method, a gateway or credential helpers, or install
# plugins are account-specific.
DROP_SETTINGS = {
    "apiKeyHelper",
    "awsAuthRefresh",
    "awsCredentialExport",
    "forceLoginMethod",
    "forceLoginOrgUUID",
    "forceLoginGatewayUrl",
    "proxyAuthHelper",
    "gcpAuthRefresh",
    "otelHeadersHelper",
    "enabledPlugins",
    "extraKnownMarketplaces",
}
# `env` entries in shared settings that could pick an Anthropic login, gateway or
# provider for the worker. Model choices under ANTHROPIC_* and every other key
# (tool API keys and the like) are shared.
SHARED_ANTHROPIC_ENV = {
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
}
AUTH_ENV_PREFIXES = ("CLAUDE_CODE_USE_", "CLAUDE_CODE_SKIP_", "CLAUDE_CODE_OAUTH")
AUTH_ENV_EXACT = {"AWS_BEARER_TOKEN_BEDROCK", "CLAUDE_CONFIG_DIR"}


def is_auth_env(key: str) -> bool:
    upper = key.upper()
    if upper.startswith("ANTHROPIC_"):
        return upper not in SHARED_ANTHROPIC_ENV
    return upper in AUTH_ENV_EXACT or upper.startswith(AUTH_ENV_PREFIXES)

# Read-only jobs use dontAsk with an allowlist instead of plan mode: plan mode
# blocks every MCP call (verified with 2.1.295), including the parent channel.
PERMISSION_MODES = {"read": "dontAsk", "full": "bypassPermissions"}
READ_ONLY_TOOLS = (
    "Read", "Grep", "Glob", "LS", "WebSearch", "WebFetch", "TodoWrite", "Task", "Agent",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git blame:*)",
    "Bash(ls:*)", "Bash(wc:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(grep:*)", "Bash(rg:*)",
    "PowerShell(Get-ChildItem:*)", "PowerShell(Get-Content:*)", "PowerShell(Measure-Object:*)",
)
# A read worker also gets a PreToolUse guard: these prefix rules alone cannot
# exclude options that write files or execute programs (git diff --output, rg --pre).

# Tools of this server inside a worker, denied so workers can't fan out further.
SELF_TOOLS = "mcp__cli-subagents,mcp__cli_subagents,mcp__plugin_cli-subagents_cli-subagents"

WORKER_NOTE = (
    "You are running as a delegated subagent for another agent session, which "
    "will read your final message. Work only on the task you were given, stay "
    "inside the stated scope, and end with a short summary of what you did, the "
    "result, and anything left open."
)

LIMIT_RE = re.compile(
    r"hit your [\w -]*limit|usage limit|limit reached|out of extra usage|rate_limit_error",
    re.IGNORECASE,
)
NOT_LIMIT_RE = re.compile(r"not your usage limit", re.IGNORECASE)
CAPACITY_RE = re.compile(r"\boverloaded_error\b|\b(?:API error:?\s*|HTTP\s+)529\b|\boverloaded\b", re.IGNORECASE)
# Weaker hint, trusted only together with a `rejected` rate-limit event.
LIMIT_HINT_RE = re.compile(r"limit|\b429\b", re.IGNORECASE)
AUTH_RE = re.compile(
    r"invalid api key|please run /login|not logged in|log in to|login expired|"
    r"oauth token has expired|authentication_error|invalid bearer token",
    re.IGNORECASE,
)


def _version_key(path: str) -> tuple[int, ...]:
    match = re.search(r"claude-code[\\/](\d+(?:\.\d+)*)", path)
    return tuple(int(part) for part in match.group(1).split(".")) if match else (0,)


def _default_dir() -> Path:
    return Path.home() / ".claude"


def _state_files(primary: Path) -> list[Path]:
    """Files that may hold the primary setup's state (`.claude.json`)."""

    files = [primary / ".claude.json"]
    if same_path(primary, _default_dir()):
        # Without CLAUDE_CONFIG_DIR, Claude Code keeps it next to the folder.
        files.insert(0, Path.home() / ".claude.json")
    return [path for path in files if path.is_file()]


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path.replace("/", os.sep)))


class ClaudeParser:
    """Turns `claude -p --output-format stream-json` events into progress and a result."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.init: dict[str, Any] = {}
        self.last_text = ""
        self.tool_uses = 0
        self.last_tool: str | None = None
        self.retries = 0
        self.last_retry_error: str | None = None
        self.rate_limit: dict[str, Any] | None = None
        self.result: dict[str, Any] | None = None

    def feed(self, event: dict[str, Any]) -> dict[str, Any] | None:
        kind = event.get("type")
        if event.get("session_id"):
            self.session_id = event["session_id"]
        if kind == "system" and event.get("subtype") == "init":
            self.init = {
                "model": event.get("model"),
                "permission_mode": event.get("permissionMode"),
                "version": event.get("claude_code_version"),
                "api_key_source": event.get("apiKeySource"),
                "mcp_servers": [
                    {"name": server.get("name"), "status": server.get("status"), "source": server.get("source")}
                    for server in event.get("mcp_servers") or []
                    if isinstance(server, dict)
                ],
                "plugins": [plugin.get("name") for plugin in event.get("plugins") or [] if isinstance(plugin, dict)],
                "skills": list(event.get("skills") or []),
                "agents": list(event.get("agents") or []),
            }
        elif kind == "system" and event.get("subtype") == "api_retry":
            self.retries += 1
            self.last_retry_error = str(event.get("error") or event.get("error_status") or "")
        elif kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.last_text = block["text"]
                elif block.get("type") == "tool_use":
                    self.tool_uses += 1
                    self.last_tool = block.get("name")
        elif kind == "rate_limit_event":
            info = event.get("rate_limit_info")
            if isinstance(info, dict):
                self.rate_limit = info
                return info
        elif kind == "result":
            self.result = event
            if event.get("subtype") == "success" and not event.get("is_error") and event.get("result"):
                self.last_text = str(event["result"])
        return None

    def progress(self) -> dict[str, Any]:
        return {
            "tool_uses": self.tool_uses,
            "last_tool": self.last_tool,
            "last_text": self.last_text[-600:],
            "retries": self.retries,
        }

    def finish(self, exit_code: int | None, stderr_tail: str) -> AttemptResult:
        result = self.result or {}
        common = {
            "session_id": self.session_id,
            "cost_usd": result.get("total_cost_usd"),
            "usage": result.get("usage"),
            "num_turns": result.get("num_turns"),
        }
        # Claude can emit a synthetic crash result with zeroed totals. It does
        # not establish that the failed run was free; debit its reservation.
        if result.get("subtype") == "error_during_execution" and not common["cost_usd"]:
            common["cost_usd"] = None
        succeeded = bool(self.result and result.get("subtype") == "success" and not result.get("is_error"))
        # A successful result is still useful if the process failed afterward.
        # Error results contain diagnostics; keep the last answer for those.
        text = (str(result.get("result") or "") or self.last_text) if succeeded else self.last_text
        if succeeded and exit_code == 0:
            return AttemptResult(SUCCESS, text=text, **common)
        if result.get("subtype") == "error_max_turns":
            return AttemptResult(MAX_TURNS, message="stopped at the max_turns limit", text=text, **common)
        if result.get("subtype") == "error_max_budget_usd":
            return AttemptResult(BUDGET, message="stopped at the account's dollar budget (--max-budget-usd)",
                                 text=text, **common)

        # Only error text counts here: the model's own words may mention limits.
        is_error = bool(self.result and result.get("is_error"))
        error_text = str(result.get("result") or "") if is_error or not self.result else ""
        haystack = "\n".join(part for part in (error_text, stderr_tail, self.last_retry_error or "") if part)
        # api_retry reports the CLI's own backoff. Only terminal diagnostics count here.
        terminal = "\n".join(part for part in (error_text, "\n".join(result.get("errors") or []), stderr_tail) if part)
        if result.get("api_error_status") == 529 or CAPACITY_RE.search(terminal):
            return AttemptResult(ERROR, message=_first_line(terminal) or "model at capacity (HTTP 529)",
                                 text=text, error_text=terminal[-4000:], at_capacity=True, **common)
        not_limit = bool(NOT_LIMIT_RE.search(haystack))
        rejected = bool(self.rate_limit and self.rate_limit.get("status") == "rejected")
        status_429 = result.get("api_error_status") == 429
        # A 429 or a `rejected` rate-limit event alone isn't enough (server throttling,
        # an unrelated failure after a rejected window): it needs an error result that
        # points at a limit as well.
        names_limit = is_error and (status_429 or bool(LIMIT_HINT_RE.search(error_text)))
        limited = not not_limit and (bool(LIMIT_RE.search(haystack)) or (rejected and names_limit))
        if limited:
            resets_at = _timestamp(self.rate_limit.get("resetsAt")) if rejected and self.rate_limit else None
            return AttemptResult(
                USAGE_LIMIT, message=_first_line(haystack) or "usage limit reached", text=text, resets_at=resets_at,
                error_text=haystack[-4000:], **common,
            )
        if result.get("api_error_status") == 401 or AUTH_RE.search(haystack):
            return AttemptResult(AUTH, message=_first_line(haystack) or "authentication failed", text=text, **common)
        message = f"claude exited with status {exit_code}"
        detail = _first_line(haystack)
        if detail:
            message += f": {detail}"
        elif not self.result:
            message += " and no result"
        return AttemptResult(ERROR, message=message, text=text, **common)


def _timestamp(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _first_line(text: str) -> str:
    for line in text.strip().splitlines():
        if line.strip():
            return line.strip()[:300]
    return ""


class ClaudeRuntime:
    name = "claude"

    # Binary --------------------------------------------------------------

    def find_binary(self, config: RuntimeConfig) -> list[str] | None:
        override = os.environ.get("CLI_SUBAGENTS_CLAUDE_COMMAND")
        if override:
            return list(json.loads(override))
        if config.binary:
            found = locate_program(config.binary)
            return [found] if found else None
        found = find_program("claude")
        if found:
            return [found]
        candidates: list[str] = []
        host = os.environ.get("CLAUDE_CODE_EXECPATH")
        if host and Path(host).is_file() and Path(host).name.lower().startswith("claude"):
            candidates.append(host)
        home = Path.home()
        for path in (home / ".local/bin/claude.exe", home / ".local/bin/claude", home / ".claude/local/claude"):
            if path.is_file():
                candidates.append(str(path))
        bundles: list[str] = []
        local = os.environ.get("LOCALAPPDATA")
        roaming = os.environ.get("APPDATA")
        if local:
            bundles += glob.glob(os.path.join(local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude", "claude-code", "*", "*", "claude.exe"))
        if roaming:
            bundles += glob.glob(os.path.join(roaming, "Claude", "claude-code", "*", "*", "claude.exe"))
        candidates += sorted((path for path in bundles if Path(path).is_file()), key=_version_key, reverse=True)
        return [candidates[0]] if candidates else None

    def _run_quiet(self, config: RuntimeConfig, args: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
        command = self.find_binary(config)
        if not command:
            return 127, "claude binary not found"
        try:
            completed = subprocess.run(
                [*command, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                env=env,
                stdin=subprocess.DEVNULL,
                **no_window_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, f"{type(exc).__name__}: {exc}"
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")

    def version(self, config: RuntimeConfig) -> str | None:
        code, output = self._run_quiet(config, ["--version"], env=clean_environment())
        return output.strip().splitlines()[0] if code == 0 and output.strip() else None

    def account_env(self, config: RuntimeConfig, account: Account) -> dict[str, str]:
        env = clean_environment()
        if not (account.is_primary and same_path(account.home, _default_dir())):
            env["CLAUDE_CONFIG_DIR"] = str(account.home)
        return env

    def auth_status(self, config: RuntimeConfig, account: Account) -> dict[str, Any]:
        """`claude auth status --json` for one account. Makes no model request."""

        code, output = self._run_quiet(config, ["auth", "status", "--json"], env=self.account_env(config, account))
        try:
            data = json.loads(output[output.index("{"):])
        except ValueError:
            return {"loggedIn": False, "error": output.strip()[:300] or f"exit {code}"}
        return {key: data.get(key) for key in ("loggedIn", "authMethod", "subscriptionType", "configDirectory")}

    def login_command(self, config: RuntimeConfig, account: Account) -> tuple[list[str], dict[str, str]]:
        command = self.find_binary(config)
        if not command:
            raise RuntimeError("claude binary not found; install Claude Code or set [claude] binary")
        return [*command, "auth", "login"], self.account_env(config, account)

    def transfer_session(self, source: Account, target: Account, session_id: str, cwd: str) -> bool:
        """Copy a session's transcript so `target` can `--resume` it. Copies only; never moves or deletes.

        Verified with Claude Code 2.1.295: a transcript copied into another account
        folder resumes there with its full history.
        """

        if not re.fullmatch(r"[0-9A-Za-z-]{8,64}", session_id):
            return False
        source_projects = source.home / "projects"
        target_projects = target.home / "projects"
        if same_path(source_projects, target_projects):
            return True
        if is_link(target_projects) or not source_projects.is_dir():
            return False
        candidates = [source_projects / _project_dir_name(cwd) / f"{session_id}.jsonl"]
        candidates += sorted(source_projects.glob(f"*/{session_id}.jsonl"))
        transcript = next((path for path in candidates if path.is_file() and not is_link(path)), None)
        if transcript is None:
            return False
        destination = target_projects / transcript.parent.name
        if is_link(destination) or is_link(destination / transcript.name):
            return False
        try:
            destination.mkdir(parents=True, exist_ok=True)
            # Fresh temporary file plus rename: never writes through a link or a hard link.
            replace_with_copy(transcript, destination / transcript.name)
            sidecar = transcript.parent / session_id  # subagent transcripts and tool results
            if sidecar.is_dir() and not is_link(sidecar) and not os.path.lexists(destination / session_id):
                shutil.copytree(sidecar, destination / session_id, symlinks=True)
        except OSError:
            return False
        return True

    # Shared setup ---------------------------------------------------------

    def prepare(self, config: RuntimeConfig, account: Account, spec: JobSpec) -> dict[str, Any]:
        """Bring the account folder in line with the primary setup and build flag files."""

        report = sync.new_report()
        shared = not (account.is_primary or same_path(account.home, config.primary))
        if shared:
            with file_lock(state_dir() / "sync" / f"claude-{account.name}.lock", timeout=30):
                sync.sync(config.primary, account.home, SYNC_PLAN, report)
            self._share_memory(config.primary, account.home, spec.cwd, report)
        out = cache_dir() / "claude" / account.name
        settings = self._overlay_settings(config, report) if shared else {}
        if config.connectors == "off":
            settings["disableClaudeAiConnectors"] = True
        report["settings_path"] = None
        if settings:
            path = out / "settings.json"
            write_json(path, settings)
            report["settings_path"] = str(path)
        report["mcp_config_path"] = None
        report["mcp_servers"] = []
        report["plugin_dirs"] = []
        if shared:
            servers = self._mcp_servers(config.primary, spec.cwd)
            if servers:
                path = out / f"mcp-{_project_key(spec.cwd)}.json"
                write_json(path, {"mcpServers": servers})
                report["mcp_config_path"] = str(path)
                report["mcp_servers"] = sorted(servers)
            report["plugin_dirs"] = self._plugin_dirs(config.primary, report)
        return report

    def _share_memory(self, primary: Path, home: Path, cwd: str, report: dict[str, Any]) -> None:
        """Point the account's auto memory for this project at the primary's memory folder."""

        name = _project_dir_name(cwd)
        source = primary / "projects" / name / "memory"
        destination = home / "projects" / name / "memory"
        try:
            if is_link(home / "projects") or is_link(home / "projects" / name):
                report["conflicts"].append("projects folder in the account is a link; project memory not shared")
                return
            if is_link(destination):
                if same_path(link_target(destination) or "", source):
                    return
                report["conflicts"].append(f"project memory for {name}: account has its own link, left in place")
                return
            if destination.exists():
                if any(destination.iterdir()):
                    report["conflicts"].append(f"project memory for {name}: account has its own memory, left in place")
                    return
                destination.rmdir()
            source.mkdir(parents=True, exist_ok=True)
            kind = make_dir_link(source, destination)
            report["linked" if kind != "copy" else "copied"].append(f"projects/{name}/memory")
        except OSError as exc:
            report["warnings"].append(f"could not share project memory: {exc}")

    def _overlay_settings(self, config: RuntimeConfig, report: dict[str, Any]) -> dict[str, Any]:
        settings = read_json(config.primary / "settings.json", {}) or {}
        if not isinstance(settings, dict):
            report["warnings"].append("primary settings.json is not a JSON object; not shared")
            return {}
        overlay = {key: value for key, value in settings.items() if key not in DROP_SETTINGS}
        env = overlay.get("env")
        if isinstance(env, dict):
            dropped = sorted(key for key in env if is_auth_env(key))
            if dropped:
                report["warnings"].append(f"login-related env settings not shared: {', '.join(dropped)}")
            overlay["env"] = {key: value for key, value in env.items() if not is_auth_env(key)}
        return overlay

    def _mcp_servers(self, primary: Path, cwd: str) -> dict[str, Any]:
        servers: dict[str, Any] = {}
        wanted = _norm(cwd)
        for path in _state_files(primary):
            state = read_json(path, {}) or {}
            if not isinstance(state, dict):
                continue
            user = state.get("mcpServers")
            if isinstance(user, dict):
                servers.update(user)
            projects = state.get("projects")
            if isinstance(projects, dict):
                for key, project in projects.items():
                    if isinstance(project, dict) and _norm(str(key)) == wanted:
                        local = project.get("mcpServers")
                        if isinstance(local, dict):
                            servers.update(local)
        return servers

    def _plugin_dirs(self, primary: Path, report: dict[str, Any]) -> list[str]:
        settings = read_json(primary / "settings.json", {}) or {}
        enabled = settings.get("enabledPlugins") if isinstance(settings, dict) else None
        installed = (read_json(primary / "plugins" / "installed_plugins.json", {}) or {}).get("plugins") or {}
        if not isinstance(enabled, dict) or not isinstance(installed, dict):
            return []
        directories: list[str] = []
        for plugin_id, on in enabled.items():
            if on is not True:
                continue
            entries = installed.get(plugin_id) or []
            user_entries = [entry for entry in entries if isinstance(entry, dict) and entry.get("scope", "user") == "user"]
            path = next((entry.get("installPath") for entry in user_entries if entry.get("installPath")), None)
            if path and Path(path).is_dir():
                directories.append(path)
            else:
                report["warnings"].append(f"plugin {plugin_id} is enabled but its folder was not found")
        return directories

    # Launch ---------------------------------------------------------------

    def launch(
        self, config: RuntimeConfig, account: Account, spec: JobSpec, prompt: str, setup: dict[str, Any]
    ) -> Launch:
        command = self.find_binary(config)
        if not command:
            raise RuntimeError("claude binary not found; install Claude Code or set [claude] binary in config.toml")
        mode = PERMISSION_MODES.get(spec.access, config.edit_permission_mode)
        argv = [*command, "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", mode]
        model = spec.model or config.default_model
        if model:
            argv += ["--model", model]
        if spec.effort:
            argv += ["--effort", spec.effort]
        if spec.max_turns:
            argv += ["--max-turns", str(spec.max_turns)]
        if spec.max_budget_usd is not None:
            argv += ["--max-budget-usd", format(spec.max_budget_usd, ".6f")]
        warnings: list[str] = []
        argv += ["--settings", str(self._job_settings(spec.job_id, setup, warnings, access=spec.access))]
        servers = dict((read_json(Path(setup["mcp_config_path"]), {}) or {}).get("mcpServers") or {}) if setup.get("mcp_config_path") else {}
        servers[PARENT_SERVER] = parent_server(spec.job_id)
        job_mcp = jobs_dir() / spec.job_id / "mcp.json"
        write_json(job_mcp, {"mcpServers": servers})
        argv += ["--mcp-config", str(job_mcp)]
        for directory in setup.get("plugin_dirs") or []:
            argv += ["--plugin-dir", directory]
        for directory in spec.add_dirs:
            argv += ["--add-dir", directory]
        argv += [
            "--disallowedTools", SELF_TOOLS,
            "--allowedTools", ",".join([f"mcp__{PARENT_SERVER}", *(READ_ONLY_TOOLS if spec.access == "read" else ())]),
            "--append-system-prompt", WORKER_NOTE + WORKER_TOOLS_NOTE,
        ]
        if spec.resume_session:
            session_id = spec.resume_session
            argv += ["--resume", session_id]
        else:
            session_id = str(uuid.uuid4())
            argv += ["--session-id", session_id]

        env = self.account_env(config, account)
        env["CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY"] = "1"
        env["DISABLE_AUTOUPDATER"] = "1"
        env["CLI_SUBAGENTS_DEPTH"] = str(spec.depth)
        env["CLI_SUBAGENTS_JOB_ID"] = spec.job_id
        stdin = ("" if spec.resume_session else parent_note(spec.parent, "claude")) + prompt
        return Launch(argv=argv, env=env, stdin=stdin, session_id=session_id, warnings=warnings)

    def _job_settings(self, job_id: str, setup: dict[str, Any], warnings: list[str], *, access: str = "edit") -> Path:
        """The account's overlay plus inbox delivery and, for read jobs, shell validation.

        The hook is added next to the user's own hooks, never in place of them.
        """

        from cli_subagents.hooks import worker_inbox_hook, worker_read_hook  # hooks imports jobs, which imports this module

        settings = read_json(Path(setup["settings_path"]), {}) if setup.get("settings_path") else {}
        settings = dict(settings) if isinstance(settings, dict) else {}
        hooks = settings.get("hooks", {})
        if isinstance(hooks, dict) and isinstance(hooks.get("PostToolUse", []), list):
            hooks = dict(hooks)
            hooks["PostToolUse"] = [*hooks.get("PostToolUse", []), worker_inbox_hook(job_id, home())]
            settings["hooks"] = hooks
        else:
            warnings.append("settings.json hooks are not in the expected shape; the worker inbox hook was not added")
        if access == "read":
            hooks = settings.get("hooks", {})
            if not isinstance(hooks, dict) or not isinstance(hooks.get("PreToolUse", []), list):
                raise ValueError("cannot install the read-only shell guard: settings.json hooks are not in the expected shape")
            if settings.get("disableAllHooks"):
                raise ValueError("cannot install the read-only shell guard: disableAllHooks is enabled")
            settings["hooks"] = {**hooks, "PreToolUse": [*hooks.get("PreToolUse", []), worker_read_hook()]}
            # A primary account/project can disable hooks too. The job overlay
            # must keep this enforcement hook enabled.
            settings["disableAllHooks"] = False
        path = jobs_dir() / job_id / "settings.json"
        write_json(path, settings)
        return path

    def interactive(self, config: RuntimeConfig, account: Account, setup: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
        """Command and environment for a normal interactive session on `account`."""

        command = self.find_binary(config)
        if not command:
            raise RuntimeError("claude binary not found; install Claude Code or set [claude] binary in config.toml")
        argv = [*command]
        if setup.get("settings_path"):
            argv += ["--settings", setup["settings_path"]]
        if setup.get("mcp_config_path"):
            argv += ["--mcp-config", setup["mcp_config_path"]]
        for directory in setup.get("plugin_dirs") or []:
            argv += ["--plugin-dir", directory]
        return argv, self.account_env(config, account)

    def new_parser(self) -> ClaudeParser:
        return ClaudeParser()


def _project_dir_name(cwd: str) -> str:
    """Claude Code's folder name for a project: every non-alphanumeric character becomes '-'."""

    return re.sub(r"[^A-Za-z0-9]", "-", os.path.normpath(cwd))


def _project_key(cwd: str) -> str:
    return hashlib.sha256(_norm(cwd).encode("utf-8")).hexdigest()[:12]
