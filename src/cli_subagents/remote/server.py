#!/usr/bin/env python3
"""Run one Claude Code job for an SSH user on one of that user's own accounts.

Started by sshd as the forced command of the user's key (see docs/remote.md):

    csa remote serve --user NAME [--config PATH]    reads SSH_ORIGINAL_COMMAND
    csa remote serve --cleanup [--config PATH]      deletes old transcripts (timer)

`python -m cli_subagents.remote.server` takes the same options.

SSH_ORIGINAL_COMMAND (what the client asked for):
    job (or empty)      run one job over stdin/stdout, NDJSON
    login ACCOUNT       interactive Claude Code in that account's folder, for /login (ssh -t)
    status              each of the user's accounts with its usage

The code chooses an account folder and passes flags. It never opens login files:
Claude Code reads its own login from the account folder.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cli_subagents.remote import protocol  # noqa: E402
from cli_subagents.remote.protocol import ProtocolError  # noqa: E402

DEFAULT_CONFIG = "/etc/cli-subagents/remote.toml"
# Built-in tools that would act on the server's disk. A job works on the
# user's PC through the pc_* tools instead, and a job without a project has no
# file tools at all.
SERVER_FILE_TOOLS = "Bash,Read,Write,Edit,MultiEdit,Glob,Grep,NotebookEdit,LS,Monitor,PowerShell,LSP"
# The only built-in tools a job gets. Everything that touches files or runs
# commands goes through the PC tools instead; ToolSearch loads MCP tools.
ALLOWED_BUILTIN_TOOLS = "WebSearch,WebFetch,TodoWrite,ToolSearch"
JOB_IDLE_CLOSE_SECONDS = 10
MAX_JOBS_PER_ACCOUNT = 2
USAGE_CACHE_SECONDS = 120
FULL_PERCENT = 95
LIMIT_PAUSE_SECONDS = 1800
CLEANUP_EVERY_SECONDS = 3600
# Folders inside an account where Claude Code keeps conversation content.
TRANSCRIPT_DIRS = ("projects", "todos", "file-history", "shell-snapshots", "session-env", "debug", "paste-cache", "plans")
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
LIMIT_TEXT = re.compile(r"usage limit|rate limit|limit reached|limit resets|out of extra usage", re.IGNORECASE)
PERCENT_LINE = re.compile(r"^\s*Current (session|week \(all models\))\s*:\s*(\d{1,3})%\s*used", re.IGNORECASE | re.MULTILINE)

PC_NOTE = (
    "You are running on a remote server for this job. The user's project folder is on their PC, "
    "not on this server. Use the mcp__pc__ tools (pc_read, pc_write, pc_edit, pc_list, pc_glob, pc_grep, "
    "pc_run) for every file and command in the project; paths are relative to the project folder. "
    "pc_run runs on the user's PC, so use it for builds and tests."
)
NO_PROJECT_NOTE = (
    "You are running on a remote server for this job, without a project folder. "
    "You have no file or shell tools; answer from the conversation and the other tools you have."
)


# --------------------------------------------------------------------------
# Config and state
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Account:
    name: str
    owner: str
    home: Path


@dataclass(frozen=True)
class Tier:
    """What one group of people may use: models, jobs at once, job length, weekly cost."""

    name: str
    models: tuple[str, ...] = ()  # empty: any model
    default_model: str | None = None
    max_jobs: int | None = None  # jobs at once across the person's accounts
    max_job_minutes: int | None = None
    weekly_cost_usd: float | None = None  # list-price cost per ISO week, from the usage log


@dataclass(frozen=True)
class Config:
    state_dir: Path
    claude: tuple[str, ...]
    transcript_days: int
    accounts: tuple[Account, ...]
    max_job_minutes: int = 180
    tiers: dict = field(default_factory=dict)  # name -> Tier
    users: dict = field(default_factory=dict)  # person -> tier name


class ServerError(Exception):
    """An error with a wire-format error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def load_config(path: Path) -> Config:
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
    server = data.get("server") or {}
    state_dir = Path(server.get("state_dir") or "/var/lib/cli-subagents-remote")
    claude = server.get("claude") or "claude"
    claude_cmd = tuple(claude) if isinstance(claude, list) else tuple(shlex.split(str(claude)))
    if not claude_cmd or not all(isinstance(part, str) and part for part in claude_cmd):
        raise ValueError("server.claude must be a command")
    days = server.get("transcript_days", 7)
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 90:
        raise ValueError("server.transcript_days must be 1 to 90")
    accounts: list[Account] = []
    seen: set[str] = set()
    for entry in data.get("account") or []:
        name, owner, home = entry.get("name"), entry.get("owner"), entry.get("home")
        if not isinstance(name, str) or not NAME.match(name):
            raise ValueError(f"account name {name!r} must be lowercase letters, digits, - or _")
        if not isinstance(owner, str) or not NAME.match(owner):
            raise ValueError(f"account {name}: owner must be a user name")
        if not isinstance(home, str) or not Path(home).is_absolute():
            raise ValueError(f"account {name}: home must be an absolute path")
        if name in seen:
            raise ValueError(f"account {name} is listed twice")
        seen.add(name)
        accounts.append(Account(name, owner, Path(home)))
    minutes = server.get("max_job_minutes", 180)
    if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= 1440:
        raise ValueError("server.max_job_minutes must be 1 to 1440")
    tiers: dict[str, Tier] = {}
    for tier_name, entry in (data.get("tier") or {}).items():
        if not NAME.match(tier_name) or not isinstance(entry, dict):
            raise ValueError(f"tier {tier_name!r} must be a [tier.<name>] table with a lowercase name")
        models = entry.get("models") or []
        if not isinstance(models, list) or not all(isinstance(m, str) and m for m in models):
            raise ValueError(f"tier {tier_name}: models must be a list of model names")
        default = entry.get("default_model")
        if default is not None and (not isinstance(default, str) or (models and default not in models)):
            raise ValueError(f"tier {tier_name}: default_model must be one of its models")
        numbers = {}
        for key, low, high in (("max_jobs", 1, 50), ("max_job_minutes", 1, 1440)):
            value = entry.get(key)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high):
                raise ValueError(f"tier {tier_name}: {key} must be {low} to {high}")
            numbers[key] = value
        cap = entry.get("weekly_cost_usd")
        if cap is not None and (isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0):
            raise ValueError(f"tier {tier_name}: weekly_cost_usd must be a positive number")
        tiers[tier_name] = Tier(tier_name, tuple(models), default, numbers["max_jobs"], numbers["max_job_minutes"],
                                float(cap) if cap is not None else None)
    users = data.get("users") or {}
    if not isinstance(users, dict):
        raise ValueError("[users] must map each person to a tier")
    for person, tier_name in users.items():
        if not NAME.match(person) or tier_name not in tiers:
            raise ValueError(f"[users] {person!r} must name a defined tier")
    return Config(state_dir, claude_cmd, days, tuple(accounts), minutes, tiers, dict(users))


