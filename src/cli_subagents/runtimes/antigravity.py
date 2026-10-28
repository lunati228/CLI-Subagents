"""Antigravity adapter: `agy -p --output-format stream-json` workers (Gemini and other models).

Every worker runs in a folder this adapter manages, used as its HOME
(USERPROFILE on Windows, the XDG folders too on macOS and Linux for extra
accounts). AGY has no setting to move its config folder, so this is the only way
to give a worker its own hooks.json, settings.json and mcp_config.json without
touching the user's ~/.gemini. The shared setup (rules, skills, plugins,
instructions) is linked from the primary ~/.gemini.

* The primary account runs in `homes/antigravity/primary`. agy keeps its login in
  the OS keyring, which it finds from any HOME (verified on Windows with agy 1.3.6).
  Where the primary's login is a token file in the real home (agy does that when it
  has no keyring, typical on Linux servers), the worker can't use another folder:
  it then runs in the real home without the review gate and without the parent
  channel, as before. `csa launch` always uses the real home.
* Extra accounts are "use at your own risk": Google's FAQ says consumer plans don't
  support automating account rotation. Rotation is on by default, as for the other
  runtimes; failover = "off" keeps unpinned jobs on the primary. Each gets its own folder plus
  SSH_CONNECTION, SSH_CLIENT and SSH_TTY; in an SSH-style session agy stores its
  login in a file under that HOME instead of the keyring (undocumented agy
  behaviour), so each folder holds its own login, written by agy itself.

Read and edit jobs run behind the review gate (`cli_subagents.agy_review`): a
PreToolUse hook in the folder's config/hooks.json answers every tool call with
auto_approve or deny, from fixed rules or a tool-less reviewer call. Edit jobs
also run with `--sandbox`, so commands are confined to the workspace and temp
folders without network unless the reviewer lets one out. Full jobs skip both.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from cli_subagents import agy_review, mcp_translate, plans, sync
from cli_subagents.agy_review import store as review_store
from cli_subagents.config import Account, RuntimeConfig, home as csa_home, homes_dir, jobs_dir, state_dir
from cli_subagents.fsutil import (
    copy_exclusive,
    file_lock,
    is_link,
    link_target,
    make_private_dir,
    no_window_kwargs,
    python_executable,
    read_json,
    remove_link,
    same_path,
    write_text,
)
from cli_subagents.hosts import parent_note
from cli_subagents.runtimes.base import (
    AUTH,
    ERROR,
    PARENT_SERVER,
    SUCCESS,
    USAGE_LIMIT,
    WORKER_TOOLS_NOTE,
    AttemptResult,
    JobSpec,
    Launch,
    clean_environment,
    parent_server,
)
from cli_subagents.runtimes.reviewers import (  # noqa: F401
    AGY_REVIEW_BUDGET, AGY_REVIEWER_MODEL as REVIEWER_MODEL, CLAUDE_REVIEWER_MODEL, REVIEW_TIMEOUT, build_chain,
)

MAX_PROMPT_CHARS = 24_000  # the prompt travels as one command-line argument

# agy rejects --effort for third-party models ("--effort is not supported for model ...", checked with
# claude-sonnet-4-6 in agy 1.4.0), and ids like gemini-3.8-flash-high or gpt-oss-120b-medium carry their level.
EFFORT_SUFFIX_RE = re.compile(r"-(minimal|low|medium|high|xhigh|max)$")


def takes_effort(model: str | None) -> bool:
    """Whether agy accepts --effort next to this model id (unset = agy's default model, which does)."""

    if not model:
        return True
    return not (model.startswith(("claude-", "gpt-")) or EFFORT_SUFFIX_RE.search(model))
MODES = {"read": "plan", "full": "accept-edits"}
VALID_MODES = ("plan", "accept-edits", "auto")
GATED_ACCESS = ("read", "edit")
PRIMARY_FOLDER = "primary"  # homes/antigravity/primary; "primary" can't be an account name
REVIEWER_FOLDER = ".reviewer"  # homes/antigravity/.reviewer/<account>; account names can't start with a dot
# REVIEWER_MODEL, CLAUDE_REVIEWER_MODEL, REVIEW_TIMEOUT and the chain's time budget live in reviewers.py.

WORKER_NOTE = (
    "[You are running as a delegated subagent for another agent session, which will read your "
    "final message. Work only on this task, stay inside its scope, and end with a short summary "
    "of what you did, the result, and anything left open.]\n\n"
)
GATE_NOTE = (
    " An automatic reviewer approves or denies each of your tool calls. If one is denied, the reason "
    "comes back to you: don't repeat it, find another way or say in your final message what you couldn't do."
)
SANDBOX_NOTE = (
    " Shell commands run in a sandbox: no network, and writes only to the workspace and temp folders. "
    "If a command really needs the network or other folders, run it with BypassSandbox set to true; "
    "the reviewer then decides."
)

LIMIT_RE = re.compile(r"quota|resource exhausted|rate limit|usage limit|\b429\b", re.IGNORECASE)
CAPACITY_RE = re.compile(
    r"\boverloaded\b|\bno capacity available for model\b|\bmodel is at capacity\b|"
    r"\b503\s+(?:UNAVAILABLE|Service Unavailable)\b|\bUNAVAILABLE\s*\(code 503\)|"
    r"\bmodel is currently experiencing high demand\b", re.IGNORECASE,
)
# Only wording that says the CLI itself lacks a login; "sign in" alone shows up in ordinary output.
AUTH_RE = re.compile(
    r"authentication (is )?required|authentication expired|not (currently )?signed in|unauthenticated"
    r"|not logged into antigravity|log back in|visit the url to log in",
    re.IGNORECASE,
)
# What agy prints on stderr when it has no usable login. A headless run then waits for a
# login code until --print-timeout, so the runner stops it as soon as this shows up.
LOGIN_PROMPT_RE = re.compile(
    r"You are not logged into Antigravity|Authentication expired|Please log back in"
    r"|Authentication required\.? Please visit the URL",
    re.IGNORECASE,
)

FAQ_URL = (
    "https://antigravity.google/docs/faq"
    "#can-i-use-antigravity-cli-headless-mode-in-third-party-coding-agents-or-automate-account-switching"
)
ADD_NOTE = (
    "Use at your own risk: Google's Antigravity FAQ says consumer plans don't support automating "
    f"account switching ({FAQ_URL}). csa switches between your Google accounts when one runs out, "
    "like for the other CLIs; set failover = \"off\" under [antigravity] to keep jobs on your main account."
)

# agy stores its login in a file instead of the OS keyring when it detects an SSH session.
# Dummy values; the same three variables a community skill exports for this.
SSH_ENV = {
    "SSH_CONNECTION": "127.0.0.1 50000 127.0.0.1 22",
    "SSH_CLIENT": "127.0.0.1 50000 22",
    "SSH_TTY": "/dev/pts/0",
}
# On macOS and Linux the XDG folders move into the account folder too (as agys does).
XDG_ENV = {
    "XDG_CONFIG_HOME": ".config",
    "XDG_DATA_HOME": ".local/share",
    "XDG_STATE_HOME": ".local/state",
    "XDG_CACHE_HOME": ".cache",
}
# Variables that carry a Google login or key of their own: an extra account must use only its file login.
GOOGLE_LOGIN_ENV = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_GENAI_USE_GCA",
    "GOOGLE_CLOUD_ACCESS_TOKEN",
    "CLOUDSDK_CONFIG",
)
# Login files agy writes in an SSH-style session, relative to the account folder. The first
# name is the one caam and a community skill report; the others are fallbacks. Any file whose
# name contains "oauth-token" in .gemini/antigravity-cli (or one folder below) counts too.
LOGIN_FILES = (
    ".gemini/antigravity-cli/antigravity-oauth-token",
    ".gemini/oauth_creds.json",
    ".gemini/google_accounts.json",
)
TOKEN_MARK = "oauth-token"

