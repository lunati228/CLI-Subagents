"""Setup bundle: carries a person's Claude Code setup from the PC to the server.

The PC side (`build_bundle`) packs skills, agents, commands, rules, output
styles, workflows, the user CLAUDE.md, settings without anything that picks a
login, MCP servers that can run on another machine and the enabled plugins into
one zip with a stable sha256. The server side (`install_bundle`,
`apply_to_account`) checks and unpacks the zip into one folder per bundle and
copies the instruction files into an account folder.

Login data never goes into a bundle: `.credentials.json` and `.claude.json` are
never read as files to pack, login settings and login environment variables are
removed from settings.json, and files that look like secrets are left out.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

FORMAT_VERSION = 1
MAX_BUNDLE_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ENTRIES = 20_000
MARKER = ".remote-agent-managed.json"

# Fixed zip metadata, so the same setup always gives the same bytes.
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
ZIP_FILE_ATTR = (stat.S_IFREG | 0o644) << 16
ZIP_UNIX = 3

# Folders of the config folder that are packed whole.
WHOLE_FOLDERS = ("agents", "commands", "output-styles", "rules", "workflows")
# Folders and files that apply_to_account manages entry by entry.
ACCOUNT_FOLDERS = (*WHOLE_FOLDERS, "skills")
ACCOUNT_FILES = ("CLAUDE.md",)
# Claude Code fills skills/synced per account, so it never travels.
SKIP_SKILLS = frozenset({"synced"})

# Folders of the config folder that hold sessions, history or login state.
# They are never packed, even when a link inside a packed folder points there.
PRIVATE_FOLDERS = (
    "projects", "sessions", "todos", "statsig", "shell-snapshots", "ide", "backups",
    "file-history", "paste-cache", "debug", "telemetry",
)
SKIP_DIRS = frozenset({"node_modules", ".git", "__pycache__"})
SKIP_FILES = ("*.pyc", ".ds_store")
# File names that hold logins, keys or shell history (matched in lower case).
SECRET_FILES = (
    ".env", ".env.*", ".envrc", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*",
    ".credentials*", "credentials", "credentials.*", "*credentials.json", ".git-credentials",
    ".netrc", ".npmrc", ".pypirc", "secrets.*", "*.secret", "*.secrets",
    ".claude.json", ".claude.json.*", "history.jsonl", "*.history", ".*_history",
)

# Settings that pick a login method, a gateway or a credential helper.
LOGIN_SETTINGS = frozenset({
    "apiKeyHelper", "awsAuthRefresh", "awsCredentialExport", "gcpAuthRefresh",
    "forceLoginMethod", "forceLoginOrgUUID", "forceLoginGatewayUrl",
    "otelHeadersHelper", "proxyAuthHelper",
})
# Settings that run commands on the PC.
LOCAL_COMMAND_SETTINGS = ("hooks", "statusLine", "fileSuggestion")
# Settings that install plugins or name folders on the PC; plugins travel separately.
OTHER_DROPPED_SETTINGS = ("enabledPlugins", "extraKnownMarketplaces")

LOGIN_ENV_EXACT = frozenset({
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY", "AWS_BEARER_TOKEN_BEDROCK", "CLAUDE_CONFIG_DIR", "AWS_PROFILE",
})
SECRET_ENV_PARTS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "APIKEY",
                    "ACCESS_KEY", "PRIVATE_KEY", "CREDENTIAL")

PORTABLE_COMMANDS = frozenset({"npx", "uvx", "uv", "bunx", "deno", "docker", "node", "python", "python3"})
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
_WINDOWS_SUFFIXES = (".cmd", ".exe", ".bat", ".ps1")
# A path that only exists on the PC: drive letter, UNC share, Git Bash drive,
# user home folders and home shortcuts.
_LOCAL_PATH = re.compile(
    r"^(?:[A-Za-z]:(?:[\\/]|$)|\\\\|//|/[A-Za-z](?:/|$)|/(?:Users|home|root)(?:/|$)"
    r"|~(?:[\\/]|$)|\$\{?HOME\}?(?:[\\/]|$)|%(?:USERPROFILE|HOMEPATH|HOMEDRIVE|APPDATA|LOCALAPPDATA)%)"
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TEMP_PREFIX = ".tmp-bundle-"
_STALE_TEMP_SECONDS = 3600


class BundleError(ValueError):
    """A bundle could not be built, verified or installed."""


class BundleTooLarge(BundleError):
    """The setup is larger than the size limit."""


@dataclass
class Bundle:
    data: bytes
    sha256: str
    size: int
    manifest: dict
    warnings: list[str] = field(default_factory=list)


@dataclass
class InstalledBundle:
    root: Path
    claude_dir: Path
    settings_path: Path
    mcp_config_path: Path
    plugin_dirs: list[Path]
    manifest: dict


# Building on the PC ---------------------------------------------------------


def default_state_files(config_dir: Path) -> list[Path]:
    """Where Claude Code keeps `.claude.json` (MCP servers, projects) for this config folder."""

    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return [config_dir / ".claude.json"]
    # Without CLAUDE_CONFIG_DIR the state file sits next to the config folder.
    return [config_dir.parent / ".claude.json"]


def build_bundle(
    config_dir: Path,
    cwd: str,
    *,
    state_files: list[Path] | None = None,
    max_bytes: int = MAX_BUNDLE_BYTES,
) -> Bundle:
    """Pack the setup in `config_dir` (plus MCP servers for project `cwd`) into a zip."""

    config_dir = Path(config_dir)
    if state_files is None:
        state_files = default_state_files(config_dir)
    packer = _Packer(config_dir, max_bytes)
    manifest: dict[str, Any] = {"format_version": FORMAT_VERSION}

    for folder in WHOLE_FOLDERS:
        source = config_dir / folder
        names = packer.add_tree(source, f"claude/{folder}") if source.is_dir() else []
        manifest[folder.replace("-", "_")] = names

    skills: list[str] = []
    skills_dir = config_dir / "skills"
    if skills_dir.is_dir():
        for entry in sorted(skills_dir.iterdir(), key=lambda p: p.name):
            if entry.name in SKIP_SKILLS or entry.name.startswith(".") or entry.name in SKIP_DIRS:
                continue
            arc = f"claude/skills/{entry.name}"
            added = packer.add_tree(entry, arc) if entry.is_dir() else packer.add_file(entry, arc)
            if added:
                skills.append(entry.name)
    manifest["skills"] = skills

    claude_md = config_dir / "CLAUDE.md"
    manifest["claude_md"] = claude_md.is_file() and packer.add_file(claude_md, "claude/CLAUDE.md")

    raw_settings = _read_json(config_dir / "settings.json", packer.warnings, "settings.json")
    if raw_settings is not None and not isinstance(raw_settings, dict):
        packer.warnings.append("settings.json is not a JSON object; settings not included")
        raw_settings = None
    settings, dropped, env_dropped = _filter_settings(raw_settings or {}, packer.warnings)
    packer.add_bytes("settings.json", _json_bytes(settings))
    manifest["settings"] = {"dropped": dropped, "env_dropped": env_dropped}

    servers = _collect_mcp_servers(state_files, cwd, packer.warnings)
    kept, skipped = _portable_mcp_servers(servers)
    for name, reason in skipped.items():
        packer.warnings.append(f"MCP server '{name}' not included: {reason}")
    packer.add_bytes("mcp.json", _json_bytes({"mcpServers": kept}))
    manifest["mcp_servers"] = {"kept": sorted(kept), "skipped": skipped}
    manifest["mcp_credentials"] = (
        "env and headers of kept MCP servers are included as configured; the bundle is "
        "installed only in the person's own folder on the server"
    )

    manifest["plugins"] = _add_plugins(config_dir, raw_settings or {}, packer)
    manifest["warnings"] = list(packer.warnings)
    packer.add_bytes("manifest.json", _json_bytes(manifest))

    data = packer.zip_bytes()
    return Bundle(
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        manifest=manifest,
        warnings=list(packer.warnings),
    )


class _Packer:
    """Collects zip entries in memory, applying the exclusion and size rules."""

    def __init__(self, config_dir: Path, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.files: dict[str, bytes] = {}
        self.total = 0
        self.warnings: list[str] = []
        config_real = _real(config_dir)
        self.blocked_exact = {config_real}
        self.blocked_under = [_real(config_dir / name) for name in PRIVATE_FOLDERS]
        self.blocked_under.append(_real(Path.home() / ".ssh"))

    def _blocked(self, path: Path, *, folder: bool = True) -> bool:
        """True for the config folder itself (as a folder to walk) and anything inside a private folder."""

        real = _real(path)
        if folder and real in self.blocked_exact:
            return True
        return any(real == blocked or real.startswith(blocked + os.sep) for blocked in self.blocked_under)

    def add_bytes(self, arc: str, data: bytes) -> None:
        self.files[arc] = data
        self.total += len(data)
        if self.total > self.max_bytes:
            raise BundleTooLarge(
                f"the setup bundle is larger than the limit of {self.max_bytes} bytes; "
                "remove large files from skills, agents or plugins"
            )

    def add_file(self, path: Path, arc: str) -> bool:
        name = path.name.lower()
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in SKIP_FILES):
            return False
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_FILES):
            self.warnings.append(f"left out {arc}: the file name looks like a secret or login file")
            return False
        # A link is judged by the file it points at, so a harmless-looking name
        # can't carry a login or key file into the bundle.
        try:
            real = Path(os.path.realpath(path))
        except OSError:
            return False
        real_name = real.name.lower()
        if real != path and (any(fnmatch.fnmatchcase(real_name, pattern) for pattern in SECRET_FILES)
                             or any(part.lower() in {".ssh", ".aws", ".gnupg"} for part in real.parts)):
            self.warnings.append(f"left out {arc}: it links to a secret or login file")
            return False
        if self._blocked(path, folder=False) or not path.is_file():
            return False
        try:
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                self.warnings.append(f"left out {arc}: {size} bytes is over the {MAX_FILE_BYTES} byte file limit")
                return False
            data = path.read_bytes()
        except OSError as exc:
            self.warnings.append(f"left out {arc}: could not read it ({exc.strerror or exc})")
            return False
        self.add_bytes(arc, data)
        return True

    def add_tree(self, source: Path, arc_prefix: str) -> list[str]:
        """Add the allowed files under `source`; return their paths relative to it."""

        added: list[str] = []
        seen: set[str] = set()
        # Links are followed, since a person's skill folder may be a link to a repo;
        # `seen` stops loops and `_blocked` stops links into private folders.
        for root, dirnames, filenames in os.walk(source, followlinks=True):
            real = _real(Path(root))
            if real in seen or self._blocked(Path(root)):
                dirnames[:] = []
                continue
            seen.add(real)
            dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
            relative_root = Path(root).relative_to(source)
            for name in sorted(filenames):
                relative = (relative_root / name).as_posix()
                if self.add_file(Path(root) / name, f"{arc_prefix}/{relative}"):
                    added.append(relative)
        return sorted(added)

    def zip_bytes(self) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for arc in sorted(self.files):
                info = zipfile.ZipInfo(arc, date_time=ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = ZIP_UNIX
                info.external_attr = ZIP_FILE_ATTR
                archive.writestr(info, self.files[arc], compresslevel=6)
        return buffer.getvalue()


def _filter_settings(settings: dict[str, Any], warnings: list[str]) -> tuple[dict[str, Any], list[str], list[str]]:
    """Remove login settings, local commands, plugin installs and login env vars."""

    result: dict[str, Any] = {}
    login: list[str] = []
    local: list[str] = []
    dropped: list[str] = []
    for key, value in settings.items():
        if key in LOGIN_SETTINGS or key.startswith("forceLogin"):
            login.append(key)
        elif key in LOCAL_COMMAND_SETTINGS:
            local.append(key)
        elif key in OTHER_DROPPED_SETTINGS:
            dropped.append(key)
        else:
            result[key] = value
    if login:
        warnings.append(f"login settings not included: {', '.join(sorted(login))}")
    if local:
        warnings.append(f"settings that run commands on the PC not included: {', '.join(sorted(local))}")

    permissions = result.get("permissions")
    if isinstance(permissions, dict) and "additionalDirectories" in permissions:
        # Those folders are on the PC and don't exist on the server.
        result["permissions"] = {k: v for k, v in permissions.items() if k != "additionalDirectories"}
        dropped.append("permissions.additionalDirectories")

    env_dropped: list[str] = []
    env = result.get("env")
    if isinstance(env, dict):
        env_dropped = sorted(name for name in env if is_login_env(name))
        if env_dropped:
            warnings.append(f"login and secret env settings not included: {', '.join(env_dropped)}")
        result["env"] = {name: value for name, value in env.items() if not is_login_env(name)}
    return result, sorted(login + local + dropped), env_dropped


def is_login_env(name: str) -> bool:
    """True for env variables that pick a login, provider or config folder, or hold a secret."""

    upper = name.upper()
    if upper.startswith("ANTHROPIC_") or upper in LOGIN_ENV_EXACT:
        return True
    if upper.startswith("CLAUDE_CODE_SKIP_") and upper.endswith("_AUTH"):
        return True
    return any(part in upper for part in SECRET_ENV_PARTS)


def _collect_mcp_servers(state_files: list[Path], cwd: str, warnings: list[str]) -> dict[str, Any]:
    """User-scope servers plus the servers set up for project `cwd`; project entries win."""

    servers: dict[str, Any] = {}
    wanted = _norm_path(cwd)
    for path in state_files:
        state = _read_json(Path(path), warnings, Path(path).name)
        if not isinstance(state, dict):
            continue
        user = state.get("mcpServers")
        if isinstance(user, dict):
            servers.update(user)
        projects = state.get("projects")
        if isinstance(projects, dict):
            for key, project in projects.items():
                if isinstance(project, dict) and _norm_path(str(key)) == wanted:
                    local = project.get("mcpServers")
                    if isinstance(local, dict):
                        servers.update(local)
    return servers


def _portable_mcp_servers(servers: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    kept: dict[str, Any] = {}
    skipped: dict[str, str] = {}
    for name in sorted(servers):
        server = servers[name]
        if not isinstance(server, dict):
            skipped[name] = "its entry is not a JSON object"
            continue
        if "url" in server or server.get("type") in ("http", "sse", "ws"):
            reason = _url_problem(server.get("url"))
            if reason:
                skipped[name] = reason
            else:
                kept[name] = dict(server)
            continue
        portable, reason = _portable_stdio(server)
        if portable is None:
            skipped[name] = reason
        else:
            kept[name] = portable
    return kept, skipped


def _url_problem(url: Any) -> str:
    if not isinstance(url, str) or not url:
        return "it has no url"
    try:
        host = urlsplit(url).hostname
    except ValueError:
        host = None
    if not host:
        return "its url has no host"
    host = host.lower()
    if host in LOCAL_HOSTS or host.endswith(".localhost") or host.startswith("127."):
        return f"its url points at the PC itself ({host})"
    return ""


def _portable_stdio(server: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    command = server.get("command")
    args = server.get("args") or []
    if not isinstance(command, str) or not command.strip():
        return None, "it has no command"
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return None, "its args are not a list of strings"
    args = list(args)
    if not args and " " in command.strip():
        command, *args = command.split()

    if _command_name(command) == "cmd":
        # Windows wraps launchers as `cmd /c npx ...`; the server runs the launcher directly.
        rest = list(args)
        while rest and rest[0].lower() in ("/s", "/q", "/d"):
            rest.pop(0)
        if not rest or rest[0].lower() not in ("/c", "/k") or len(rest) < 2:
            return None, "it runs cmd without a command to unwrap"
        command, args = rest[1], rest[2:]
        if not args and " " in command.strip():
            command, *args = command.split()

    if "/" in command or "\\" in command:
        return None, f"its command is a path on the PC ({command})"
    name = _command_name(command)
    if name not in PORTABLE_COMMANDS:
        return None, f"its command '{command}' is not one of {', '.join(sorted(PORTABLE_COMMANDS))}"
    for arg in args:
        if _is_local_path(arg):
            return None, f"an argument uses a path on the PC ({arg})"

    portable = dict(server)
    portable["command"] = name
    if args or "args" in server:
        portable["args"] = args
    return portable, ""


def _command_name(command: str) -> str:
    base = re.split(r"[\\/]", command.strip())[-1].lower()
    for suffix in _WINDOWS_SUFFIXES:
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def _is_local_path(arg: str) -> bool:
    candidates = [arg]
    if "=" in arg:
        candidates.append(arg.split("=", 1)[1])
    return any(_LOCAL_PATH.match(candidate) for candidate in candidates)


def _add_plugins(config_dir: Path, settings: dict[str, Any], packer: _Packer) -> list[dict[str, str]]:
    """Add every user-scope plugin enabled in settings whose folder exists."""

    enabled = settings.get("enabledPlugins")
    if not isinstance(enabled, dict):
        return []
    doc = _read_json(config_dir / "plugins" / "installed_plugins.json", packer.warnings, "installed_plugins.json")
    installed = doc.get("plugins") if isinstance(doc, dict) else None
    if not isinstance(installed, dict):
        installed = {}
    added: list[dict[str, str]] = []
    used: set[str] = set()
    for plugin_id in sorted(enabled):
        if enabled[plugin_id] is not True:
            continue
        entries = installed.get(plugin_id)
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            entries = []
        folder = next(
            (
                Path(entry["installPath"])
                for entry in entries
                if isinstance(entry, dict)
                and entry.get("scope", "user") == "user"
                and isinstance(entry.get("installPath"), str)
                and Path(entry["installPath"]).is_dir()
            ),
            None,
        )
        if folder is None:
            packer.warnings.append(f"plugin {plugin_id} is enabled but its folder was not found; not included")
            continue
        safe = _safe_id(plugin_id, used)
        packer.add_tree(folder, f"plugins/{safe}")
        added.append({"id": plugin_id, "dir": f"plugins/{safe}"})
    return added


def _safe_id(plugin_id: str, used: set[str]) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]", "_", plugin_id).strip(".") or "plugin"
    safe, number = base, 2
    while safe in used:
        safe, number = f"{base}-{number}", number + 1
    used.add(safe)
    return safe


# Installing on the server ---------------------------------------------------


def install_bundle(
    data: bytes,
    sha256: str,
    bundles_root: Path,
    *,
    keep: int = 3,
    max_bytes: int = MAX_BUNDLE_BYTES,
) -> InstalledBundle:
    """Verify and unpack a bundle into `bundles_root/<sha256>/`, reusing an existing copy."""

    sha256 = (sha256 or "").lower()
    if not _SHA256.fullmatch(sha256):
        raise BundleError("the bundle sha256 must be 64 hex characters")
    if hashlib.sha256(data).hexdigest() != sha256:
        raise BundleError("the bundle does not match its sha256")
    if len(data) > max_bytes:
        raise BundleTooLarge(f"the bundle is larger than the limit of {max_bytes} bytes")

    bundles_root = Path(bundles_root)
    bundles_root.mkdir(parents=True, exist_ok=True)
    final = bundles_root / sha256
    if _is_link(final):
        raise BundleError("the bundle folder is a link; refusing to use it")
    if final.is_dir() and not (final / "manifest.json").is_file():
        shutil.rmtree(final)  # not a finished bundle; install it again
    if not final.is_dir():
        temporary = Path(tempfile.mkdtemp(prefix=_TEMP_PREFIX, dir=bundles_root))
        try:
            _extract(data, temporary, max_bytes)
            _load_manifest(temporary)
            _refilter(temporary)
            try:
                os.rename(temporary, final)
            except OSError:
                if not (final / "manifest.json").is_file():
                    raise
                # Another job installed the same bundle at the same time; use that copy.
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    installed = load_installed(final)
    _prune(bundles_root, final, keep)
    return installed


def _refilter(folder: Path) -> None:
    """Apply the PC-side filters again on the server.

    A changed client could send settings that redirect Claude Code to another
    API address, add hooks or login helpers, or MCP servers that point at the
    server itself. The server never relies on the PC having removed them.
    """
    warnings: list[str] = []
    settings_path = folder / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    clean, _login, _local = _filter_settings(settings if isinstance(settings, dict) else {}, warnings)
    settings_path.write_text(json.dumps(clean, indent=2, sort_keys=True), encoding="utf-8")
    mcp_path = folder / "mcp.json"
    try:
        servers = (json.loads(mcp_path.read_text(encoding="utf-8")) or {}).get("mcpServers") or {}
    except (OSError, ValueError, AttributeError):
        servers = {}
    kept, _skipped = _portable_mcp_servers(servers if isinstance(servers, dict) else {})
    kept.pop("pc", None)
    mcp_path.write_text(json.dumps({"mcpServers": kept}, indent=2, sort_keys=True), encoding="utf-8")


def load_installed(bundle_dir: Path) -> InstalledBundle:
    """Describe a bundle folder that install_bundle created earlier, without the zip.

    Raises BundleError (a ValueError) when the folder is missing or incomplete.
    """

    root = Path(bundle_dir)
    if _is_link(root) or not root.is_dir():
        raise BundleError(f"no installed bundle at {root.name}")
    if not _SHA256.fullmatch(root.name):
        raise BundleError("an installed bundle folder is named after its sha256")
    manifest = _load_manifest(root)
    for name in ("settings.json", "mcp.json"):
        if not (root / name).is_file():
            raise BundleError(f"the installed bundle is incomplete: {name} is missing")
    try:
        os.utime(root)  # mark it as recently used, so pruning keeps it
    except OSError:
        pass
    plugins = root / "plugins"
    return InstalledBundle(
        root=root,
        claude_dir=root / "claude",
        settings_path=root / "settings.json",
        mcp_config_path=root / "mcp.json",
        plugin_dirs=sorted(p for p in plugins.iterdir() if p.is_dir()) if plugins.is_dir() else [],
        manifest=manifest,
    )


def _check_entry_name(name: str) -> list[str]:
    """The parts of a safe zip entry name inside the bundle layout, or an error."""

    if not name or name.startswith(("/", "\\")) or "\\" in name or ":" in name or "\x00" in name:
        raise BundleError(f"unsafe name in bundle: {name!r}")
    parts = name.rstrip("/").split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise BundleError(f"unsafe name in bundle: {name!r}")
    top = parts[0]
    if len(parts) == 1 and not name.endswith("/"):
        allowed = top in ("settings.json", "mcp.json", "manifest.json")
    elif top == "claude":
        allowed = len(parts) == 1 or parts[1] in ACCOUNT_FOLDERS or (len(parts) == 2 and parts[1] in ACCOUNT_FILES)
    else:
        allowed = top == "plugins"
    if not allowed:
        raise BundleError(f"unexpected entry in bundle: {name!r}")
    return parts


def _extract(data: bytes, target: Path, max_bytes: int) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise BundleError(f"the bundle is not a valid zip: {exc}") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES:
            raise BundleError(f"the bundle has more than {MAX_ENTRIES} entries")
        names: set[str] = set()
        total = 0
        for info in infos:
            _check_entry_name(info.filename)
            mode = info.external_attr >> 16
            if info.create_system == ZIP_UNIX and stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise BundleError(f"links and special files are not allowed in a bundle: {info.filename!r}")
            key = info.filename.rstrip("/")
            if key in names:
                raise BundleError(f"duplicate entry in bundle: {info.filename!r}")
            names.add(key)
            if not info.is_dir() and info.file_size > MAX_FILE_BYTES:
                raise BundleTooLarge(f"bundle entry is over the {MAX_FILE_BYTES} byte file limit: {info.filename!r}")
            total += info.file_size
            if total > max_bytes:
                raise BundleTooLarge(f"the unpacked bundle is larger than the limit of {max_bytes} bytes")

        root = target.resolve()
        for info in infos:
            parts = _check_entry_name(info.filename)
            destination = target.joinpath(*parts)
            if not destination.resolve().is_relative_to(root):
                raise BundleError(f"unsafe name in bundle: {info.filename!r}")
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            try:
                with archive.open(info) as source, open(destination, "xb") as sink:
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        if written > info.file_size:
                            raise BundleError(f"bundle entry is larger than it claims: {info.filename!r}")
                        sink.write(chunk)
            except (zipfile.BadZipFile, zlib.error) as exc:
                raise BundleError(f"the bundle is damaged: {exc}") from exc


def _load_manifest(root: Path) -> dict:
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BundleError(f"the bundle has no readable manifest.json: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION:
        raise BundleError("the bundle uses an unknown format version")
    return manifest


def _prune(bundles_root: Path, current: Path, keep: int) -> None:
    """Delete the oldest bundle folders beyond `keep`, never `current`, plus stale temp folders."""

    now = time.time()
    bundles: list[tuple[float, Path]] = []
    for entry in bundles_root.iterdir():
        try:
            if _is_link(entry) or not entry.is_dir():
                continue
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if entry.name.startswith(_TEMP_PREFIX):
            if now - mtime > _STALE_TEMP_SECONDS:
                shutil.rmtree(entry, ignore_errors=True)
        elif _SHA256.fullmatch(entry.name) and entry.name != current.name:
            bundles.append((mtime, entry))
    bundles.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    for _, old in bundles[max(keep - 1, 0):]:
        shutil.rmtree(old, ignore_errors=True)


# Applying to an account folder ----------------------------------------------


def apply_to_account(installed: InstalledBundle, account_home: Path) -> list[str]:
    """Make the account's agents, commands, rules, skills and CLAUDE.md match the bundle.

    Only entries listed in the marker file were put there by a bundle; those are
    replaced or removed. Everything else in the account folder stays as it is.
    """

    warnings: list[str] = []
    home = Path(account_home)
    if _is_link(home):
        return ["the account folder is a link; setup not applied"]
    home.mkdir(parents=True, exist_ok=True)
    marker = home / MARKER
    if _is_link(marker):
        return [f"{MARKER} is a link; setup not applied"]
    items = _read_marker(marker)

    desired: dict[str, Path] = {}
    for folder in ACCOUNT_FOLDERS:
        source_dir = installed.claude_dir / folder
        if not source_dir.is_dir():
            continue
        for entry in sorted(source_dir.iterdir(), key=lambda p: p.name):
            key = f"{folder}/{entry.name}"
            if _valid_key(key):
                desired[key] = entry
    for name in ACCOUNT_FILES:
        if (installed.claude_dir / name).is_file():
            desired[name] = installed.claude_dir / name

    for key in sorted(set(items) - set(desired), reverse=True):
        try:
            if _remove_managed(home, key, warnings):
                items.pop(key)
        except OSError as exc:
            warnings.append(f"could not remove {key}: {exc}")

    for key, source in sorted(desired.items()):
        destination = home / key
        if destination.parent != home:
            if _is_link(destination.parent) or (destination.parent.exists() and not destination.parent.is_dir()):
                warnings.append(f"{key}: its folder in the account is a link or a file; left alone")
                continue
            destination.parent.mkdir(exist_ok=True)
        if os.path.lexists(destination) and key not in items:
            warnings.append(f"{key}: the account has its own version; left in place")
            continue
        try:
            digest = _digest(source)
            current = None if _is_link(destination) or not destination.exists() else _digest(destination)
            if current != digest:
                _replace(source, destination)
            items[key] = {"sha256": digest}
        except OSError as exc:
            warnings.append(f"could not update {key}: {exc}")

    _write_json_atomic(marker, {"version": 1, "bundle": installed.root.name, "items": items})
    return warnings


def _valid_name(name: str) -> bool:
    return bool(name) and not name.startswith(".") and not any(char in name for char in "/\\:")


def _valid_key(key: str) -> bool:
    """Only keys of the managed layout count, so a changed marker can't reach other files."""

    if key in ACCOUNT_FILES:
        return True
    folder, _, name = key.partition("/")
    return folder in ACCOUNT_FOLDERS and _valid_name(name) and not (folder == "skills" and name in SKIP_SKILLS)