def tier_for(config: Config, user: str) -> Tier | None:
    """The person's tier, or the tier named "default", or None (no extra limits)."""
    name = config.users.get(user, "default")
    return config.tiers.get(name)


def week_cost(folder: Path) -> float:
    """List-price cost of the person's jobs in the current ISO week, from their usage log."""
    year, week, _ = datetime.date.today().isocalendar()
    current = f"{year}-W{week:02d}"
    total = 0.0
    try:
        lines = (folder / "usage.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0.0
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("week") == current:
            total += float(entry.get("cost_usd") or 0.0)
    return total


def apply_tier(job: dict, tier: Tier | None, folder: Path) -> dict:
    """Check the job against the person's tier and fill in its model; raise ServerError when refused."""
    if tier is None:
        return job
    model = job.get("model") or tier.default_model or (tier.models[0] if tier.models else None)
    if tier.models and model not in tier.models:
        raise ServerError("model_not_allowed", f"model {model} is not allowed for you; allowed: {', '.join(tier.models)}")
    if tier.weekly_cost_usd is not None and week_cost(folder) >= tier.weekly_cost_usd:
        raise ServerError("cap_reached", f"you reached your weekly cap of ${tier.weekly_cost_usd:.0f}; it resets on Monday")
    return {**job, "model": model}


def user_dir(config: Config, user: str) -> Path:
    """Everything the server keeps for one person: notes, jobs, bundles, work folders, usage."""
    return _private_dir(config.state_dir / "users" / user)


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


class FileLock:
    """A lock file created with O_EXCL; stale locks older than a minute are broken."""

    def __init__(self, path: Path, timeout: float = 30.0) -> None:
        self.path = path
        self.timeout = timeout

    def __enter__(self) -> "FileLock":
        deadline = time.monotonic() + self.timeout
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > 60:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise ServerError("internal", "server state is locked; try again") from None
                time.sleep(0.05)

    def __exit__(self, *exc) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


class State:
    """Per-account notes: when it was last used, its last usage reading, and a pause after a limit."""

    def __init__(self, folder: Path) -> None:
        self.path = folder / "accounts.json"
        self.lock = FileLock(folder / "accounts.lock")

    def read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def update(self, account: str, **fields) -> None:
        with self.lock:
            data = self.read()
            entry = data.get(account) if isinstance(data.get(account), dict) else {}
            entry.update(fields)
            data[account] = entry
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.path)


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        return True  # os.kill(pid, 0) would send CTRL_C_EVENT on Windows; stale files expire instead
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def running_jobs(folder_root: Path, account: str) -> int:
    folder = folder_root / "running" / account
    count = 0
    for marker in folder.glob("*") if folder.is_dir() else []:
        try:
            pid = int(marker.read_text(encoding="utf-8").strip() or "0")
            fresh = time.time() - marker.stat().st_mtime < 12 * 3600
        except (OSError, ValueError):
            continue
        if pid and fresh and _pid_alive(pid):
            count += 1
        else:
            try:
                marker.unlink()
            except OSError:
                pass
    return count


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------


