"""Remote adapter: Claude Code runs on a server, the job's tools act on this PC.

An account of this runtime is a Claude Code config folder on a server, logged
in there with Claude Code's own `/login` (`csa accounts login NAME --runtime
remote` opens it over `ssh -t`). A job starts `cli_subagents.remote.relay` on
this PC. The relay:

* serves the project folder through a local MCP server (`remote/bridge.py`),
  read-only for `access=read`, and proxies the csa-parent tools through it;
* packs the Claude setup of `[remote] primary` without login data
  (`remote/bundle.py`);
* opens `ssh -T -R 127.0.0.1:0:127.0.0.1:<bridge port> <server> job`, so the
  server reaches the bridge through the job's own connection;
* prints the server's Claude Code stream-json events on stdout, which
  `ClaudeParser` reads as it does for a local worker;
* sends follow-ups from the main session into the running conversation.

The server (`remote/server.py`, the ssh forced command) runs the unmodified
`claude -p` in the account folder csa named, after checking that the account
belongs to the ssh user. No code here reads, copies or sends a Claude login.
A job and its follow-ups never move to another account.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any
import uuid

from cli_subagents import accounts
from cli_subagents.config import Account, RuntimeConfig, jobs_dir, state_dir
from cli_subagents.fsutil import (
    find_program,
    locate_program,
    no_window_kwargs,
    python_executable,
    read_json,
    write_json,
    write_private,
)
from cli_subagents.runtimes.base import (
    AUTH,
    BUDGET,
    ERROR,
    USAGE_LIMIT,
    WORKER_TOOLS_NOTE,
    AttemptResult,
    JobSpec,
    Launch,
    clean_environment,
    parent_server,
)
from cli_subagents.runtimes.claude import WORKER_NOTE, ClaudeParser, _state_files

RELAY_EVENT = "csa.remote"  # events the relay adds to the worker's stream (accepted, delivered, warning, error)
STATUS_CACHE_SECONDS = 120
SSH_TIMEOUT_SECONDS = 45
# Where a project keeps instructions Claude Code would load locally. On the server
# they are files on the PC, so the worker is told to read them with pc_read.
PROJECT_INSTRUCTIONS = ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md", "AGENTS.md")

REMOTE_NOTE = (
    " You run on a remote server; the project folder is on the user's PC. Use the mcp__pc__ tools "
    "for every file and command in the project (paths are relative to the project folder)."
)
READ_NOTE = " This job is read-only: you can read and search the project, not change it or run commands."


def ssh_options(config: RuntimeConfig, *, batch: bool = True) -> list[str]:
    """Options for ssh calls to the server: the configured key and, for jobs and status, no prompts.

    The interactive login runs without BatchMode, so ssh can ask about an unknown
    host key or a key passphrase.
    """

    options = (["-o", "BatchMode=yes"] if batch else []) + ["-o", "ServerAliveInterval=30"]
    if config.ssh_key:
        options += ["-i", config.ssh_key]
    return options


class RemoteParser(ClaudeParser):
    """ClaudeParser plus the relay's own events: the account the server took and its refusals."""

    def __init__(self) -> None:
        super().__init__()
        self.account: str | None = None
        self.resumed: bool | None = None
        self.remote_error: dict[str, str] | None = None
        self.accepted = False

    def feed(self, event: dict[str, Any]) -> dict[str, Any] | None:
        if event.get("type") != RELAY_EVENT:
            return super().feed(event)
        what = event.get("event")
        if what == "accepted":
            self.accepted = True
            self.account = str(event.get("account") or "") or None
            self.resumed = bool(event.get("resumed"))
            if event.get("session_id"):
                self.session_id = str(event["session_id"])
        elif what == "error":
            self.remote_error = {"code": str(event.get("code") or "internal"),
                                 "message": str(event.get("message") or "the server refused the job")}
        return None

    def progress(self) -> dict[str, Any]:
        data = super().progress()
        if self.account:
            data["server_account"] = self.account
        return data

    def finish(self, exit_code: int | None, stderr_tail: str) -> AttemptResult:
        error = self.remote_error
        if error is None or self.result is not None:
            return super().finish(exit_code, stderr_tail)
        code, message = error["code"], error["message"]
        common = {"session_id": self.session_id, "text": self.last_text, "before_start": not self.accepted}
        if code == "all_limited":
            return AttemptResult(USAGE_LIMIT, message=f"server: {message}", error_text=message, no_rotation=True, **common)
        if code in ("account_not_owned", "no_account"):
            return AttemptResult(AUTH, message=f"server: {message}", **common)
        if code == "cap_reached":  # the server admin's weekly cap for this person
            return AttemptResult(BUDGET, message=f"server: {message}", **common)
        if code == "claude_failed":
            # Claude Code ended without a result: judge its last words like a local worker's.
            judged = super().finish(exit_code if exit_code else 1, "\n".join(part for part in (message, stderr_tail) if part))
            judged.message = f"server: {judged.message}"
            return judged
        return AttemptResult(ERROR, message=f"server ({code}): {message}", **common)