def _read_marker(marker: Path) -> dict[str, dict]:
    try:
        doc = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    raw = doc.get("items") if isinstance(doc, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, dict) and _valid_key(key)}


def _remove_managed(home: Path, key: str, warnings: list[str]) -> bool:
    destination = home / key
    if destination.parent != home and _is_link(destination.parent):
        warnings.append(f"{key}: its folder in the account is a link; left alone")
        return False
    if _is_link(destination):
        _unlink_link(destination)
    elif destination.is_dir():
        shutil.rmtree(destination)
    elif os.path.lexists(destination):
        destination.unlink()
    return True


def _replace(source: Path, destination: Path) -> None:
    """Copy `source` next to `destination`, then swap it in, so a reader never sees half a copy."""

    token = uuid.uuid4().hex[:12]
    temporary = destination.with_name(f".{destination.name}.new-{token}")
    if source.is_dir():
        shutil.copytree(source, temporary)
    else:
        shutil.copyfile(source, temporary)
    try:
        if os.path.lexists(destination):
            old = destination.with_name(f".{destination.name}.old-{token}")
            os.rename(destination, old)
            os.rename(temporary, destination)
            if _is_link(old):
                _unlink_link(old)
            elif old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
            else:
                old.unlink()
        else:
            os.rename(temporary, destination)
    finally:
        if temporary.is_dir() and not _is_link(temporary):
            shutil.rmtree(temporary, ignore_errors=True)
        elif os.path.lexists(temporary):
            temporary.unlink()


