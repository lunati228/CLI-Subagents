"""Optional quota steps and model pools. All figures come from local records."""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase
import time
from typing import Any

from cli_subagents.config import QuotaPool, RuntimeConfig, jobs_dir
from cli_subagents.fsutil import read_json
from cli_subagents.plans import account_limits, model_allowed

WEEK = 7 * 86400


def pool_for(runtime: RuntimeConfig, model: str | None) -> QuotaPool | None:
    chosen = model or runtime.default_model
    if not chosen:
        return None
    return next((pool for pool in runtime.pools if any(fnmatchcase(chosen, glob) for glob in pool.models)), None)


def job_pool(runtime: RuntimeConfig, status: dict, spec: dict) -> str | None:
    if status.get("pool"):
        return str(status["pool"])
    pool = pool_for(runtime, spec.get("model"))
    return pool.name if pool else None


def room_cap(runtime: RuntimeConfig, now: float, model: str | None = None,
             pinned: str | None = None) -> tuple[int | None, float | None]:
    """Cap and best remaining percent on regular, eligible accounts; unknown uses the top step.

    A job pinned to one account is judged by that account alone, fallback accounts included:
    a free fallback account with room must not inherit the main account's empty week.
    """

    from cli_subagents import accounts
    from cli_subagents.limits import utilization

    if not runtime.max_jobs_by_room:
        return None, None
    usage = accounts.load_usage(runtime.runtime)
    remaining = []
    for account in runtime.worker_accounts() if pinned is None else [runtime.find(pinned)]:
        if account is None:
            continue
        if (pinned is None and account.fallback) or not model_allowed(runtime, account_limits(runtime, account), model):
            continue
        record = usage.get(account.name)
        if accounts.cooldown_until(record, now):
            continue
        used = utilization(record, now)
        if used is not None:
            remaining.append(round(max(0.0, min(100.0, (1 - used) * 100)), 10))
    left = max(remaining) if remaining else None
    if left is None:
        return int(runtime.max_jobs_by_room[0][1]), None
    for threshold, cap in runtime.max_jobs_by_room:
        if left > threshold:
            return int(cap), left
    return int(runtime.max_jobs_by_room[-1][1]), left


@dataclass
class PoolState:
    running: dict[tuple[str, str], int] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=dict)
    executing: dict[tuple[str, str], int] = field(default_factory=dict)
    executing_totals: dict[str, int] = field(default_factory=dict)
    weekly: dict[tuple[str, str], int] = field(default_factory=dict)
    reserved: dict[tuple[str, str], int] = field(default_factory=dict)

    def budget_used(self, account: str, pool: str) -> int:
        key = (account, pool)
        return self.weekly.get(key, 0) + self.reserved.get(key, 0)


def pool_state(runtime: RuntimeConfig, counts: Any, now: float, exclude_job: str | None = None) -> PoolState:
    """Count jobs once per account and pool, including every account used during failover.

    Active jobs that have not made an attempt reserve a weekly start. Finished jobs
    count only if they started. Refused jobs never count.
    """

    state = PoolState()
    if not runtime.pools:
        return state
    active = {status["job_id"]: (status, spec) for status, spec in counts.records
              if status.get("runtime") == runtime.runtime and status.get("job_id") != exclude_job}
    started: dict[str, set[tuple[str, str]]] = {}
    folder = jobs_dir()
    for path in folder.iterdir() if folder.is_dir() else []:
        if not path.is_dir() or path.name == exclude_job:
            continue
        status = read_json(path / "status.json")
        if not isinstance(status, dict) or status.get("runtime") != runtime.runtime or status.get("outcome") in ("refused", "limit"):
            continue
        spec = read_json(path / "spec.json", {})
        if not isinstance(spec, dict):
            spec = {}
        pool = job_pool(runtime, status, spec)
        entries = status.get("attempts") or []
        if not entries and status.get("account") and status.get("state") in ("running", "done") and status.get("started_at"):
            entries = [status]
        keys = set()
        for attempt in entries:
            when = attempt.get("started_at")
            account = attempt.get("account")
            attempt_pool = attempt.get("pool") or pool
            if (not attempt.get("before_start") and account and attempt_pool and isinstance(when, (int, float))
                    and now - WEEK < when <= now and attempt.get("outcome") not in ("refused", "limit")):
                keys.add((str(account), str(attempt_pool)))
        started[path.name] = keys
        for key in keys:
            state.weekly[key] = state.weekly.get(key, 0) + 1
    for job_id, (status, spec) in active.items():
        pool = job_pool(runtime, status, spec)
        if not pool:
            continue
        state.totals[pool] = state.totals.get(pool, 0) + 1
        if status.get("state") == "running":
            state.executing_totals[pool] = state.executing_totals.get(pool, 0) + 1
        account = status.get("account") or status.get("assigned_account") or spec.get("account")
        if account:
            key = (str(account), pool)
            state.running[key] = state.running.get(key, 0) + 1
            if status.get("state") == "running":
                state.executing[key] = state.executing.get(key, 0) + 1
            if key not in started.get(job_id, set()):
                state.reserved[key] = state.reserved.get(key, 0) + 1
    return state


def pool_report(runtime: RuntimeConfig, account: str, state: PoolState, record: dict, now: float) -> list[dict]:
    from cli_subagents import accounts

    reports = []
    for pool in runtime.pools:
        key = (account, pool.name)
        running, weekly = state.running.get(key, 0), state.weekly.get(key, 0)
        text = f"{pool.name.capitalize()}: {running} running"
        if pool.per_account is not None:
            text += f" of {pool.per_account}"
        if pool.weekly_jobs is not None:
            text += f", {weekly} of {pool.weekly_jobs} this week"
        until = accounts.pool_cooldown_until(record, pool.name, now)
        if until:
            text += f", cooling down until {time.strftime('%Y-%m-%d %H:%M', time.localtime(until))}"
        data = {"name": pool.name, "models": pool.models, "running_jobs": running,
                "weekly_started": weekly, "weekly_reserved": state.reserved.get(key, 0), "state": text}
        for field_name in ("per_account", "total", "weekly_jobs", "cooldown_minutes"):
            value = getattr(pool, field_name)
            if value is not None:
                data[field_name] = value
        data["total_running"] = state.totals.get(pool.name, 0)
        if pool.total is not None:
            data["state"] += f", {data['total_running']} of {pool.total} across accounts"
        if until:
            data["cooldown_until"] = until
        reports.append(data)
    return reports
