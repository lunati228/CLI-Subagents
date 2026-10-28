"""Backend-neutral filesystem policy construction and path validation.

The policy grants workspaces, protects code-running settings and denies secrets.
Backend-specific schema and enforcement details live in backends/.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Iterable

from cli_subagents.sandbox.model import Policy, PolicyError

# Inside writable workspaces these stay read-only when they exist and can't be
# created when they don't: git internals, agent and editor settings, hooks.
# All of them run code later, outside the sandbox (the next git command, agent
# session or editor start). Mirrors the review gate's write-protected names.
PROTECTED_NAMES = (
    ".git", ".agents", ".agent", ".claude", ".codex", ".cursor", ".gemini", ".opencode", ".windsurf",
    ".devcontainer", ".husky", ".vscode", "opencode.json", "opencode.jsonc",
)
# Credential folders in the home folder: denied, so their file names stay
# hidden even with enumerate-only access to the drive.
HIDDEN_HOME_DIRS = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store")
# Tool and SDK variables whose folders a worker's commands need to read; the
# list follows MXC's own available_tools_policy (src/mxc-sdk/src/policy.rs, MIT).
TOOL_VARIABLES = (
    ("PYTHONPATH", True), ("PYTHONHOME", False), ("VCINSTALLDIR", False), ("VSINSTALLDIR", False),
    ("PSModulePath", True), ("VCPKG_ROOT", False), ("GOPATH", False), ("GOROOT", False), ("CARGO_HOME", False),
    ("RUSTUP_HOME", False), ("JAVA_HOME", False), ("NVM_HOME", False), ("NVM_SYMLINK", False),
    ("NODE_PATH", True), ("DOTNET_ROOT", False), ("CONDA_PREFIX", False), ("VIRTUAL_ENV", False),
    ("PYENV_ROOT", False),
)
# Default per-user tool folders that hold no credentials.
HOME_TOOL_DIRS = (".rustup", "go", ".nuget/packages")
def _key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def _inside(path: str, root: str) -> bool:
    path, root = _key(path), _key(root)
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


def _real(path: str | Path) -> str:
    """Absolute final path: MXC applies grants to the resolved path, not to links."""

    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError):
        return os.path.abspath(os.path.expanduser(str(path)))


def _unique(paths: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for path in paths:
        key = _key(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def _is_root(path: str) -> bool:
    candidate = Path(path)
    return candidate == Path(candidate.anchor) or candidate.parent == candidate


def private_roots() -> list[str]:
    """Folders a worker may never write in: csa's home, agent homes and credential folders."""

    from cli_subagents.config import home as csa_home

    user = Path.home()
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME") or user / ".config")
    xdg_data = Path(os.environ.get("XDG_DATA_HOME") or user / ".local" / "share")
    roots = [csa_home(), user / ".claude", user / ".codex", user / ".gemini", xdg_config / "opencode",
             xdg_data / "opencode", *(user / name for name in HIDDEN_HOME_DIRS)]
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        value = os.environ.get(name)
        if value:
            roots.append(Path(value))
    return _unique(_real(root) for root in roots)


def container_roots() -> list[str]:
    """Folders that hold many apps' settings: never writable as a whole (their subfolders may be projects)."""

    user = Path.home()
    roots = [user, user / ".config", user / ".local", user / ".local" / "share", user / ".cache"]
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA", "LOCALAPPDATA"):
        value = os.environ.get(name)
        if value:
            roots.append(Path(value))
    if os.name == "nt":
        roots += [user / "AppData", user / "AppData" / "Local", user / "AppData" / "Roaming"]
    return _unique(_real(root) for root in roots)


def check_workspace(path: str) -> str:
    """The resolved workspace, or PolicyError when making it writable would expose settings or the profile."""

    real = _real(path)
    if _is_root(real):
        raise PolicyError(f"the sandbox won't make a drive root writable ({real}); use a project folder")
    for root in [*container_roots(), *private_roots()]:
        if _inside(root, real):
            raise PolicyError(f"the sandbox won't make {real} writable: it contains {root} (your profile, "
                              "app or agent settings, or csa's own files); use a project folder")
    for root in private_roots():
        if _inside(real, root):
            raise PolicyError(f"the sandbox won't make {real} writable: it is inside {root}, which holds agent "
                              "settings, credentials or csa's own files")
    return real


def protections(workspace: str) -> tuple[list[str], list[str]]:
    """(read-only, denied) paths that keep a workspace's code-running config out of the worker's reach."""

    readonly, denied = [], []
    for name in PROTECTED_NAMES:
        target = Path(workspace) / name
        if os.path.lexists(target):
            readonly.append(str(target))
        else:
            denied.append(str(target))
    git = Path(workspace) / ".git"
    if git.is_file():
        readonly += git_dirs_of_worktree(git)
    return readonly, denied


def git_dirs_of_worktree(marker: Path) -> list[str]:
    """For a `.git` file (worktree or submodule): its git dir and the shared common dir, read-only."""

    try:
        text = marker.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    line = next((item for item in text.splitlines() if item.lower().startswith("gitdir:")), "")
    target = line.split(":", 1)[1].strip() if line else ""
    if not target:
        return []
    gitdir = Path(target) if Path(target).is_absolute() else marker.parent / target
    found = [_real(gitdir)]
    try:
        common = (gitdir / "commondir").read_text(encoding="utf-8").strip()
        if common:
            found.append(_real(Path(common) if Path(common).is_absolute() else gitdir / common))
    except OSError:
        pass
    return [item for item in found if Path(item).exists()]