def claude_env(account: Account) -> dict[str, str]:
    """The environment for Claude Code on one account: its folder, and no login override."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("ANTHROPIC_")
        and key not in {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "AWS_BEARER_TOKEN_BEDROCK", "SSH_ORIGINAL_COMMAND"}
    }
    env["CLAUDE_CONFIG_DIR"] = str(account.home)
    env["DISABLE_AUTOUPDATER"] = "1"
    env["CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY"] = "1"
    return env


def parse_usage(text: str) -> int | None:
    """The highest 'N% used' of the session and weekly lines of `claude -p /usage`, or None."""
    values = [int(match.group(2)) for match in PERCENT_LINE.finditer(text)]
    return max(values) if values else None


def read_usage(config: Config, state: State, account: Account) -> int | None:
    """Percent used of the account's tightest window; cached for a short while.

    `claude -p /usage` is answered locally by Claude Code from its own account
    data; no model call is made.
    """
    cached = state.read().get(account.name, {}).get("usage")
    if isinstance(cached, dict) and time.time() - cached.get("at", 0) < USAGE_CACHE_SECONDS:
        return cached.get("percent")
    try:
        done = subprocess.run(
            [*config.claude, "-p", "/usage"],
            env=claude_env(account),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        percent = parse_usage(done.stdout)
    except (OSError, subprocess.TimeoutExpired):
        percent = None
    state.update(account.name, usage={"percent": percent, "at": time.time()})
    return percent


def owned_accounts(config: Config, user: str) -> list[Account]:
    return [account for account in config.accounts if account.owner == user]


def check_folder_owner(account: Account) -> None:
    """Refuse an account folder that this server user doesn't own.

    Each person has their own Unix user on the server and their account folders
    belong to it, so one person's job can't read another person's account.
    """
    if os.name == "nt":
        return
    try:
        info = account.home.lstat()
    except FileNotFoundError:
        return  # created on first login, by this user
    if info.st_uid != os.getuid() or (info.st_mode & 0o077):
        raise ServerError("internal", f"account folder for {account.name} must belong to this user with mode 0700")


def pick_account(config: Config, state: State, user: str) -> Account:
    """The user's account with the most room; a new job only, never mid-job."""
    accounts = owned_accounts(config, user)
    if not accounts:
        raise ServerError("no_account", f"no Claude account on this server belongs to {user}")
    notes = state.read()
    now = time.time()
    choices: list[tuple[int, float, Account]] = []
    busy = 0
    for account in accounts:
        note = notes.get(account.name, {}) if isinstance(notes.get(account.name), dict) else {}
        if note.get("limited_until", 0) > now:
            continue
        if running_jobs(user_dir(config, user), account.name) >= MAX_JOBS_PER_ACCOUNT:
            busy += 1
            continue
        percent = read_usage(config, state, account)
        if percent is not None and percent >= FULL_PERCENT:
            continue
        # Unknown usage sorts after known readings but before a full account.
        choices.append((percent if percent is not None else 50, note.get("last_used", 0), account))
    if not choices:
        if busy:
            raise ServerError("session_account_busy", "every account with room is running its maximum of jobs; try again shortly")
        raise ServerError("all_limited", "every one of your accounts is at its usage limit; try again after a reset")
    choices.sort(key=lambda item: (item[0], item[1], item[2].name))
    return choices[0][2]