class RemoteRuntime:
    name = "remote"
    add_note = ("This account lives on the server: its name must match an account the server's admin "
                "listed for your ssh key. Log it in once with: csa accounts login NAME --runtime remote")

    # Binary --------------------------------------------------------------

    def find_binary(self, config: RuntimeConfig) -> list[str] | None:
        """The ssh client. CLI_SUBAGENTS_SSH_COMMAND (a JSON list) replaces it, for tests."""

        override = os.environ.get("CLI_SUBAGENTS_SSH_COMMAND")
        if override:
            return list(json.loads(override))
        if config.binary:
            found = locate_program(config.binary)
            return [found] if found else None
        found = find_program("ssh")
        if found:
            return [found]
        system = os.environ.get("SystemRoot")
        if system and (Path(system) / "System32" / "OpenSSH" / "ssh.exe").is_file():
            return [str(Path(system) / "System32" / "OpenSSH" / "ssh.exe")]
        return None

    def _run(self, config: RuntimeConfig, args: list[str], timeout: float = SSH_TIMEOUT_SECONDS) -> tuple[int, str, str]:
        command = self.find_binary(config)
        if not command:
            return 127, "", "ssh not found"
        try:
            done = subprocess.run([*command, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout, env=clean_environment(), stdin=subprocess.DEVNULL, **no_window_kwargs())
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, "", f"{type(exc).__name__}: {exc}"
        return done.returncode, done.stdout or "", done.stderr or ""

    def version(self, config: RuntimeConfig) -> str | None:
        code, out, err = self._run(config, ["-V"], timeout=15)
        text = (err or out).strip()
        return text.splitlines()[0] if text else None

    # Accounts -------------------------------------------------------------

    def server_status(self, config: RuntimeConfig, refresh: bool = False) -> dict[str, Any]:
        """`ssh <server> status --json`: the ssh user's accounts on the server, cached a short while.

        The server answers from Claude Code's local `/usage` command; no model call is made.
        """

        if not config.server:
            return {"error": "no server set; add server = \"user@host\" under [remote]"}
        cache = state_dir() / "remote" / "status.json"
        cached = read_json(cache, {}) or {}
        key = f"{config.server}|{config.ssh_key or ''}"
        entry = cached.get(key) if isinstance(cached, dict) else None
        if not refresh and isinstance(entry, dict) and time.time() - float(entry.get("at") or 0) < STATUS_CACHE_SECONDS:
            return entry["status"]
        code, out, err = self._run(config, [*ssh_options(config), "-T", config.server, "status", "--json"])
        try:
            status = json.loads(out[out.index("{"):]) if code == 0 else None
        except ValueError:
            status = None
        if not isinstance(status, dict) or not isinstance(status.get("accounts"), list):
            detail = (err or out).strip().splitlines()
            return {"error": (detail[-1] if detail else f"ssh exited with status {code}")[:300]}
        cached = cached if isinstance(cached, dict) else {}
        cached[key] = {"at": time.time(), "status": status}
        write_json(cache, cached)
        self._record_usage(status)
        return status

    def _forget_status(self, config: RuntimeConfig) -> None:
        cache = state_dir() / "remote" / "status.json"
        cached = read_json(cache, {}) or {}
        if isinstance(cached, dict) and cached.pop(f"{config.server}|{config.ssh_key or ''}", None) is not None:
            write_json(cache, cached)

    def _record_usage(self, status: dict[str, Any]) -> None:
        """Keep the server's usage readings beside the rate-limit windows from job events."""

        now = time.time()
        records = accounts.load_usage(self.name)
        for item in status.get("accounts") or []:
            if not isinstance(item, dict) or not isinstance(item.get("usage_percent"), (int, float)):
                continue
            name = str(item.get("name") or "")
            windows = dict((records.get(name) or {}).get("windows") or {})
            windows["server"] = {"utilization": float(item["usage_percent"]) / 100, "resets_at": None}
            accounts.update_usage(self.name, name, windows=windows, observed_at=now)

    def auth_status(self, config: RuntimeConfig, account: Account) -> dict[str, Any]:
        status = self.server_status(config)
        if status.get("error"):
            return {"loggedIn": False, "error": status["error"], "server": config.server}
        entry = next((item for item in status["accounts"] if isinstance(item, dict) and item.get("name") == account.name), None)
        if entry is None:
            return {"loggedIn": False, "server": config.server,
                    "error": f"the server has no account '{account.name}' for this ssh key"}
        report = {"loggedIn": bool(entry.get("logged_in")), "authMethod": "server", "server": config.server}
        if isinstance(entry.get("usage_percent"), (int, float)):
            report["usagePercent"] = entry["usage_percent"]
        if isinstance(entry.get("running"), int):
            report["runningOnServer"] = entry["running"]
        return report

    def login_command(self, config: RuntimeConfig, account: Account) -> tuple[list[str], dict[str, str]]:
        command = self.find_binary(config)
        if not command:
            raise RuntimeError("ssh not found; install OpenSSH or set [remote] binary")
        if not config.server:
            raise RuntimeError('no server set; add server = "user@host" under [remote]')
        self._forget_status(config)  # the next status call asks the server again
        # Claude Code's own /login runs on the server, in that account's folder.
        return [*command, *ssh_options(config, batch=False), "-t", config.server, "login", account.name], clean_environment()

    def login_instructions(self, account: Account) -> str:
        return "Claude Code opens on the server. Type /login, finish the sign-in in your browser, then type /exit."

    def preflight(self, config: RuntimeConfig, account: Account) -> AttemptResult | None:
        if not config.server:
            return AttemptResult(ERROR, message='no server set; add server = "user@host" under [remote] in config.toml')
        return None

    # Setup ----------------------------------------------------------------

    def prepare(self, config: RuntimeConfig, account: Account, spec: JobSpec) -> dict[str, Any]:
        """Pack the Claude setup of [remote] primary for the server; nothing changes on this PC."""

        from cli_subagents.remote import bundle as bundle_module

        report: dict[str, Any] = {"warnings": [], "conflicts": [], "mcp_servers": []}
        built = bundle_module.build_bundle(config.primary, spec.cwd, state_files=_state_files(config.primary))
        report["warnings"] += [f"setup for the server: {item}" for item in built.warnings]
        report["mcp_servers"] = list((built.manifest.get("mcp_servers") or {}).get("kept") or [])
        report["bundle_sha256"], report["bundle_size"] = built.sha256, built.size
        if spec.job_id != "probe":
            path = jobs_dir() / spec.job_id / "remote" / "bundle.zip"
            write_private(path, built.data)  # it holds the MCP server settings that go to the server
            report["bundle_path"] = str(path)
        return report

    # Launch ---------------------------------------------------------------

    def launch(
        self, config: RuntimeConfig, account: Account, spec: JobSpec, prompt: str, setup: dict[str, Any]
    ) -> Launch:
        command = self.find_binary(config)
        if not command:
            raise RuntimeError("ssh not found; install OpenSSH or set [remote] binary in config.toml")
        if not config.server:
            raise RuntimeError('no server set; add server = "user@host" under [remote] in config.toml')
        folder = jobs_dir() / spec.job_id / "remote"
        resume = None
        if spec.resume_session:
            resume = {"session_id": spec.resume_session, "account": spec.resume_account or account.name}
        job = {
            "csa_job_id": spec.job_id,
            "wire_job_id": str(uuid.uuid4()),
            "ssh": [*command, *ssh_options(config)],
            "server": config.server,
            "account": account.name,
            "cwd": spec.cwd,
            "access": spec.access,
            "model": spec.model or config.default_model,
            "effort": spec.effort,
            "max_turns": spec.max_turns,
            "resume": resume,
            "history_jobs": history_jobs(spec) if resume else [],
            "bundle": ({"path": setup["bundle_path"], "sha256": setup["bundle_sha256"], "size": setup["bundle_size"]}
                       if setup.get("bundle_path") else None),
            "parent": parent_server(spec.job_id),
        }
        path = folder / "job.json"
        write_json(path, job)
        relay = Path(__file__).resolve().parents[1] / "remote" / "relay.py"
        # -I: a package or module in the project folder (the worker's cwd) can't shadow the relay's imports.
        argv = [python_executable(), "-I", str(relay), str(path)]
        env = clean_environment()
        env["PYTHONIOENCODING"] = "utf-8"
        stdin = prompt if spec.resume_session else worker_note(spec) + prompt
        warnings = [] if setup.get("bundle_path") else ["the Claude setup was not packed; the server job runs without your skills and settings"]
        return Launch(argv=argv, env=env, stdin=stdin, session_id=spec.resume_session, warnings=warnings)

    def new_parser(self) -> RemoteParser:
        return RemoteParser()


def worker_note(spec: JobSpec) -> str:
    """What a local Claude worker gets as appended system prompt, given here as the first message's preamble."""

    note = WORKER_NOTE + REMOTE_NOTE + (READ_NOTE if spec.access == "read" else "") + WORKER_TOOLS_NOTE
    found = [name for name in PROJECT_INSTRUCTIONS if (Path(spec.cwd) / name).is_file()]
    if found:
        note += (" The project's own instructions are in " + ", ".join(found)
                 + "; read them with pc_read before you start and follow them.")
    return f"<csa_worker_note>\n{note.strip()}\n</csa_worker_note>\n\n"


def history_jobs(spec: JobSpec) -> list[str]:
    """Earlier jobs of this conversation, oldest first, then this one: the relay's transcript sources."""

    chain = [spec.job_id]
    current = spec.resume_job
    while current and current not in chain and len(chain) < 50:
        chain.append(current)
        current = (read_json(jobs_dir() / current / "spec.json", {}) or {}).get("resume_job")
    return list(reversed(chain))
