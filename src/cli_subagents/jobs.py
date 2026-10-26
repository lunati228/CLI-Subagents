"""Job records on disk: create, start, inspect, wait, cancel, list, clean up.

One folder per job under `~/.cli-subagents/jobs/<job_id>/`:

    spec.json      what to run (written once)
    status.json    live state, written atomically by the runner
    events.jsonl   raw events from the worker CLI
    result.md      final answer text
    runner.log     runner stdout/stderr (diagnostics)
    cancel         marker file; its presence asks the runner to stop
    notify.pending the result waits for delivery; renamed to notify.claimed
                   by whoever delivers it first (hook, subagent_wait, subagent_result)
    notify.lock    held while either marker changes
"""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time
from typing import Any

from cli_subagents import limits, quota
from cli_subagents.config import RUNTIMES, jobs_dir, load_config, state_dir
from cli_subagents.fsutil import (
    file_lock,
    kill_tree,
    make_private_dir,
    pid_alive,
    python_executable,
    read_json,
    read_text,
    same_process,
    spawn_detached,
    write_json,
    write_text,
)
from cli_subagents.hosts import detect_host, host_context
from cli_subagents.runtimes import get_runtime
from cli_subagents.runtimes.base import ACCESS_LEVELS, CANCELLED, ERROR, LOST, JobSpec

ACTIVE_STATES = ("queued", "starting", "running")
JOB_ID_CHARS = set("0123456789abcdefghijklmnopqrstuvwxyz-")
NOTIFY_PENDING = "notify.pending"
NOTIFY_CLAIMED = "notify.claimed"
NOTIFY_LOCK = "notify.lock"


def job_dir(job_id: str) -> Path:
    if not job_id or set(job_id) - JOB_ID_CHARS or len(job_id) > 64:
        raise ValueError(f"invalid job id: {job_id!r}")
    return jobs_dir() / job_id


def new_job_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


def create_job(**kwargs: Any) -> dict[str, Any]:
    """Validate a request, check the limits, write the job record and start its runner.

    Raises limits.LimitError (a ValueError) when a limit says the job can't start now.
    Counting and starting happen under one lock, so two callers can't both take the last slot.
    """

    if not kwargs.get("start", True):
        return _create_job(**kwargs)
    with file_lock(state_dir() / "admission.lock", timeout=30):
        return _create_job(**kwargs)