def named_account(config: Config, state: State, user: str, name: str, *, conversation: bool = False) -> Account:
    """The account a job names: one of the sender's own, not paused after a limit, with a free slot.

    The client chose it (csa picks by its own usage records); the server only
    checks that it may run there. It never substitutes another account.
    """
    for account in owned_accounts(config, user):
        if account.name == name:
            note = state.read().get(name, {})
            if isinstance(note, dict) and note.get("limited_until", 0) > time.time():
                stays = "; this conversation stays on it" if conversation else ""
                raise ServerError("all_limited", f"account {name} is at its usage limit{stays}, try again after the reset")
            if running_jobs(user_dir(config, user), name) >= MAX_JOBS_PER_ACCOUNT:
                raise ServerError("session_account_busy", f"account {name} is running its maximum of jobs; try again shortly")
            return account
    raise ServerError("account_not_owned", f"account {name} is not one of your accounts")


def resume_account(config: Config, state: State, user: str, name: str) -> Account:
    """The account a session ran on. A resume stays on it, even when it is near its limit."""
    return named_account(config, state, user, name, conversation=True)


# --------------------------------------------------------------------------
# Transcript retention
# --------------------------------------------------------------------------


def cleanup(config: Config, now: float | None = None, user: str | None = None) -> int:
    """Delete conversation files older than transcript_days in every account; return how many.

    Only files under the account folders' transcript folders and the server's
    own job folders are removed. Links are removed as links, never followed.
    """
    now = time.time() if now is None else now
    cutoff = now - config.transcript_days * 86400
    accounts = owned_accounts(config, user) if user else list(config.accounts)
    roots = [account.home / name for account in accounts for name in TRANSCRIPT_DIRS]
    users = config.state_dir / "users"
    if user:
        roots.append(users / user / "jobs")
    elif users.is_dir():
        roots += [folder / "jobs" for folder in users.iterdir()]
    if hasattr(os, "fwalk"):
        return sum(_cleanup_fd(root, cutoff) for root in roots)
    removed = 0
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            continue
        for current, dirs, files in os.walk(root, topdown=False, followlinks=False):
            folder = Path(current)
            for name in files:
                path = folder / name
                try:
                    if path.lstat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1
                except OSError:
                    pass
            for name in dirs:
                path = folder / name
                try:
                    if path.is_symlink():
                        if path.lstat().st_mtime < cutoff:
                            path.unlink()
                            removed += 1
                    elif not any(path.iterdir()):
                        path.rmdir()
                except OSError:
                    pass
    return removed


def _cleanup_fd(root: Path, cutoff: float) -> int:
    """POSIX cleanup through open folder handles, so a folder swapped for a link mid-walk is never followed."""
    try:
        if root.is_symlink() or not root.is_dir():
            return 0
    except OSError:
        return 0
    removed = 0
    for _folder, dirs, files, dir_fd in os.fwalk(root, topdown=False, follow_symlinks=False):
        for name in files:
            try:
                if os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mtime < cutoff:
                    os.unlink(name, dir_fd=dir_fd)
                    removed += 1
            except OSError:
                pass
        for name in dirs:
            try:
                os.rmdir(name, dir_fd=dir_fd)  # only empty folders go
            except OSError:
                pass
    return removed


def cleanup_if_due(config: Config, user: str) -> None:
    marker = user_dir(config, user) / "last-cleanup"
    try:
        if time.time() - marker.stat().st_mtime < CLEANUP_EVERY_SECONDS:
            return
    except OSError:
        pass
    marker.write_text(str(int(time.time())), encoding="utf-8")
    cleanup(config, user=user)


# --------------------------------------------------------------------------
# One job
# --------------------------------------------------------------------------


class Out:
    """Thread-safe writer for wire-format lines."""

    def __init__(self, stream: IO[bytes]) -> None:
        self.stream = stream
        self.lock = threading.Lock()

    def send(self, message: dict) -> None:
        with self.lock:
            try:
                self.stream.write(protocol.encode(message))
                self.stream.flush()
            except (OSError, ValueError):
                pass