# The shared setup, relative to the primary ~/.gemini, synced as three folders so each
# gets its own manifest. hooks.json and settings.json are written per folder instead
# (the user's content plus the review gate), and config.json is shared with an extra
# account only when it doesn't select a login of its own (see IDENTITY_KEYS).
ROOT_PLAN = sync.SyncPlan(files=("GEMINI.md", "AGENTS.md"))
CONFIG_FILES = ("GEMINI.md", "AGENTS.md")
CLI_FILES: tuple[str, ...] = ()
IDENTITY_KEYS = {("config", "config.json"): "enableAdc"}
SETTINGS_IDENTITY_KEY = "modelProvider"
SYNCED_FOLDERS = ("", "config", "antigravity-cli")
SELF_COMMANDS = {"csa", "csa.exe", "cli-subagents-mcp", "cli-subagents-mcp.exe"}
SELF_NAMES = {"cli-subagents", "cli_subagents"}
PROBE_JOB = "probe"  # job id used by csa accounts add/sync and csa launch: no worker, no channel
MCP_MARKER = ".cli-subagents-mcp-config"
# Read paths in a worker folder (and their sources in ~/.gemini) that hold the shared setup.
SETUP_READ_PATHS = (
    "GEMINI.md", "AGENTS.md", "config/GEMINI.md", "config/AGENTS.md", "config/rules", "config/skills",
    "config/workflows", "antigravity-cli/rules", "antigravity-cli/skills", "antigravity-cli/plugins",
    "antigravity-cli/mcp", "antigravity-cli/builtin",
)


def gemini_root(config: RuntimeConfig) -> Path:
    """The primary ~/.gemini folder (config.primary is its antigravity-cli folder by default)."""

    primary = Path(config.primary).expanduser()
    return primary.parent if primary.name == "antigravity-cli" else primary