def _create_job(
    *,
    runtime: str,
    prompt: str,
    cwd: str,
    account: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    override: str | None = None,
    access: str = "edit",
    max_turns: int | None = None,
    timeout_seconds: int = 1800,
    add_dirs: list[str] | None = None,
    resume_job: str | None = None,
    host: str | None = None,
    start: bool = True,
) -> dict[str, Any]:
    limits.validate_override(override)
    config = load_config()
    runtime_config = config.runtime(runtime)
    if runtime == "opencode" and not get_runtime(runtime).find_binary(runtime_config):
        raise ValueError("OpenCode is disabled or not installed; install OpenCode v2 (@opencode/cli) to use this worker")
    depth = int(os.environ.get("CLI_SUBAGENTS_DEPTH", "0") or 0)
    if depth >= config.max_depth:
        raise ValueError(
            "this session is itself a CLI Subagents worker; nested delegation is disabled "
            f"(max_depth = {config.max_depth})"
        )
    if not prompt or not prompt.strip():
        raise ValueError("prompt is empty")
    if access not in ACCESS_LEVELS:
        raise ValueError(f"access must be one of {ACCESS_LEVELS}")
    path = Path(cwd).expanduser()
    if not path.is_absolute() or not path.is_dir():
        raise ValueError("cwd must be an existing absolute directory")
    if path.resolve() == Path(path.anchor).resolve() or path.resolve() == Path.home().resolve():
        raise ValueError("cwd cannot be a drive root or your home folder; use a project folder")
    timeout_seconds = int(timeout_seconds)
    if not 30 <= timeout_seconds <= 6 * 3600:
        raise ValueError("timeout_seconds must be between 30 and 21600")
    directories = []
    for item in add_dirs or []:
        directory = Path(item).expanduser()
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError(f"add_dirs entry is not an existing absolute folder: {item}")
        directories.append(str(directory))

    resume_session = None
    resume_account = None
    if resume_job:
        previous = read_status(resume_job)
        closed = read_json(job_dir(resume_job) / "inbox-closed.json", {}) or {}
        if previous.get("state") in ACTIVE_STATES:
            if not closed.get("session_id"):
                raise ValueError(
                    f"job {resume_job} is still running; wait for it to finish, or send it a follow-up with subagent_message"
                )
        resume_session = closed.get("session_id") or previous.get("session_id")
        if not resume_session:
            raise ValueError(f"job {resume_job} has no session to continue")
        if previous.get("runtime") != runtime:
            raise ValueError("resume_job belongs to a different runtime")
        resume_account = closed.get("account") or previous.get("account")
        resume_config = runtime_config.find(resume_account) if resume_account else None
        if resume_config and resume_config.explicit_only and account != resume_account:
            raise ValueError(f"Resuming explicit-only account '{resume_account}' requires account='{resume_account}'")
        if not hasattr(get_runtime(runtime), "transfer_session"):
            # The session lives in one account's folder and can't be moved: stay there.
            account = resume_account or account

    admission = limits.admit(config, runtime, account=account, model=model, effort=effort, override=override) if start else None
    if admission is not None:
        effort = admission.effort

    model = model or runtime_config.default_model
    pool = quota.pool_for(runtime_config, model)
    spec = JobSpec(
        job_id=new_job_id(),
        runtime=runtime,
        prompt=prompt,
        cwd=str(path.resolve()),
        account=account or None,
        model=model or None,
        effort=effort or None,
        override=override,
        access=access,
        max_turns=int(max_turns) if max_turns else None,
        timeout_seconds=timeout_seconds,
        add_dirs=directories,
        resume_session=resume_session,
        resume_account=resume_account,
        parent=host_context(host or detect_host(), str(path.resolve())),
        depth=depth + 1,
        created=time.time(),
    )
    folder = job_dir(spec.job_id)
    make_private_dir(folder.parent)
    folder.mkdir(mode=0o700)
    write_json(folder / "spec.json", spec.to_dict())
    status = {
        "job_id": spec.job_id,
        "runtime": runtime,
        "override": override,
        "state": "queued",
        "outcome": None,
        "cwd": spec.cwd,
        "account": None,
        "assigned_account": admission.account if admission else account,
        "pool": pool.name if pool else None,
        "created_at": spec.created,
        "updated_at": spec.created,
        "attempts": [],
        "warnings": [],
        # The runner rewrites "warnings"; this key survives, and summarize() shows it with them.
        "limit_notes": list(admission.warnings) if admission else [],
    }
    write_json(folder / "status.json", status)
    if start:
        try:
            pid = spawn_detached(
                [python_executable(), "-m", "cli_subagents", "_runner", spec.job_id],
                folder / "runner.log",
                env=_runner_env(),
            )
        except OSError as exc:
            mark_pending(spec.job_id)
            return update_status(
                spec.job_id, state="done", outcome=ERROR, message=f"could not start the runner: {exc}", finished_at=time.time()
            )
        # The runner owns status.json; the launcher only leaves its PID beside it.
        write_text(folder / "runner.pid", str(pid))
        status["runner_pid"] = pid
    return status


def _runner_env() -> dict[str, str]:
    env = dict(os.environ)
    package_root = str(Path(__file__).resolve().parents[1])
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = package_root + (os.pathsep + existing if existing else "")
    return env


def read_spec(job_id: str) -> JobSpec:
    data = read_json(job_dir(job_id) / "spec.json")
    if not isinstance(data, dict):
        raise ValueError(f"unknown job: {job_id}")
    return JobSpec.from_dict(data)


def read_status(job_id: str) -> dict[str, Any]:
    folder = job_dir(job_id)
    status = read_json(folder / "status.json")
    if not isinstance(status, dict):
        raise ValueError(f"unknown job: {job_id}")
    if status.get("state") in ACTIVE_STATES:
        pid = status.get("runner_pid") or _runner_pid(folder)
        stale = time.time() - float(status.get("updated_at") or 0) > 15
        if pid and stale and not pid_alive(int(pid)):
            status.update(state="done", outcome=LOST, message="the runner process stopped unexpectedly")
            _stop_orphan(status)
    return status


def _stop_orphan(status: dict[str, Any]) -> None:
    """Kill the worker a dead runner left behind, if it is still the same process."""

    pid = status.get("child_pid")
    started = status.get("child_started")
    try:
        if pid and same_process(int(pid), int(started) if started is not None else None):
            kill_tree(int(pid))
    except (TypeError, ValueError, OSError, subprocess.SubprocessError):
        pass