def work_dir(config: Config, user: str, workspace: dict) -> Path:
    """A stable folder per user and project, so a resume finds its session."""
    base = config.state_dir / "users" / user / "work"
    if workspace["mode"] != "pc":
        return _private_dir(base / "_none")
    label = workspace["label"]
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", label)[:40] or "project"
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:8]
    return _private_dir(base / f"{slug}-{digest}")


def session_file(account: Account, cwd: Path, session_id: str) -> Path:
    return account.home / "projects" / protocol.project_dir_name(str(cwd)) / f"{session_id}.jsonl"


def install_setup(config: Config, user: str, job: dict, messages, out: Out):
    """The user's setup bundle for this job, asking the PC for it when the server doesn't have it."""
    spec = job.get("bundle")
    if not spec:
        return None
    from cli_subagents.remote import bundle as bundle_mod

    sha = spec["sha256"]
    root = config.state_dir / "users" / user / "bundles"
    folder = root / sha
    if folder.is_dir():
        try:
            return bundle_mod.load_installed(folder)
        except ValueError:
            pass
    out.send({"type": "need_bundle", "sha256": sha})
    reply = next(messages, None)
    if not reply or reply.get("type") != "bundle" or reply.get("sha256") != sha:
        raise ServerError("bad_request", "expected the setup bundle")
    try:
        data = base64.b64decode(reply.get("data") or "", validate=True)
        return bundle_mod.install_bundle(data, sha, _private_dir(root))
    except ValueError as exc:
        raise ServerError("bad_request", f"setup bundle rejected: {exc}") from None


def build_mcp_config(job: dict, installed, path: Path) -> None:
    servers: dict = {}
    if installed is not None and installed.mcp_config_path and Path(installed.mcp_config_path).is_file():
        data = json.loads(Path(installed.mcp_config_path).read_text(encoding="utf-8"))
        servers.update((data or {}).get("mcpServers") or {})
    workspace = job["workspace"]
    proxies = workspace.get("proxies") or []
    # The PC's servers win over bundle servers of the same name.
    for name in ("pc", *proxies):
        servers.pop(name, None)
    if workspace["mode"] == "pc":
        headers = {"Authorization": f"Bearer {workspace['bridge_token']}"}
        servers["pc"] = {"type": "http", "url": f"http://127.0.0.1:{workspace['bridge_port']}/mcp", "headers": headers}
        # Stdio servers that stay on the PC (csa-parent and the like), through the same bridge.
        for name in proxies:
            servers[name] = {"type": "http", "url": f"http://127.0.0.1:{workspace['bridge_port']}/mcp/{name}",
                             "headers": dict(headers)}
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"mcpServers": servers}, handle)


def claude_argv(config: Config, job: dict, installed, mcp_path: Path, session: tuple[str, str]) -> list[str]:
    workspace_pc = job["workspace"]["mode"] == "pc"
    argv = [
        *config.claude,
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", job["permission_mode"],
        "--mcp-config", str(mcp_path),
        "--strict-mcp-config",
        "--tools", ALLOWED_BUILTIN_TOOLS,
        "--disallowedTools", SERVER_FILE_TOOLS,
        "--append-system-prompt", PC_NOTE if workspace_pc else NO_PROJECT_NOTE,
    ]
    if workspace_pc:
        argv += ["--allowedTools", ",".join(["mcp__pc", *(f"mcp__{name}" for name in job["workspace"].get("proxies") or [])])]
    if installed is not None:
        if installed.settings_path and Path(installed.settings_path).is_file():
            argv += ["--settings", str(installed.settings_path)]
        for directory in installed.plugin_dirs or []:
            argv += ["--plugin-dir", str(directory)]
    if job.get("model"):
        argv += ["--model", job["model"]]
    if job.get("effort"):
        argv += ["--effort", job["effort"]]
    if job.get("max_turns"):
        argv += ["--max-turns", str(job["max_turns"])]
    kind, session_id = session
    argv += ["--resume", session_id] if kind == "resume" else ["--session-id", session_id]
    return argv