def enclosing_git_dir(cwd: str) -> list[str]:
    """When the job runs in a subfolder of a repository, that repository's git dir (read-only)."""

    start = Path(cwd)
    for folder in start.parents:
        marker = folder / ".git"
        if marker.is_dir():
            return [_real(marker)]
        if marker.is_file():
            return [_real(marker), *git_dirs_of_worktree(marker)]
    return []


def toolchain_dirs(env: dict[str, str]) -> list[str]:
    """Read-only folders for the tools a worker runs: PATH, tool variables and per-user tool folders.

    Never a drive root, the home folder or one of its ancestors, the Windows
    folder (readable anyway), or a folder that holds credentials or agent settings.
    """

    def lookup(name: str) -> str | None:
        for key, value in env.items():
            if key.upper() == name.upper():
                return value
        return None

    candidates: list[str] = []
    for item in (lookup("PATH") or "").split(os.pathsep):
        if item.strip():
            candidates.append(item.strip().strip('"'))
    for name, is_list in TOOL_VARIABLES:
        value = lookup(name)
        if value:
            candidates += [part for part in value.split(os.pathsep) if part.strip()] if is_list else [value.strip()]
    user = Path.home()
    candidates += [str(user / relative) for relative in HOME_TOOL_DIRS]
    if os.name == "nt":
        appdata = lookup("APPDATA")
        if appdata:
            candidates.append(str(Path(appdata) / "Python"))
        local = lookup("LOCALAPPDATA")
        if local:
            programs = Path(local) / "Programs"
            try:
                candidates += [str(entry) for entry in programs.iterdir() if entry.is_dir()]
            except OSError:
                pass
    windows = os.environ.get("SystemRoot") or os.environ.get("windir")
    private, containers = private_roots(), container_roots()
    result = []
    for candidate in candidates:
        real = _real(os.path.expandvars(candidate))
        if not Path(real).is_dir() or _is_root(real):
            continue
        if windows and _inside(real, _real(windows)):
            continue
        if any(_inside(root, real) for root in [*containers, *private]):
            continue  # the home folder, AppData or an agent home itself, or one of their ancestors
        if any(_inside(real, root) for root in private):
            continue  # inside an agent home, a credentials folder or csa's home
        result.append(real)
    return _unique(result)


def python_dirs() -> list[str]:
    """The interpreter and standard library commands may use inside the sandbox."""

    import sys

    folders = [sys.prefix, sys.base_prefix, sys.exec_prefix, str(Path(sys.executable).parent)]
    return _unique(_real(folder) for folder in folders if folder)


def package_dir() -> str:
    """The folder that holds the cli_subagents package (site-packages, or src/ of a checkout)."""

    import cli_subagents

    return _real(Path(cli_subagents.__file__).resolve().parents[1])


def host_paths(executable: list[str]) -> list[str]:
    """Code used outside containment must stay trusted across command launches."""

    programs = [str(Path(item).resolve().parent) for item in executable[:2]
                if os.path.isabs(item) and Path(item).is_file()]
    return _unique([package_dir(), *python_dirs(), *programs])


def git_config_paths() -> list[str]:
    """The user's git config files, so git inside the sandbox behaves as outside (read-only)."""

    user = Path.home()
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or user / ".config")
    found = [user / ".gitconfig", xdg / "git"]
    return [_real(path) for path in found if path.exists()]


def resolve_program(argv: list[str], env: dict[str, str]) -> list[str]:
    """argv with an absolute program path: the container is started by path, not by PATH search."""

    if not argv:
        raise PolicyError("no command to run")
    program = argv[0]
    if not os.path.isabs(program):
        path = next((value for key, value in env.items() if key.upper() == "PATH"), None)
        found = shutil.which(program, path=path)
        if not found:
            raise PolicyError(f"could not find {program} for the sandbox")
        program = found
    return [str(Path(program)), *argv[1:]]




def command_policy(workspaces: list[str], env: dict[str, str]) -> Policy:
    """Least-privilege base policy; command containers never get agent state."""

    from cli_subagents.agy_review.paths import HOME_SECRETS, secret_reason

    roots = [check_workspace(root) for root in workspaces]
    denied = [*private_roots(), *(str(Path.home() / name) for name in HOME_SECRETS),
              str(Path.home() / ".cargo" / "credentials"), str(Path.home() / ".cargo" / "credentials.toml")]
    seen = 0
    for root in roots:
        for folder, dirs, files in os.walk(root, followlinks=False):
            # Don't follow junctions or read agent/git settings to discover secrets.
            for name in list(dirs):
                path = Path(folder) / name
                if name in PROTECTED_NAMES or not _inside(_real(path), root):
                    dirs.remove(name)
                    if name != ".git":
                        denied.append(str(path))
            for name in files:
                seen += 1
                if seen > 250_000:
                    raise PolicyError("workspace has too many files to check for secrets; use a narrower workspace")
                path = str(Path(folder) / name)
                if secret_reason(path, [str(Path.home())], trusted=root):
                    denied.append(path)
    return Policy(roots, readonly=_unique([*toolchain_dirs(env), *python_dirs(), *git_config_paths(),
                                          *enclosing_git_dir(roots[0])]),
                  denied=_unique(denied), pinned=host_paths([]), protected_names=list(PROTECTED_NAMES))