def _runner_pid(folder: Path) -> int | None:
    try:
        return int((folder / "runner.pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def update_status(job_id: str, **fields: Any) -> dict[str, Any]:
    folder = job_dir(job_id)
    status = read_json(folder / "status.json", {}) or {}
    status.update(fields)
    status["updated_at"] = time.time()
    write_json(folder / "status.json", status)
    return status


def mark_pending(job_id: str) -> None:
    """Record that the job's result waits for delivery. Written before the final status."""

    folder = job_dir(job_id)
    with file_lock(folder / NOTIFY_LOCK):
        if not (folder / NOTIFY_CLAIMED).exists():
            write_text(folder / NOTIFY_PENDING, str(time.time()))


def is_pending(job_id: str) -> bool:
    return (job_dir(job_id) / NOTIFY_PENDING).exists()


def claim_delivery(job_id: str) -> bool:
    """Take the one delivery of a finished result. True only for the first caller.

    Racing callers (the wake-up hook, subagent_wait, subagent_result) take turns
    under notify.lock. The rename alone is not enough: on Windows, callers that
    open notify.pending at the same moment can all rename it, the later ones
    "renaming" notify.claimed to itself. A finished job that never got a marker
    (its runner died) is claimed by creating notify.claimed exclusively.
    """

    folder = job_dir(job_id)
    if not folder.is_dir() or (folder / NOTIFY_CLAIMED).exists():
        return False  # no such job (the lock would create its folder), or delivered already
    try:
        with file_lock(folder / NOTIFY_LOCK):
            return _claim_locked(job_id, folder)
    except TimeoutError:
        return False


def _claim_locked(job_id: str, folder: Path) -> bool:
    try:
        os.rename(folder / NOTIFY_PENDING, folder / NOTIFY_CLAIMED)
        return True
    except FileNotFoundError:
        pass
    except OSError:
        return False  # notify.claimed already exists (Windows refuses to rename over it)
    if (folder / NOTIFY_CLAIMED).exists() or (folder / NOTIFY_PENDING).exists():
        return False
    try:
        if read_status(job_id).get("state") in ACTIVE_STATES:
            return False
        fd = os.open(folder / NOTIFY_CLAIMED, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except (ValueError, OSError):
        return False
    os.close(fd)
    return True


def wait_for(job_id: str, seconds: float) -> dict[str, Any]:
    """Block up to `seconds` until the job finishes; return its status."""

    deadline = time.monotonic() + max(0.0, seconds)
    status = read_status(job_id)
    while status.get("state") in ACTIVE_STATES and time.monotonic() < deadline:
        time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
        status = read_status(job_id)
    return status


def read_result(job_id: str, max_chars: int = 20000) -> dict[str, Any]:
    status = read_status(job_id)
    path = job_dir(job_id) / "result.md"
    text = read_text(path) if path.exists() else ""
    truncated = len(text) > max_chars
    return {
        **status,
        "result": text[:max_chars],
        "result_truncated": truncated,
        "result_path": str(path) if path.exists() else None,
        "events_path": str(job_dir(job_id) / "events.jsonl"),
    }


def cancel(job_id: str) -> dict[str, Any]:
    status = read_status(job_id)
    if status.get("outcome") == LOST:
        # The runner is gone; read_status already stopped its worker. Record the cancel.
        _stop_orphan(status)
        return update_status(
            job_id, state="done", outcome=CANCELLED, message="cancelled (the runner had stopped)",
            finished_at=time.time(), child_pid=None,
        )
    if status.get("state") not in ACTIVE_STATES:
        return status
    folder = job_dir(job_id)
    write_text(folder / "cancel", str(time.time()))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = read_status(job_id)
        if status.get("state") not in ACTIVE_STATES:
            return status
        time.sleep(0.3)
    # The runner didn't react; stop everything ourselves.
    _stop_orphan(status)
    runner_pid = status.get("runner_pid") or _runner_pid(folder)
    if runner_pid and pid_alive(int(runner_pid)):
        kill_tree(int(runner_pid))
    return update_status(job_id, state="done", outcome=CANCELLED, message="cancelled", finished_at=time.time())


def list_jobs(limit: int = 20) -> list[dict[str, Any]]:
    folder = jobs_dir()
    if not folder.is_dir():
        return []
    ids = sorted((path.name for path in folder.iterdir() if path.is_dir()), reverse=True)[:limit]
    jobs = []
    for job_id in ids:
        try:
            status = read_status(job_id)
        except ValueError:
            continue
        jobs.append(
            {key: status.get(key) for key in ("job_id", "runtime", "state", "outcome", "account", "created_at", "cwd", "override")}
        )
    return jobs


def clean_old_jobs(days: int) -> int:
    config = load_config()
    if any(pool.weekly_jobs is not None for runtime in config.runtimes.values() for pool in runtime.pools):
        days = max(days, 7)  # weekly budgets must keep the records they count
    folder = jobs_dir()
    if not folder.is_dir():
        return 0
    from cli_subagents import spend

    for runtime in RUNTIMES:
        spend.flush(runtime)
    cutoff = time.time() - days * 86400
    removed = 0
    for path in folder.iterdir():
        if not path.is_dir() or path.is_symlink():
            continue
        try:
            status = read_status(path.name)  # a job whose runner died reads as done/lost
        except ValueError:
            status = {}
        if status.get("state") in ACTIVE_STATES:
            continue
        if float(status.get("updated_at") or path.stat().st_mtime) < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    return removed