def _is_self_server(name: str, server: Any) -> bool:
    """True for this tool's own MCP server: a worker must not start subagents of its own."""

    if str(name).lower() in SELF_NAMES:
        return True
    if not isinstance(server, dict):
        return False
    command = Path(str(server.get("command") or "")).name.lower()
    args = server.get("args")
    args = [str(item) for item in args] if isinstance(args, list) else []
    return command in SELF_COMMANDS or any("cli_subagents" in item for item in args)


def _identity_reason(path: Path, key: str) -> str | None:
    """Why a settings file can't be shared (it sets the key, or can't be read), or None if it can."""

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return "could not be read as JSON"
    if isinstance(data, dict) and key in data:
        return f"sets {key}, which can select a login of its own"
    return None


def _same_settings(text: str, wanted: dict[str, Any]) -> bool:
    """True when settings.json text means the same as `wanted`: agy drops keys that hold their default."""

    try:
        current = json.loads(text)
    except ValueError:
        return False
    if not isinstance(current, dict):
        return False

    def strip(data: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in data.items() if (key, value) != ("toolPermission", agy_review.install.TOOL_PERMISSION)}

    return strip(current) == strip(wanted)


def _link_folder(target: Path, link: Path) -> bool:
    """A junction (Windows) or symlink to a folder; never a copy, since the folder holds a login file."""

    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
            return True
        except (ImportError, OSError):
            pass
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        return False


def has_file_login(home: Path) -> bool:
    """True when agy's login file exists under `home` (checked by name only, never read)."""

    if any((home / name).is_file() for name in LOGIN_FILES):
        return True
    cli = home / ".gemini" / "antigravity-cli"
    try:
        folders = []
        for entry in cli.iterdir():
            if TOKEN_MARK in entry.name.lower() and entry.is_file():
                return True
            if entry.is_dir() and not is_link(entry):
                folders.append(entry)
        for folder in folders:
            if any(TOKEN_MARK in entry.name.lower() and entry.is_file() for entry in folder.iterdir()):
                return True
    except OSError:
        return False
    return False


def _reset_time(result: dict[str, Any], error: Any, text: str) -> float | None:
    values = []
    for source in (result, error if isinstance(error, dict) else {}):
        values.extend(source.get(key) for key in ("resets_at", "resetsAt", "reset_time", "resetTime"))
    match = re.search(r"reset(?:s)?(?: at| time)?[\s:=]+(\d{4}-\d\d-\d\d[T ][\d:.]+(?:Z|[+-]\d\d:\d\d)?)", text, re.IGNORECASE)
    if match:
        values.append(match.group(1))
    for value in values:
        try:
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str) and value:
                return float(value) if value.replace(".", "", 1).isdigit() else datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError):
            continue
    return None


class AntigravityParser:
    def __init__(self, review_log: Path | None = None) -> None:
        self.session_id: str | None = None
        self.init: dict[str, Any] = {}
        self.last_text = ""
        self.tool_uses = 0
        self.last_tool: str | None = None
        self.result: dict[str, Any] | None = None
        self.review_log = review_log  # set when the job ran behind the review gate
        self._texts: dict[int, str] = {}
        self._steps_seen: set[int] = set()

    def feed(self, event: dict[str, Any]) -> dict[str, Any] | None:
        kind = event.get("event")
        if event.get("conversation_id"):
            self.session_id = event["conversation_id"]
        if kind == "init":
            init = event.get("init") or {}
            self.init = {
                "model": init.get("model"),
                "permission_mode": init.get("permission_mode"),
                "tools": len(init.get("tools") or []),
            }
        elif kind == "step_update":
            step = event.get("step_update") or {}
            index = int(step.get("step_index") or 0)
            step_type = step.get("step_type")
            if step_type == "agent_response":
                self._texts[index] = self._texts.get(index, "") + str(step.get("text_delta") or "")
                if self._texts[index].strip():
                    self.last_text = self._texts[index]
            elif step_type not in (None, "user_input") and index not in self._steps_seen:
                self._steps_seen.add(index)
                self.tool_uses += 1
                self.last_tool = str(step.get("tool_name") or step_type)
        elif kind == "result":
            self.result = event.get("result") or {}
            self.session_id = self.result.get("conversation_id") or self.session_id
            if self.result.get("response"):
                self.last_text = str(self.result["response"])
        return None

    def progress(self) -> dict[str, Any]:
        return {"tool_uses": self.tool_uses, "last_tool": self.last_tool, "last_text": self.last_text[-600:]}

    def finish(self, exit_code: int | None, stderr_tail: str) -> AttemptResult:
        result = self.result or {}
        common = {"session_id": self.session_id, "usage": result.get("usage"), "num_turns": result.get("num_turns")}
        response = (str(result.get("response") or "") or self.last_text).strip()
        if result.get("status") == "SUCCESS" and exit_code == 0:
            denied = [item for item in result.get("denied_actions") or [] if isinstance(item, dict)]
            if not response and denied:
                # Headless agy refused a tool it couldn't ask about and stopped without an answer.
                return AttemptResult(ERROR, message=self._denied_message(denied, stderr_tail), **common)
            return AttemptResult(SUCCESS, text=response, **common)
        text = response  # retain the response even if the CLI failed after emitting it
        error = result.get("error")
        error_text = error if isinstance(error, str) else str((error or {}).get("message") or "")
        haystack = "\n".join(part for part in (error_text, stderr_tail) if part)
        message = next((line.strip() for line in haystack.splitlines() if line.strip()), "")[:300]
        if (isinstance(error, dict) and error.get("code") == 503) or CAPACITY_RE.search(haystack):
            return AttemptResult(ERROR, message=message or "model at capacity (HTTP 503)", text=text,
                                 error_text=haystack[-4000:], at_capacity=True, **common)
        if LIMIT_RE.search(haystack):
            return AttemptResult(
                USAGE_LIMIT, message=message or "quota reached", text=text, error_text=haystack[-4000:],
                resets_at=_reset_time(result, error, haystack), **common
            )
        if AUTH_RE.search(haystack):
            return AttemptResult(AUTH, message=message or "Antigravity needs a login", text=text, **common)
        status = result.get("status")
        detail = message or (f"result status {status}" if status else "no result")
        return AttemptResult(ERROR, message=f"agy exited with status {exit_code}: {detail}", text=text, **common)

    def _denied_message(self, denied: list[dict[str, Any]], stderr_tail: str) -> str:
        names = sorted({str(item.get("display_name") or item.get("action") or "?") for item in denied})
        actions = sorted({str(item.get("action") or "?") for item in denied})
        message = (
            f"agy stopped without an answer: it refused {', '.join(names)} (permission {', '.join(actions)}) "
            "because a headless run can't ask for approval."
        )
        if self.review_log is not None and not review_store.read_log(self.review_log):
            message += " The review gate never answered, so its hook did not load; check config/hooks.json in the worker's folder."
        elif self.review_log is None:
            message += " This job ran without the review gate (access full skips it; see docs/gemini.md)."
        detail = next((line.strip() for line in stderr_tail.splitlines() if "permission" in line.lower()), "")
        return (message + (f" agy said: {detail[:300]}" if detail else ""))[:900]


