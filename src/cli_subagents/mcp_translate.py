"""Share the MCP servers of a Claude Code session with workers of other CLIs.

Research (2026-10-16; read for the mapping only, no code copied)
-----------------------------------------------------------------
* Codex config reference, https://developers.openai.com/codex/config-reference
  (now https://learn.chatgpt.com/docs/config-file/config-reference): stdio servers take
  `command`, `args`, `env`, `env_vars`, `cwd`; streamable-HTTP servers take `url`,
  `bearer_token_env_var`, `http_headers`, `env_http_headers`; both take `enabled`,
  `startup_timeout_sec`, `tool_timeout_sec`. `env_vars` names variables Codex forwards
  from its own environment to the server. There is no SSE transport.
* Checked against codex-cli 0.162.1 with `codex mcp get <name> --json` and `-c` overrides:
  `env_vars`, `cwd`, `bearer_token_env_var`, `http_headers` and `env_http_headers` all load.
  `codex mcp add --help` offers only `--env`, `--url`, `--bearer-token-env-var`.
* rulesync (dyoshikawa/rulesync, MIT), src/features/mcp/codexcli-mcp.ts: maps `headers` to
  `http_headers` for URL servers only; moves `Authorization: Bearer ${VAR}` to
  `bearer_token_env_var` and `Header: ${VAR}` to `env_http_headers`; drops `env` on URL
  servers; renames `httpUrl` to `url`; converts ms timeouts to seconds. It converts files
  at rest (`${VAR}` stays a reference). We convert resolved values for one job, so values
  travel through the Codex process environment instead of the command line.
* caam and similar account switchers only swap login files; none translates MCP config.
* Claude Code MCP scopes, https://code.claude.com/docs/en/mcp: user and local servers live in
  `~/.claude.json`, project servers in `.mcp.json` (need approval), plugin servers in the
  plugin's `.mcp.json`; `${VAR}` and `${VAR:-default}` are expanded in command, args, env,
  url and headers.

Design
------
`collect_servers` gathers what the Claude session would use for a folder, expands `${VAR}`,
and drops our own server and servers it can't resolve. `to_codex` turns them into `-c`
overrides plus extra environment for the Codex process, and `to_agy` into Antigravity's
`mcpServers` JSON shape. Both return warnings for what they leave out. Values of `env` and
headers are never written into the overrides: Codex forwards them by variable name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import tomllib
from typing import Any

from cli_subagents.fsutil import read_json, same_path

# Server names that are this tool's own; Codex and Claude runs already handle them.
SELF_NAMES = {"cli-subagents", "cli_subagents", "csa-parent", "csa_parent"}
SELF_COMMANDS = {"csa", "csa.exe", "cli-subagents-mcp", "cli-subagents-mcp.exe"}
BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
SECRET_FLAG_RE = re.compile(r"(?i)(token|secret|passw(or)?d|api[-_]?key|bearer|authorization|credential)")
URL_CREDENTIALS_RE = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
# Variables a server's `env` must not set on the Codex process.
PROTECTED_ENV_PREFIXES = ("CODEX_", "CLI_SUBAGENTS_", "OPENAI_")


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path.replace("/", os.sep)))


# Collecting ---------------------------------------------------------------


@dataclass
class Collected:
    servers: dict[str, dict[str, Any]] = field(default_factory=dict)  # name -> Claude-shaped entry, `${VAR}` expanded
    warnings: list[str] = field(default_factory=list)


def _expand(value: Any, environ: dict[str, str], extra: dict[str, str], missing: set[str]) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            found = extra.get(name, environ.get(name))
            if found is not None:
                return found
            if match.group(2) is not None:
                return match.group(2)
            missing.add(name)
            return match.group(0)

        return ENV_REF_RE.sub(replace, value)
    if isinstance(value, list):
        return [_expand(item, environ, extra, missing) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, environ, extra, missing) for key, item in value.items()}
    return value


def _is_self(name: str, server: dict[str, Any]) -> bool:
    command = Path(str(server.get("command") or "")).name.lower()
    args = server.get("args")
    args = [str(item) for item in args] if isinstance(args, list) else []
    return (
        name in SELF_NAMES
        or name.endswith(":cli-subagents")
        or command in SELF_COMMANDS
        or any("cli_subagents" in item for item in args)
    )


def _state_servers(primary: Path, cwd: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """(user servers, local servers for cwd, the cwd's project state) from the primary's `.claude.json`."""

    from cli_subagents.runtimes.claude import _state_files  # the files claude.py reads for its own workers

    user: dict[str, Any] = {}
    local: dict[str, Any] = {}
    project_state: dict[str, Any] = {}
    wanted = _norm(cwd)
    for path in _state_files(primary):
        state = read_json(path, {}) or {}
        if not isinstance(state, dict):
            continue
        if isinstance(state.get("mcpServers"), dict):
            user.update(state["mcpServers"])
        projects = state.get("projects")
        if isinstance(projects, dict):
            for key, project in projects.items():
                if isinstance(project, dict) and _norm(str(key)) == wanted:
                    project_state.update(project)
                    if isinstance(project.get("mcpServers"), dict):
                        local.update(project["mcpServers"])
    return user, local, project_state


def _project_mcp_files(cwd: str) -> list[Path]:
    """`.mcp.json` files from the folder up to the repository root, nearest first."""

    found: list[Path] = []
    folder = Path(cwd)
    for current in (folder, *folder.parents):
        if (current / ".mcp.json").is_file():
            found.append(current / ".mcp.json")
        if (current / ".git").exists():
            break
    return found


def _servers_of(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    inner = data.get("mcpServers")
    if isinstance(inner, dict):
        return inner
    # A plugin's .mcp.json may list the servers at the top level.
    return {key: value for key, value in data.items() if isinstance(value, dict) and ("command" in value or "url" in value)}


def _plugin_servers(primary: Path, warnings: list[str]) -> dict[str, tuple[dict[str, Any], dict[str, str]]]:
    """Servers of enabled user-scope plugins, named `<plugin>_<server>`, with the variables to expand."""

    settings = read_json(primary / "settings.json", {}) or {}
    enabled = settings.get("enabledPlugins") if isinstance(settings, dict) else None
    installed = (read_json(primary / "plugins" / "installed_plugins.json", {}) or {}).get("plugins") or {}
    result: dict[str, tuple[dict[str, Any], dict[str, str]]] = {}
    if not isinstance(enabled, dict) or not isinstance(installed, dict):
        return result
    for plugin_id, on in enabled.items():
        if on is not True:
            continue
        entries = [entry for entry in installed.get(plugin_id) or [] if isinstance(entry, dict) and entry.get("scope", "user") == "user"]
        root = next((entry.get("installPath") for entry in entries if entry.get("installPath")), None)
        if not root or not Path(root).is_dir():
            continue  # claude.py already warns about a missing plugin folder for its own workers
        plugin = str(plugin_id).split("@")[0]
        manifest = read_json(Path(root) / ".claude-plugin" / "plugin.json", {}) or read_json(Path(root) / "plugin.json", {}) or {}
        servers: dict[str, Any] = {}
        inline = manifest.get("mcpServers") if isinstance(manifest, dict) else None
        if isinstance(inline, dict):
            servers.update(inline)
        elif isinstance(inline, str) and (Path(root) / inline).is_file():
            servers.update(_servers_of(read_json(Path(root) / inline, {})))
        if not servers:
            servers.update(_servers_of(read_json(Path(root) / ".mcp.json", {})))
        for name, server in servers.items():
            if isinstance(server, dict):
                result[f"{plugin}_{name}"] = (server, {"CLAUDE_PLUGIN_ROOT": str(root)})
    return result


def collect_servers(primary: Path, cwd: str, environ: dict[str, str] | None = None) -> Collected:
    """The MCP servers a Claude Code session in `cwd` uses, as Claude-shaped entries.

    Order of precedence, like Claude Code: local, then project (`.mcp.json`, only the
    ones the user approved), then user, then plugins. claude.ai connectors are
    account-bound and never listed here. Our own server is dropped.
    """

    environ = dict(os.environ if environ is None else environ)
    result = Collected()
    user, local, project_state = _state_servers(primary, cwd)
    disabled = {str(item) for item in project_state.get("disabledMcpServers") or []}

    candidates: dict[str, tuple[dict[str, Any], dict[str, str]]] = {}
    for name, server in _plugin_servers(primary, result.warnings).items():
        candidates[name] = server
    for name, server in user.items():
        if isinstance(server, dict):
            candidates[str(name)] = (server, {})
    settings = read_json(primary / "settings.json", {}) or {}
    approve_all = bool(project_state.get("enableAllProjectMcpServers")) or (
        isinstance(settings, dict) and bool(settings.get("enableAllProjectMcpServers"))
    )
    approved = {str(item) for item in project_state.get("enabledMcpjsonServers") or []}
    rejected = {str(item) for item in project_state.get("disabledMcpjsonServers") or []}
    for path in reversed(_project_mcp_files(cwd)):  # nearest file wins
        for name, server in _servers_of(read_json(path, {})).items():
            if not isinstance(server, dict) or str(name) in rejected:
                continue
            if approve_all or str(name) in approved:
                candidates[str(name)] = (server, {})
            else:
                result.warnings.append(f"MCP server '{name}' from {path.name} is not approved in Claude Code; not shared")
    for name, server in local.items():
        if isinstance(server, dict):
            candidates[str(name)] = (server, {})

    for name, (server, extra) in candidates.items():
        if name in disabled or _is_self(name, server):
            continue
        missing: set[str] = set()
        expanded = _expand(server, environ, extra, missing)
        if missing:
            result.warnings.append(f"MCP server '{name}' skipped: it needs unset variable(s) {', '.join(sorted(missing))}")
            continue
        result.servers[name] = expanded
    return result


def collect_native_servers(host: str, primary: Path, cwd: str, environ: dict[str, str] | None = None) -> Collected:
    """Parent settings in the common shape used by the runtime translators.

    Read only MCP configuration, never login files or provider credentials.
    Claude's scope/approval rules remain in collect_servers.
    """

    if host == "claude":
        return collect_servers(primary, cwd, environ)
    source_env = dict(os.environ if environ is None else environ)
    result = Collected()
    candidates: dict[str, Any] = {}
    if host == "codex":
        from cli_subagents.runtimes.codex import config_files

        for path in config_files(primary, cwd):
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8")).get("mcp_servers") or {}
                if isinstance(data, dict):
                    for name, entry in data.items():
                        if isinstance(entry, dict):
                            # Config layers can override one field of a server.
                            candidates[name] = {**candidates.get(name, {}), **entry}
            except (OSError, UnicodeError, ValueError):
                result.warnings.append(f"could not read parent MCP settings from {path.name}")
    elif host == "antigravity":
        root = primary.parent if primary.name == "antigravity-cli" else primary
        candidates = _servers_of(read_json(root / "config" / "mcp_config.json", {}))
    else:
        return result
    for name, original in candidates.items():
        if not isinstance(original, dict) or original.get("enabled") is False or original.get("disabled") is True:
            continue
        server = dict(original)
        if host == "codex":
            missing = []
            env = {}
            for key in _string_list(server.get("env_vars")):
                if key in source_env:
                    env[key] = source_env[key]
                else:
                    missing.append(key)
            env.update(_string_map(server.get("env")))
            if env:
                server["env"] = env
            headers = _string_map(server.get("http_headers"))
            for header, variable in _string_map(server.get("env_http_headers")).items():
                if variable in source_env:
                    headers[header] = source_env[variable]
                else:
                    missing.append(variable)
            token = server.get("bearer_token_env_var")
            if token:
                if str(token) in source_env:
                    headers["Authorization"] = "Bearer " + source_env[str(token)]
                else:
                    missing.append(str(token))
            if missing:
                result.warnings.append(f"MCP server '{name}' skipped: it needs unset variable(s) {', '.join(sorted(set(missing)))}")
                continue
            if headers:
                server["headers"] = headers
        elif server.get("serverUrl"):
            server["url"] = server.pop("serverUrl")
        if _is_self(str(name), server):
            continue
        result.servers[str(name)] = server
    return result


# Shared helpers -----------------------------------------------------------


def _transport(server: dict[str, Any]) -> str | None:
    kind = str(server.get("type") or "").lower()
    if kind in ("", "stdio"):
        if server.get("command"):
            return "stdio"
        return "http" if server.get("url") and not kind else None
    if kind in ("http", "streamable-http", "streamable_http"):
        return "http"
    return kind  # sse, ws, ...


def _string_list(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _string_map(value: Any) -> dict[str, str]:
    return {str(key): str(item) for key, item in value.items()} if isinstance(value, dict) else {}


def _secret_in_args(args: list[str]) -> str | None:
    """A reason when an argument looks like it carries a credential (it would show on the command line)."""

    previous_flag = False
    for arg in args:
        if URL_CREDENTIALS_RE.search(arg):
            return "an argument contains a URL with a password"
        if arg.startswith("-") and "=" in arg and SECRET_FLAG_RE.search(arg.split("=", 1)[0]) and arg.split("=", 1)[1]:
            return f"argument {arg.split('=', 1)[0]} carries a value"
        if previous_flag and not arg.startswith("-"):
            return "a credential-like flag is followed by a value"
        previous_flag = arg.startswith("-") and "=" not in arg and bool(SECRET_FLAG_RE.search(arg))
    return None


def _toml(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(str(k), ensure_ascii=False)} = {_toml(v)}" for k, v in value.items()) + "}"
    return json.dumps(str(value), ensure_ascii=False)


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


# Codex --------------------------------------------------------------------


@dataclass
class CodexServers:
    overrides: list[str] = field(default_factory=list)  # `-c key=value` argv parts
    env: dict[str, str] = field(default_factory=dict)  # variables to add to the Codex process
    names: list[str] = field(default_factory=list)  # servers that were passed on
    warnings: list[str] = field(default_factory=list)


def to_codex(servers: dict[str, dict[str, Any]], taken: set[str], base_env: dict[str, str]) -> CodexServers:
    """`-c` overrides and process environment that give a Codex worker these servers.

    `taken` holds server names the user's Codex config already defines; those win.
    `base_env` is the worker's environment before our additions. Secrets (`env` values,
    header values, bearer tokens) go into `CodexServers.env`, never into the overrides;
    stdio servers receive them through `env_vars`, HTTP servers through
    `bearer_token_env_var` and `env_http_headers`.
    """

    out = CodexServers()
    claimed = {name.lower() for name in taken}
    process_env = {key.upper(): value for key, value in base_env.items()}
    for index, (original, server) in enumerate(servers.items()):
        name = _safe_name(original)
        skip = _codex_one(out, index, original, name, server, claimed, process_env)
        if skip:
            out.warnings.append(f"MCP server '{original}' not shared with Codex: {skip}")
    return out


def _codex_one(
    out: CodexServers, index: int, original: str, name: str, server: dict[str, Any], claimed: set[str], process_env: dict[str, str]
) -> str | None:
    if name.lower() in claimed:
        return None  # already in the user's Codex config (or earlier in this list): theirs wins, nothing to report
    transport = _transport(server)
    prefix = f"mcp_servers.{name}"
    overrides: list[str] = []
    env: dict[str, str] = {}
    if transport == "stdio":
        command = str(server["command"])
        args = _string_list(server.get("args"))
        reason = _secret_in_args(args)
        if reason:
            return f"{reason}; it would be visible on the command line"
        overrides += [f"{prefix}.command={_toml(command)}"]
        if args:
            overrides.append(f"{prefix}.args={_toml(args)}")
        if server.get("cwd"):
            overrides.append(f"{prefix}.cwd={_toml(server['cwd'])}")
        names: list[str] = []
        for key, value in _string_map(server.get("env")).items():
            upper = key.upper()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or upper.startswith(PROTECTED_ENV_PREFIXES):
                return f"its env variable {key} can't be passed to a Codex worker"
            current = {**process_env, **{k.upper(): v for k, v in out.env.items()}, **{k.upper(): v for k, v in env.items()}}
            if upper in current and current[upper] != value:
                return f"its env variable {key} clashes with another value in the worker's environment"
            env[key] = value
            names.append(key)
        if names:
            overrides.append(f"{prefix}.env_vars={_toml(names)}")
    elif transport == "http":
        url = str(server.get("url") or "")
        if not url:
            return "no url"
        if URL_CREDENTIALS_RE.search(url) or "?" in url:
            return "its url carries credentials or a query string that would be visible on the command line"
        if server.get("headersHelper"):
            return "it uses headersHelper"
        overrides.append(f"{prefix}.url={_toml(url)}")
        safe = re.sub(r"[^A-Za-z0-9]", "_", name).upper()
        env_headers: dict[str, str] = {}
        for position, (header, value) in enumerate(_string_map(server.get("headers")).items()):
            variable = f"CSA_MCP_{index}_{safe}_{position}"
            bearer = re.fullmatch(r"(?i)bearer\s+(\S+)", value) if header.lower() == "authorization" else None
            if bearer:
                variable = f"CSA_MCP_{index}_{safe}_TOKEN"
                env[variable] = bearer.group(1)
                overrides.append(f"{prefix}.bearer_token_env_var={_toml(variable)}")
            else:
                env[variable] = value
                env_headers[header] = variable
        if env_headers:
            overrides.append(f"{prefix}.env_http_headers={_toml(env_headers)}")
    else:
        return f"transport '{transport or 'unknown'}' isn't supported by Codex (stdio and streamable HTTP only)"
    claimed.add(name.lower())
    out.names.append(name)
    out.env.update(env)
    out.overrides += [part for override in overrides for part in ("-c", override)]
    return None


# Antigravity --------------------------------------------------------------


def to_agy(servers: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """The `mcpServers` object for Antigravity's mcp_config.json: command/args/env/cwd, or serverUrl/headers.

    agy's MCP page documents one `serverUrl` key for both streamable HTTP and SSE servers.

    The file lives in the job's folder, so values are written as they are.
    """

    result: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for name, server in servers.items():
        transport = _transport(server)
        if transport == "stdio":
            entry: dict[str, Any] = {"command": str(server["command"]), "args": _string_list(server.get("args"))}
            if server.get("env"):
                entry["env"] = _string_map(server["env"])
            if server.get("cwd"):
                entry["cwd"] = str(server["cwd"])  # agy's mcp_config.json takes cwd (antigravity.google/docs/mcp)
        elif transport in ("http", "sse") and server.get("url"):
            entry = {"serverUrl": str(server["url"])}
            if server.get("headers"):
                entry["headers"] = _string_map(server["headers"])
        else:
            warnings.append(f"MCP server '{name}' not shared with Antigravity: transport '{transport or 'unknown'}' not supported")
            continue
        result[name] = entry
    return result, warnings


# OpenCode -----------------------------------------------------------------


@dataclass
class OpenCodeServers:
    servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def to_opencode(servers: dict[str, dict[str, Any]]) -> OpenCodeServers:
    """Translate to v2's `mcp.servers`, keeping private values in the environment.

    OpenCode expands `{env:NAME}` in its inline config. Disable Code Mode so parent
    messaging and the other MCP tools remain directly callable by the worker.
    """

    out = OpenCodeServers()
    for index, (name, server) in enumerate(servers.items()):
        transport = _transport(server)
        entry: dict[str, Any] = {"codemode": False}
        values: dict[str, str] = {}

        def reference(value: str) -> str:
            variable = f"CSA_OPENCODE_MCP_{index}_{len(values)}"
            values[variable] = value
            return "{env:" + variable + "}"

        if transport == "stdio":
            entry.update(type="local", command=[str(server["command"]), *_string_list(server.get("args"))])
            # Arguments can themselves contain tokens. Config substitution avoids
            # writing them to a job config or placing them on csa's command line.
            entry["command"] = [reference(value) for value in entry["command"]]
            if server.get("env"):
                entry["environment"] = {key: reference(value) for key, value in _string_map(server["env"]).items()}
            if server.get("cwd"):
                entry["cwd"] = str(server["cwd"])
        elif transport in ("http", "sse") and server.get("url") and not server.get("headersHelper"):
            entry.update(type="remote", url=reference(str(server["url"])))
            if server.get("headers"):
                entry["headers"] = {key: reference(value) for key, value in _string_map(server["headers"]).items()}
        else:
            reason = "headersHelper is unsupported" if server.get("headersHelper") else f"transport '{transport or 'unknown'}' is unsupported"
            out.warnings.append(f"MCP server '{name}' not shared with OpenCode: {reason}")
            continue
        out.servers[name] = entry
        out.env.update(values)
    return out