def first_prompt(job: dict, resumed: bool) -> str:
    resume = job.get("resume")
    if resume and not resumed and resume.get("history_md"):
        return (
            "Earlier conversation in this thread, from the user's copy (the server's copy has expired):\n\n"
            f"{resume['history_md']}\n\n---\n\nNew message:\n{job['prompt']}"
        )
    return job["prompt"]


def kill_tree(proc: subprocess.Popen) -> None:
    """Stop Claude Code and everything it started (its MCP servers), even after it exited."""
    if os.name != "nt":
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            try:
                proc.wait(timeout=5)
                time.sleep(0.2)
            except subprocess.TimeoutExpired:
                continue
        return
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.terminate()
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "nt":
                proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass


def log_usage(config: Config, user: str, account: Account, model: str | None, result: dict | None) -> None:
    """Append one line without prompt or reply text, dated only by ISO week."""
    usage = (result or {}).get("usage") or {}
    year, week, _ = datetime.date.today().isocalendar()
    line = {
        "week": f"{year}-W{week:02d}",
        "user": user,
        "account": account.name,
        "model": model,
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cost_usd": float((result or {}).get("total_cost_usd") or 0.0),
        "is_error": bool((result or {}).get("is_error", result is None)),
    }
    path = user_dir(config, user) / "usage.jsonl"
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def _read_sessions(folder: Path) -> dict:
    try:
        data = json.loads((folder / "sessions.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember_session(folder: Path, session_id: str, account: str) -> None:
    """Record which account a conversation runs on (no content), so a resume can't move it."""
    with FileLock(folder / "sessions.lock"):
        data = _read_sessions(folder)
        data[session_id] = {"account": account, "at": int(time.time())}
        cutoff = time.time() - 400 * 86400
        data = {key: value for key, value in data.items() if isinstance(value, dict) and value.get("at", 0) > cutoff}
        tmp = folder / "sessions.tmp"
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, folder / "sessions.json")


def run_job(config: Config, user: str, stdin: IO[bytes], stdout: IO[bytes]) -> int:
    out = Out(stdout)
    messages = protocol.read_messages(stdin)
    folder = user_dir(config, user)
    state = State(folder)
    try:
        cleanup_if_due(config, user)
    except OSError:
        pass
    marker: Path | None = None
    try:
        first = next(messages, None)
        if first is None:
            raise ServerError("bad_request", "no job received")
        job = protocol.validate_job(first)
        resume = job.get("resume")
        if resume:
            known = _read_sessions(folder).get(resume["session_id"])
            if not isinstance(known, dict):
                raise ServerError("bad_request", "this server doesn't know that conversation; start a new one")
            if known.get("account") != resume["account"] or job["account"] not in (None, resume["account"]):
                raise ServerError("account_not_owned", "that conversation runs on another account and stays there")
        tier = tier_for(config, user)
        job = apply_tier(job, tier, folder)
        # Choosing the account and claiming its slot happen under one lock, so
        # two jobs started together can't both take the last slot.
        with FileLock(folder / "start.lock"):
            if tier is not None and tier.max_jobs is not None:
                running = sum(running_jobs(folder, a.name) for a in owned_accounts(config, user))
                if running >= tier.max_jobs:
                    raise ServerError("session_account_busy", f"you already run {running} job(s), your limit is {tier.max_jobs}")
            if resume:
                account = resume_account(config, state, user, resume["account"])
            elif job["account"]:
                account = named_account(config, state, user, job["account"])
            else:
                account = pick_account(config, state, user)
            check_folder_owner(account)
            job_key = str(uuid.uuid4())
            marker = _private_dir(folder / "running" / account.name) / job_key
            marker.write_text(str(os.getpid()), encoding="utf-8")
        installed = install_setup(config, user, job, messages, out)
    except (ProtocolError, ServerError) as exc:
        if marker is not None:
            marker.unlink(missing_ok=True)
        code = "bad_request" if isinstance(exc, ProtocolError) else exc.code
        out.send({"type": "error", "code": code, "message": str(exc)})
        return 2 if code == "bad_request" else 1

    if installed is not None:
        from cli_subagents.remote import bundle as bundle_mod

        for warning in bundle_mod.apply_to_account(installed, account.home) or []:
            out.send({"type": "warning", "text": warning})

    cwd = work_dir(config, user, job["workspace"])
    resumed = bool(resume) and session_file(account, cwd, resume["session_id"]).is_file()
    session = ("resume", resume["session_id"]) if resumed else ("new", str(uuid.uuid4()))
    job_dir = _private_dir(folder / "jobs" / job_key)
    mcp_path = job_dir / "mcp.json"
    build_mcp_config(job, installed, mcp_path)
    argv = claude_argv(config, job, installed, mcp_path, session)

    state.update(account.name, last_used=time.time())
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=claude_env(account),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=(os.name != "nt"),
        )
    except OSError as exc:
        _remove_tree(job_dir)
        marker.unlink(missing_ok=True)
        out.send({"type": "error", "code": "claude_failed", "message": f"could not start Claude Code: {type(exc).__name__}"})
        return 1
    marker.write_text(str(proc.pid), encoding="utf-8")
    _remember_session(folder, session[1], account.name)
    out.send({"type": "accepted", "job_id": job["job_id"], "account": account.name,
              "session_id": session[1], "resumed": resumed})

    feed = threading.Lock()
    pending = [1]
    open_input = [True]
    finished = threading.Event()
    last_event = [time.monotonic(), False]  # time of the last event, and whether it was a result

    def write_user(text: str) -> bool:
        with feed:
            if not open_input[0]:
                return False
            try:
                proc.stdin.write(protocol.encode(protocol.user_message(text)))
                proc.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def close_input() -> None:
        with feed:
            if open_input[0]:
                open_input[0] = False
                try:
                    proc.stdin.close()
                except OSError:
                    pass

    def client_reader() -> None:
        try:
            for message in messages:
                kind = message.get("type")
                if kind == "message" and isinstance(message.get("text"), str) and message["text"].strip():
                    with feed:
                        pending[0] += 1
                    delivered = write_user(message["text"])
                    if not delivered:
                        with feed:
                            pending[0] -= 1
                        out.send({"type": "warning", "text": "the job had already finished; message not delivered"})
                    message_id = protocol.message_id(message)
                    if message_id:
                        # The PC marks a follow-up read only when it went in.
                        out.send({"type": "message_status", "id": message_id, "delivered": delivered})
                elif kind == "cancel":
                    kill_tree(proc)
                    return
        except ProtocolError:
            pass
        # The SSH session closed before the job ended: nobody is left to read
        # the answer and the PC tools are gone, so stop the job.
        if not finished.is_set():
            kill_tree(proc)

    def watchdog() -> None:
        minutes = min(config.max_job_minutes, (tier.max_job_minutes if tier and tier.max_job_minutes else config.max_job_minutes))
        deadline = time.monotonic() + minutes * 60
        while not finished.wait(1):
            if time.monotonic() > deadline:
                out.send({"type": "warning", "text": f"stopped after {minutes} minutes"})
                kill_tree(proc)
                return
            # Claude Code may answer two quick messages in one turn. When the
            # last event was a result and nothing followed, end the input.
            if last_event[1] and time.monotonic() - last_event[0] > JOB_IDLE_CLOSE_SECONDS:
                close_input()

    stderr_tail: list[bytes] = []

    def stderr_reader() -> None:
        for chunk in iter(lambda: proc.stderr.read(4096), b""):
            stderr_tail.append(chunk)
            del stderr_tail[:-4]

    # The task goes in first; only then are follow-up messages forwarded, so a
    # quick follow-up can't overtake the task itself.
    write_user(first_prompt(job, resumed))
    threading.Thread(target=client_reader, daemon=True).start()
    threading.Thread(target=stderr_reader, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()

    model = job.get("model")
    result: dict | None = None
    session_id = session[1]
    try:
        for raw in iter(proc.stdout.readline, b""):
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            last_event[0], last_event[1] = time.monotonic(), event.get("type") == "result"
            if event.get("type") == "system" and event.get("subtype") == "init":
                model = event.get("model") or model
                session_id = event.get("session_id") or session_id
            out.send({"type": "event", "event": event})
            if event.get("type") == "result":
                result = event
                session_id = event.get("session_id") or session_id
                with feed:
                    pending[0] -= 1
                    done_all = pending[0] <= 0
                if done_all:
                    close_input()
        proc.wait()
    finally:
        finished.set()
        close_input()
        kill_tree(proc)
        marker.unlink(missing_ok=True)
        # The job folder holds the PC tools' token and the user's MCP settings.
        _remove_tree(job_dir)

    if session_id != session[1]:
        _remember_session(folder, session_id, account.name)
    if result is not None and result.get("is_error") and LIMIT_TEXT.search(str(result.get("result") or "")):
        state.update(account.name, limited_until=time.time() + LIMIT_PAUSE_SECONDS)
    try:
        log_usage(config, user, account, model, result)
    except OSError:
        pass

    if result is None:
        tail = b"".join(stderr_tail).decode("utf-8", "replace").strip().splitlines()[-3:]
        out.send({"type": "error", "code": "claude_failed",
                  "message": "Claude Code ended without a result" + (": " + " / ".join(tail) if tail else "")})
        return 1
    out.send({
        "type": "done",
        "job_id": job["job_id"],
        "exit_code": proc.returncode,
        "is_error": bool(result.get("is_error")),
        "result": result.get("result"),
        "session_id": session_id,
        "account": account.name,
        "usage": result.get("usage") or {},
        "cost_usd": result.get("total_cost_usd"),
    })
    return 0


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------
# Other commands
# --------------------------------------------------------------------------


def run_login(config: Config, user: str, name: str) -> int:
    for account in owned_accounts(config, user):
        if account.name == name:
            _private_dir(account.home)
            check_folder_owner(account)
            print(f"Claude Code for account {name}. Type /login and finish the sign-in in your browser, then /exit.")
            # No tools and no MCP servers: this session is only for signing in.
            return subprocess.call([*config.claude, "--tools", "", "--strict-mcp-config"],
                                   env=claude_env(account), cwd=account.home)
    print(f"error: account {name} is not one of your accounts", file=sys.stderr)
    return 1


def read_login(config: Config, account: Account) -> bool | None:
    """Whether Claude Code says the account is logged in (`claude auth status --json`, no model call).

    Claude Code reads its own login; this code only reads its yes or no.
    """
    try:
        done = subprocess.run([*config.claude, "auth", "status", "--json"], env=claude_env(account), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60, stdin=subprocess.DEVNULL)
        data = json.loads(done.stdout[done.stdout.index("{"):])
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return bool(data.get("loggedIn")) if isinstance(data, dict) else None


def run_status(config: Config, user: str, as_json: bool = False) -> int:
    state = State(user_dir(config, user))
    accounts = owned_accounts(config, user)
    if as_json:
        report = []
        for account in accounts:
            report.append({"name": account.name, "logged_in": read_login(config, account),
                           "usage_percent": read_usage(config, state, account),
                           "running": running_jobs(user_dir(config, user), account.name)})
        print(json.dumps({"user": user, "accounts": report}))
        return 0
    if not accounts:
        print(f"no Claude account on this server belongs to {user}")
        return 1
    for account in accounts:
        percent = read_usage(config, state, account)
        shown = "unknown (not logged in?)" if percent is None else f"{percent}% of the tightest window used"
        print(f"{account.name}: {shown}, {running_jobs(user_dir(config, user), account.name)} job(s) running")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Claude Code jobs for an SSH user on their own accounts.")
    parser.add_argument("--user", help="the person this SSH key belongs to")
    parser.add_argument("--config", default=os.environ.get("CSA_REMOTE_CONFIG", DEFAULT_CONFIG))
    parser.add_argument("--cleanup", action="store_true", help="delete transcripts older than transcript_days and exit")
    parser.add_argument("command", nargs="*", help="for local testing; over SSH the client's command is used")
    args = parser.parse_args(argv)
    try:
        config = load_config(Path(args.config))
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"error: cannot load {args.config}: {exc}", file=sys.stderr)
        return 2
    if args.cleanup:
        print(f"removed {cleanup(config)} old file(s)")
        return 0
    if not args.user or not NAME.match(args.user):
        print("error: --user must name the person this key belongs to", file=sys.stderr)
        return 2
    words = shlex.split(os.environ.get("SSH_ORIGINAL_COMMAND", "")) or args.command or ["job"]
    if words[0] == "job" and len(words) == 1:
        return run_job(config, args.user, sys.stdin.buffer, sys.stdout.buffer)
    if words[0] == "login" and len(words) == 2:
        return run_login(config, args.user, words[1])
    if words[0] == "status" and words[1:] in ([], ["--json"]):
        return run_status(config, args.user, as_json=words[1:] == ["--json"])
    print("error: allowed commands are: job, login ACCOUNT, status [--json]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