class AntigravityRuntime:
    name = "antigravity"
    add_note = ADD_NOTE

    def __init__(self) -> None:
        self._review_log: Path | None = None

    def find_binary(self, config: RuntimeConfig) -> list[str] | None:
        override = os.environ.get("CLI_SUBAGENTS_AGY_COMMAND")
        if override:
            return list(json.loads(override))
        candidates = [config.binary, os.environ.get("AGY_CMD"), "agy"]
        for candidate in candidates:
            if candidate:
                found = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
                if found:
                    return [found]
        local = os.environ.get("LOCALAPPDATA")
        if local and (Path(local) / "agy" / "bin" / "agy.exe").is_file():
            return [str(Path(local) / "agy" / "bin" / "agy.exe")]
        return None

    # Accounts -------------------------------------------------------------

    def account_env(self, config: RuntimeConfig, account: Account) -> dict[str, str]:
        """The primary keeps the normal environment; an extra account gets its own HOME and a file login."""

        env = clean_environment()
        if account.is_primary:
            return env
        for key in list(env):
            if key.upper() in GOOGLE_LOGIN_ENV:
                del env[key]
        home = Path(account.home).expanduser()
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env.update(SSH_ENV)
        if os.name != "nt":
            for key, relative in XDG_ENV.items():
                env[key] = str(home / relative)
        return env

    def worker_home(self, config: RuntimeConfig, account: Account) -> Path | None:
        """The folder a job runs in, or None when the primary must use the real home (file login)."""

        if not account.is_primary:
            return Path(account.home).expanduser()
        if has_file_login(gemini_root(config).parent):
            return None
        return homes_dir() / "antigravity" / PRIMARY_FOLDER

    def worker_env(self, config: RuntimeConfig, account: Account, home: Path | None) -> dict[str, str]:
        if not account.is_primary:
            return self.account_env(config, account)
        env = clean_environment()
        if home is not None:
            env["HOME"] = str(home)
            env["USERPROFILE"] = str(home)
        return env

    def logged_in(self, account: Account) -> bool:
        """An extra account counts as logged in when agy's login file exists (checked by name only)."""

        return has_file_login(Path(account.home).expanduser())

    def login_hint(self, account: Account) -> str:
        if account.is_primary:
            return "Run agy in a terminal and sign in."
        return f"Run: csa accounts login {account.name} --runtime antigravity"

    def preflight(self, config: RuntimeConfig, account: Account) -> AttemptResult | None:
        """Ends an extra-account job at once when the account has no login yet."""

        if account.is_primary or self.logged_in(account):
            return None
        return AttemptResult(
            AUTH, message=f"Antigravity account '{account.name}' has no login yet. {self.login_hint(account)}"
        )

    def check_stderr(self, text: str, account: Account) -> AttemptResult | None:
        """Called by the runner on new stderr: stop a run that is waiting for a login."""

        match = LOGIN_PROMPT_RE.search(text or "")
        if not match:
            return None
        line = next((part.strip() for part in text[match.start():].splitlines() if part.strip()), match.group(0))
        return AttemptResult(AUTH, message=f"{line[:200]} {self.login_hint(account)}", error_text=text[-4000:])

    def _run_quiet(self, config: RuntimeConfig, args: list[str], timeout: int = 60) -> tuple[int, str]:
        command = self.find_binary(config)
        if not command:
            return 127, "agy binary not found"
        try:
            completed = subprocess.run(
                [*command, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, env=clean_environment(), stdin=subprocess.DEVNULL, **no_window_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, f"{type(exc).__name__}: {exc}"
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")

    def version(self, config: RuntimeConfig) -> str | None:
        code, output = self._run_quiet(config, ["--version"])
        return output.strip().splitlines()[0] if code == 0 and output.strip() else None

    def auth_status(self, config: RuntimeConfig, account: Account) -> dict[str, Any]:
        """Primary: lists models, which needs a login but no model request. Extra accounts: the login file.

        agy models isn't run for an extra account: without a login it would wait for a login code.
        """

        if not account.is_primary:
            if self.logged_in(account):
                return {"loggedIn": True, "method": "file"}
            return {"loggedIn": False, "method": "file", "error": f"no login file yet. {self.login_hint(account)}"}
        code, output = self._run_quiet(config, ["models"], timeout=90)
        models = [line.split()[0] for line in output.splitlines() if re.match(r"^[a-z0-9][a-z0-9._-]*\s", line)]
        if code == 0 and models:
            return {"loggedIn": True, "models": models}
        return {"loggedIn": False, "error": output.strip().splitlines()[-1][:200] if output.strip() else f"exit {code}"}

    def login_command(self, config: RuntimeConfig, account: Account) -> tuple[list[str], dict[str, str]]:
        """Plain interactive agy. In an extra account's environment it shows a sign-in link and asks for the code."""

        command = self.find_binary(config)
        if not command:
            raise RuntimeError("agy binary not found; install Antigravity CLI or set [antigravity] binary")
        return [*command], self.account_env(config, account)

    def login_cwd(self, account: Account) -> str | None:
        return None if account.is_primary else str(Path(account.home).expanduser())

    def login_instructions(self, account: Account) -> str:
        if account.is_primary:
            return "Sign in in agy, then exit it."
        return (
            "agy prints a Google sign-in link. Open it, sign in with the account this profile should use, "
            "paste the code back into agy, then exit agy (/exit or Ctrl+C). Ignore odd characters in the "
            "prompt: agy thinks it runs over SSH and queries the terminal."
        )

    # Shared setup ---------------------------------------------------------

    def _plans(self, root: Path, report: dict[str, Any], primary: bool = False) -> list[tuple[str, sync.SyncPlan]]:
        files = {"config": list(CONFIG_FILES), "antigravity-cli": list(CLI_FILES)}
        for (folder, name), key in IDENTITY_KEYS.items():
            source = root / folder / name
            if not source.is_file():
                continue
            reason = None if primary else _identity_reason(source, key)
            if reason:
                report["warnings"].append(f"{folder}/{name} {reason}; not shared with extra Antigravity accounts")
            else:
                files[folder].append(name)
        try:
            # This tool's own plugin stays out: workers must not start subagents of their own.
            own = frozenset(
                ("plugins", entry.name)
                for entry in (root / "antigravity-cli" / "plugins").iterdir()
                if "cli-subagents" in entry.name.lower()
            )
        except OSError:
            own = frozenset()
        return [
            ("", ROOT_PLAN),
            ("config", sync.SyncPlan(whole=("rules",), folders=("skills",), files=tuple(files["config"]))),
            (
                "antigravity-cli",
                sync.SyncPlan(
                    whole=("rules",), folders=("skills", "plugins"), files=tuple(files["antigravity-cli"]), skip=own
                ),
            ),
        ]

    def prepare(self, config: RuntimeConfig, account: Account, spec: JobSpec) -> dict[str, Any]:
        report = sync.new_report()
        home = self.worker_home(config, account)
        if home is None:
            report["warnings"].append(
                "your primary Antigravity login is a token file in your home folder, so its jobs run there "
                "without the review gate and without the parent channel"
            )
            return report
        root = gemini_root(config)
        if same_path(home, root.parent) or same_path(home, Path.home()):
            raise RuntimeError(f"{home} is your own home folder; an extra Antigravity account needs its own folder")
        with file_lock(state_dir() / "sync" / f"antigravity-{account.name}.lock", timeout=30):
            make_private_dir(home)  # holds the account's login file (extra accounts)
            gemini = home / ".gemini"
            for folder, plan in self._plans(root, report, primary=account.is_primary):
                part = sync.new_report()
                sync.sync(root / folder if folder else root, gemini / folder if folder else gemini, plan, part)
                prefix = f"{folder}/" if folder else ""
                for key, values in part.items():
                    report[key] += [value if key in ("conflicts", "warnings") else prefix + value for value in values]
            report["review_gate"] = self._write_hooks(root, gemini, report)
            self._write_settings(root, gemini, account, report)
            report["parent_channel"] = self._write_mcp_config(config, root, gemini, spec, report, account)
            report["worker_home"] = str(home)
            if report["review_gate"] and self._uses_own_agy_reviewer(config) and spec.job_id != PROBE_JOB:
                reviewer_home = self._prepare_reviewer_home(account, home, report)
                if reviewer_home is not None:
                    report["reviewer_home"] = str(reviewer_home)
        return report

    def _write_generated(self, path: Path, content: str, report: dict[str, Any], same: Any = None) -> bool:
        """Write a file this adapter owns in a worker folder; keep one copy of what was there first.

        `same(current_text)` may say that a different text means the same (agy rewrites settings.json).
        """

        relative = "/".join(path.parts[-2:])
        if is_link(path) or is_link(path.parent):
            report["conflicts"].append(f"{relative}: is a link in the account folder, left in place")
            return False
        current = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
        if current == content or (current is not None and same is not None and same(current)):
            return True
        if current is not None:
            backup = path.with_name(path.name + ".before-cli-subagents")
            if not os.path.lexists(backup):
                copy_exclusive(path, backup)
                report["warnings"].append(f"kept the folder's previous {relative} as {backup.name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text(path, content)
        return True

    def _write_hooks(self, root: Path, gemini: Path, report: dict[str, Any]) -> bool:
        """config/hooks.json: the user's hooks plus the review gate. Returns True when the gate is in place."""

        source = root / "config" / "hooks.json"
        user: Any = read_json(source, None) if source.is_file() else {}
        if not isinstance(user, dict):
            report["warnings"].append("config/hooks.json in your setup is not a JSON object; workers don't get your hooks")
            user = {}
        try:
            command = agy_review.hook_command(python_executable())
        except ValueError as exc:
            report["warnings"].append(f"the review gate is off for Antigravity workers: {exc}")
            content, gate = user, False
        else:
            content, gate = agy_review.merged_hooks(user, command), True
        written = self._write_generated(gemini / "config" / "hooks.json", json.dumps(content, indent=2) + "\n", report)
        return gate and written

    def _write_settings(self, root: Path, gemini: Path, account: Account, report: dict[str, Any]) -> None:
        """antigravity-cli/settings.json: the user's settings with the permission mode the gate relies on."""

        source = root / "antigravity-cli" / "settings.json"
        user: Any = {}
        if source.is_file():
            data = read_json(source, None)
            if not isinstance(data, dict):
                report["warnings"].append("antigravity-cli/settings.json could not be read as JSON; workers don't get your settings")
            elif not account.is_primary and SETTINGS_IDENTITY_KEY in data:
                report["warnings"].append(
                    f"antigravity-cli/settings.json sets {SETTINGS_IDENTITY_KEY}, which can select a login of its own; "
                    "not shared with extra Antigravity accounts"
                )
            else:
                user = data
        wanted = agy_review.gated_settings(user)
        content = json.dumps(wanted, indent=2) + "\n"
        self._write_generated(
            gemini / "antigravity-cli" / "settings.json", content, report, same=lambda text: _same_settings(text, wanted)
        )

    def _prepare_reviewer_home(self, account: Account, worker: Path, report: dict[str, Any]) -> Path | None:
        """A bare folder the agy reviewer runs from: no MCP servers, no user hooks, only the gate (which
        denies the reviewer every tool). The primary finds its keyring login from any folder; an extra
        account's login file lives in .gemini/antigravity-cli, so that one folder is linked from the
        account folder. Returns None (the reviewer then runs in the worker folder) when that link fails."""

        folder = homes_dir() / "antigravity" / REVIEWER_FOLDER / account.name
        gemini = folder / ".gemini"
        make_private_dir(gemini / "config")
        cli = gemini / "antigravity-cli"
        if account.is_primary:
            make_private_dir(cli)
            self._write_generated(cli / "settings.json", "{}\n", report, same=lambda text: _same_settings(text, {}))
        else:
            target = worker / ".gemini" / "antigravity-cli"
            make_private_dir(target)
            if is_link(cli) and not same_path(link_target(cli) or "", target):
                remove_link(cli)
            if not is_link(cli):
                if os.path.lexists(cli) or not _link_folder(target, cli):
                    report["warnings"].append(
                        f"could not prepare a separate reviewer folder for {account.name}; the reviewer runs in its folder"
                    )
                    return None
        try:
            command = agy_review.hook_command(python_executable())
        except ValueError:
            return None
        self._write_generated(gemini / "config" / "hooks.json", json.dumps(agy_review.merged_hooks({}, command), indent=2) + "\n", report)
        self._write_generated(gemini / "config" / "mcp_config.json", json.dumps({"mcpServers": {}}) + "\n", report)
        return folder

    def _parent_mcp_servers(self, config: RuntimeConfig, spec: JobSpec, report: dict[str, Any]) -> dict[str, Any]:
        """The Claude session's MCP servers in agy's shape, for jobs started from Claude Code."""

        if spec.job_id == PROBE_JOB or not config.share_parent_mcp or not spec.parent or spec.parent.get("host") != "claude":
            return {}
        try:
            from cli_subagents.config import default_primary, load_config

            try:
                primary = load_config().runtime("claude").primary
            except (OSError, ValueError):
                primary = default_primary("claude")
            collected = mcp_translate.collect_servers(primary, spec.cwd)
            servers, warnings = mcp_translate.to_agy(collected.servers)
        except Exception as exc:  # sharing is a convenience: never fail the job over it
            report["warnings"].append(f"could not read the Claude session's MCP servers: {type(exc).__name__}")
            return {}
        report["warnings"] += [*collected.warnings, *warnings]
        return servers

    def _write_mcp_config(
        self, config: RuntimeConfig, root: Path, gemini: Path, spec: JobSpec, report: dict[str, Any],
        account: Account | None = None,
    ) -> bool:
        """Write the folder's config/mcp_config.json: the user's servers, the parent session's, and csa-parent.

        agy has no flag for per-run MCP servers, and whether it passes its environment on to
        MCP servers is unverified, so the job id is written into the file. The file is
        rewritten for every job while that job holds the account's slot; with one slot per
        account (the default) no other job starts from it in between. With more slots the
        channel is left out. Returns True when the worker gets the channel.
        """

        source = root / "config" / "mcp_config.json"
        data: Any = read_json(source, {}) if source.is_file() else {}
        if not isinstance(data, dict):
            report["warnings"].append(
                "config/mcp_config.json in your setup is not a JSON object; workers get no MCP servers from it"
            )
            data = {}
        servers = data.get("mcpServers")
        servers = servers if isinstance(servers, dict) else {}
        shared = {name: server for name, server in servers.items() if not _is_self_server(name, server)}
        for name, server in self._parent_mcp_servers(config, spec, report).items():
            if name not in shared and not _is_self_server(name, server) and name != PARENT_SERVER:
                shared[name] = server  # your own Gemini servers win on a name clash
        slots = plans.account_cap(config, account) if account is not None else config.max_jobs_per_account
        channel = spec.job_id != PROBE_JOB and slots == 1
        if channel:
            shared[PARENT_SERVER] = parent_server(spec.job_id)
        content = json.dumps({**data, "mcpServers": shared}, indent=2) + "\n"

        destination = gemini / "config" / "mcp_config.json"
        if is_link(destination) or is_link(destination.parent):
            report["conflicts"].append("config/mcp_config.json: is a link in the account folder, left in place")
            return False
        current = destination.read_text(encoding="utf-8") if destination.is_file() else None
        if current == content:
            return channel
        marker = gemini / MCP_MARKER
        if current is not None and not marker.exists():
            # First run on a folder that already had its own MCP config: keep a copy.
            backup = destination.with_name("mcp_config.json.before-cli-subagents")
            if not os.path.lexists(backup):
                copy_exclusive(destination, backup)
                report["warnings"].append(f"kept the account's previous config/mcp_config.json as {backup.name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_text(destination, content)
        write_text(marker, "config/mcp_config.json is generated from the primary setup for every job\n")
        return channel

    def fallback_copies(self, account: Account) -> list[str]:
        """Shared entries that fell back to copies, across the account's three synced folders."""

        if account.is_primary:
            return []
        gemini = Path(account.home).expanduser() / ".gemini"
        found: list[str] = []
        for folder in SYNCED_FOLDERS:
            prefix = f"{folder}/" if folder else ""
            found += [prefix + item for item in sync.fallback_copies(gemini / folder if folder else gemini)]
        return found

    # Review gate ----------------------------------------------------------

    def _reviewer_chain(self, config: RuntimeConfig, job: Path, warnings: list[str],
                        reviewer_home: str | None = None) -> list[dict[str, Any]]:
        # An agy reviewer without `account` uses the job's own account and its reviewer folder.
        # The worker runs with a managed HOME and SSH/XDG folders; the other reviewers get the real ones.
        return build_chain(config, job, warnings, own_agy=(self.find_binary(config) or [], reviewer_home),
                           home=str(gemini_root(config).parent), unset_env=[*SSH_ENV, *XDG_ENV])

    @staticmethod
    def _uses_own_agy_reviewer(config: RuntimeConfig) -> bool:
        entries, _defaults = config.review_chain()
        return any(entry.kind == "agy" and entry.account is None for entry in entries)

    def standalone_reviewer_home(self, config: RuntimeConfig, account: Account, warnings: list[str]) -> Path | None:
        """An account's bare reviewer folder, for an agy reviewer outside that account's own jobs
        (another runtime's job, or an `account` named in the reviewer chain).

        None (with a warning) when the account is the primary and its login is a token file in
        the real home: the reviewer can't run from a bare folder then, and the real home has no
        gate to stop it from using tools.
        """

        worker = self.worker_home(config, account)
        if worker is None:
            warnings.append("your primary Antigravity login is a token file in your home folder, so the agy reviewer "
                            "can't run from its own folder")
            return None
        report = sync.new_report()
        with file_lock(state_dir() / "sync" / f"antigravity-{account.name}.lock", timeout=30):
            folder = self._prepare_reviewer_home(account, worker, report)
        warnings.extend(report["warnings"])
        return folder

    def _review_context(
        self, config: RuntimeConfig, spec: JobSpec, home: Path, sandbox: bool, warnings: list[str], setup: dict[str, Any]
    ) -> Path:
        job = jobs_dir() / spec.job_id
        root = gemini_root(config)
        parent = spec.parent or {}
        read_paths = [str(item) for item in [*(parent.get("instructions") or []), *(parent.get("skills") or [])]]
        for base in (home / ".gemini", root):
            read_paths += [str(base / relative) for relative in SETUP_READ_PATHS]
        data = {
            "version": 1,
            "job": spec.job_id,
            "access": spec.access,
            "sandbox": sandbox,
            "workspaces": [spec.cwd, *spec.add_dirs],
            "read_paths": read_paths,
            "protected_paths": [str(csa_home())],
            "agent_home": str(home),
            "user_home": str(root.parent),
            "trusted_mcp_servers": [PARENT_SERVER],
            "reviewers": self._reviewer_chain(config, job, warnings, setup.get("reviewer_home")),
            "review_budget": AGY_REVIEW_BUDGET,
            "log": str(job / "review.jsonl"),
            "cache": str(job / "review-cache.json"),
        }
        return agy_review.write_context(job / "review-context.json", data)

    # Launch ---------------------------------------------------------------

    def launch(
        self, config: RuntimeConfig, account: Account, spec: JobSpec, prompt: str, setup: dict[str, Any]
    ) -> Launch:
        command = self.find_binary(config)
        if not command:
            raise RuntimeError("agy binary not found; install Antigravity CLI or set [antigravity] binary")
        home = self.worker_home(config, account)
        warnings: list[str] = []
        gate = spec.access in GATED_ACCESS and home is not None and bool(setup.get("review_gate"))
        sandbox = gate and config.sandbox and spec.access == "edit"
        if spec.access in GATED_ACCESS and not gate:
            warnings.append(
                "this job runs without the review gate, so agy refuses commands, MCP calls and reads outside the "
                "workspace (headless mode can't ask)"
            )
        extra = ""
        if setup.get("parent_channel"):
            extra += WORKER_TOOLS_NOTE
        if gate:
            extra += GATE_NOTE + (SANDBOX_NOTE if sandbox else "")
        note = WORKER_NOTE.rstrip("]\n") + extra + "]\n\n" if extra else WORKER_NOTE
        text = note + ("" if spec.resume_session else parent_note(spec.parent, "antigravity")) + prompt
        if len(text) > MAX_PROMPT_CHARS:
            raise RuntimeError(
                f"prompt is {len(text)} characters; Antigravity takes at most {MAX_PROMPT_CHARS}. "
                "Point to files instead of pasting them."
            )
        mode = MODES.get(spec.access) or (config.edit_permission_mode if config.edit_permission_mode in VALID_MODES else "accept-edits")
        argv = [*command, "--mode", mode, "--output-format", "stream-json"]
        if spec.access == "full":
            argv.append("--dangerously-skip-permissions")
        if sandbox:
            argv.append("--sandbox")
        model = spec.model or config.default_model
        if model:
            argv += ["--model", model]
        if spec.effort and takes_effort(model):
            argv += ["--effort", spec.effort]
        elif spec.effort:
            warnings.append(f"effort '{spec.effort}' left out: agy takes no --effort for model '{model}'")
        for directory in spec.add_dirs:
            argv.append(f"--add-dir={directory}")
        if spec.resume_session:
            argv += ["--conversation", str(spec.resume_session)]
        argv.append(f"--print-timeout={spec.timeout_seconds}s")
        argv.append(f"--print={text}")
        env = self.worker_env(config, account, home)
        env["CLI_SUBAGENTS_DEPTH"] = str(spec.depth)
        env["CLI_SUBAGENTS_JOB_ID"] = spec.job_id
        self._review_log = None
        if gate and home is not None:
            context = self._review_context(config, spec, home, sandbox, warnings, setup)
            env[agy_review.CONTEXT_ENV] = str(context)
            self._review_log = jobs_dir() / spec.job_id / "review.jsonl"
        return Launch(argv=argv, env=env, stdin="", session_id=spec.resume_session, warnings=warnings)

    def interactive(self, config: RuntimeConfig, account: Account, setup: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
        return self.login_command(config, account)

    def new_parser(self) -> AntigravityParser:
        return AntigravityParser(self._review_log)