def _digest(path: Path) -> str:
    """Content hash of a file or folder tree (names and bytes), to skip unchanged copies."""

    digest = hashlib.sha256()
    if path.is_file():
        digest.update(b"file\0" + path.read_bytes())
        return digest.hexdigest()
    for root, dirnames, filenames in os.walk(path):
        dirnames.sort()
        for name in sorted(filenames):
            file_path = Path(root) / name
            relative = file_path.relative_to(path).as_posix()
            if file_path.is_symlink():
                digest.update(f"link\0{relative}\0{os.readlink(file_path)}\0".encode())
                continue
            digest.update(f"file\0{relative}\0".encode())
            digest.update(hashlib.sha256(file_path.read_bytes()).digest())
    return digest.hexdigest()


# Helpers --------------------------------------------------------------------


def _read_json(path: Path, warnings: list[str], label: str) -> Any:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except OSError as exc:
        warnings.append(f"could not read {label}: {exc.strerror or exc}")
        return None
    try:
        return json.loads(text)
    except ValueError:
        warnings.append(f"{label} is not valid JSON; ignored")
        return None


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _write_json_atomic(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.new-{uuid.uuid4().hex[:12]}")
    temporary.write_bytes(_json_bytes(value))
    os.replace(temporary, path)


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(path.replace("/", os.sep)))


def _real(path: Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def _is_link(path: Path) -> bool:
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)  # Windows junctions, Python 3.12+
    return bool(isjunction and isjunction(path))


def _unlink_link(path: Path) -> None:
    try:
        os.unlink(path)
    except (IsADirectoryError, PermissionError):
        os.rmdir(path)  # a Windows junction or directory link is removed this way
