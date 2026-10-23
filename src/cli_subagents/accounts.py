"""Account pools: usage state, cooldowns, per-account job slots and selection."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import time
from typing import Any, BinaryIO

from cli_subagents.config import Account, QuotaPool, RuntimeConfig, state_dir
from cli_subagents.plans import account_cap, account_limits, model_allowed
from cli_subagents.fsutil import file_lock, read_json, same_process, try_lock, unlock, write_json

# Default for `usage_ceiling`: above this utilization (0..1) an account gets no new jobs
# (admission) and sorts after every other account (ranking).
NEARLY_FULL = 0.95
MAX_COOLDOWN_SECONDS = 7 * 86400
RETRY_AFTER_RE = re.compile(
    r"retry[- ]after\b[\s:=]*(\d+(?:\.\d+)?)\s*(s|sec|secs|seconds?|m|min|mins|minutes?|h|hours?)?\b", re.IGNORECASE
)


def _usage_path(runtime: str):
    return state_dir() / f"{runtime}-usage.json"


def load_usage(runtime: str) -> dict[str, dict[str, Any]]:
    data = read_json(_usage_path(runtime), {})
    return data if isinstance(data, dict) else {}


def update_usage(runtime: str, account: str, **fields: Any) -> dict[str, Any]:
    """Merge fields into one account's usage record under a lock."""

    path = _usage_path(runtime)
    with file_lock(path.with_suffix(".lock")):
        data = load_usage(runtime)
        record = dict(data.get(account) or {})
        record.update({key: value for key, value in fields.items() if value is not None})
        record["updated"] = time.time()
        data[account] = record
        write_json(path, data)
    return record


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def record_rate_limit_event(runtime: str, account: str, info: dict[str, Any]) -> None:
    """Store the windows reported by a `rate_limit_event` (Claude) or equivalent."""

    windows: dict[str, Any] = {}
    unified = info.get("unifiedWindows")
    if isinstance(unified, dict):
        for name, window in unified.items():
            if isinstance(window, dict):
                windows[str(name)] = {
                    "utilization": _number(window.get("utilization")),
                    "resets_at": _number(window.get("resetsAt")),
                }
    elif info.get("rateLimitType"):
        windows[str(info["rateLimitType"])] = {
            "utilization": _number(info.get("utilization")),
            "resets_at": _number(info.get("resetsAt")),
        }
    # Only usage figures are stored here. A cooldown comes from the runner, and only
    # when an attempt actually ended with a usage limit.
    update_usage(runtime, account, status=info.get("status"), windows=windows or None)


def retry_after_seconds(text: str) -> float | None:
    """A Retry-After value in error text: "Retry-After: 120" or "retry after 5 minutes"."""

    match = RETRY_AFTER_RE.search(text or "")
    if not match:
        return None
    unit = (match.group(2) or "s").lower()
    scale = 3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1
    return float(match.group(1)) * scale


def cooldown_end(error_text: str, resets_at: float | None, cooldown_minutes: int, now: float | None = None) -> float:
    """When an account that hit its limit is usable again.

    In order (OpenCodex's shape): a Retry-After value in the error text, then the
    reset time the CLI reported, then the configured cooldown; never more than 7 days.
    """

    now = now or time.time()
    retry = retry_after_seconds(error_text)
    if retry is not None and retry > 0:
        until = now + retry
    elif resets_at and resets_at > now:
        until = float(resets_at)
    else:
        until = now + cooldown_minutes * 60
    return min(until, now + MAX_COOLDOWN_SECONDS)


def set_cooldown(runtime: str, account: str, until: float, reason: str) -> None:
    update_usage(runtime, account, cooldown_until=until, cooldown_reason=reason)


def pool_cooldown_until(record: dict[str, Any] | None, pool: str, now: float | None = None) -> float | None:
    return cooldown_until(((record or {}).get("pools") or {}).get(pool), now)


def set_pool_cooldown(runtime: str, account: str, pool: str, until: float, reason: str) -> None:
    path = _usage_path(runtime)
    with file_lock(path.with_suffix(".lock")):
        data = load_usage(runtime)
        record = data.setdefault(account, {})
        record.setdefault("pools", {}).setdefault(pool, {}).update(cooldown_until=until, cooldown_reason=reason)
        record["updated"] = time.time()
        write_json(path, data)


def clear_cooldown(runtime: str, account: str) -> bool:
    """Forget an account's cooldown. Returns True if it had one."""

    path = _usage_path(runtime)
    with file_lock(path.with_suffix(".lock")):
        data = load_usage(runtime)
        record = data.get(account)
        if not isinstance(record, dict) or ("cooldown_until" not in record and not record.get("pools")):
            return False
        record.pop("cooldown_until", None)
        record.pop("cooldown_reason", None)
        record.pop("pools", None)
        record["updated"] = time.time()
        write_json(path, data)
    return True


def max_utilization(record: dict[str, Any] | None) -> float | None:
    if not record:
        return None
    values = [
        float(window["utilization"])
        for window in (record.get("windows") or {}).values()
        if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float))
    ]
    return max(values) if values else None


def cooldown_until(record: dict[str, Any] | None, now: float | None = None) -> float | None:
    now = now or time.time()
    until = (record or {}).get("cooldown_until")
    return float(until) if isinstance(until, (int, float)) and until > now else None


def _owner_path(lock_path: Path) -> Path:
    return lock_path.with_name(lock_path.stem + ".owner.json")


@dataclass
class Lease:
    runtime: str
    account: Account
    handle: BinaryIO
    path: Path | None = None  # the slot's lock file

    pool_slots: list[tuple[BinaryIO, Path]] = field(default_factory=list)

    def set_owner(self, pid: int, started: int | None, job_id: str | None = None) -> None:
        """Record the worker process, so the slot stays busy if the runner dies before it."""

        paths = ([self.path] if self.path is not None else []) + [path for _handle, path in self.pool_slots]
        for path in paths:
            write_json(_owner_path(path), {"pid": pid, "started": started, "job_id": job_id})

    def release(self) -> None:
        try:
            if self.path is not None:
                _owner_path(self.path).unlink(missing_ok=True)
        except OSError:
            pass
        finally:
            unlock(self.handle)
            for handle, path in self.pool_slots:
                try:
                    _owner_path(path).unlink(missing_ok=True)
                except OSError:
                    pass
                finally:
                    unlock(handle)
            self.pool_slots.clear()


def _home_key(home: Path) -> str:
    """A short key for an account folder, the same for every name that points to it."""

    try:
        resolved = str(Path(home).expanduser().resolve())
    except (OSError, RuntimeError):
        resolved = os.path.abspath(str(home))
    return hashlib.sha256(os.path.normcase(resolved).encode("utf-8")).hexdigest()[:16]


def _slot_path(runtime: RuntimeConfig, account: Account, slot: int) -> Path:
    # Keyed on the folder, not the name: two names for one folder share its slots.
    return state_dir() / "leases" / runtime.runtime / _home_key(account.home) / f"{slot}.lock"


def _orphan_alive(lock_path: Path) -> bool:
    """True if the slot's recorded worker still runs (its runner died without cleaning up)."""

    owner = read_json(_owner_path(lock_path))
    if not isinstance(owner, dict):
        return False
    try:
        pid = int(owner.get("pid") or 0)
        started = owner.get("started")
        return same_process(pid, int(started) if started is not None else None)
    except (TypeError, ValueError):
        return False


def _take_slot(lock_path: Path) -> BinaryIO | None:
    """Lock a free slot. A slot whose recorded worker still runs counts as busy."""

    handle = try_lock(lock_path)
    if handle is None:
        return None
    if _orphan_alive(lock_path):
        unlock(handle)
        return None
    try:
        _owner_path(lock_path).unlink(missing_ok=True)  # stale record of a finished worker
    except OSError:
        pass
    return handle


def _take_capped_slot(folder: Path, cap: int, *, override: bool = False) -> tuple[BinaryIO, Path] | None:
    """Lease a slot, counting overflow slots too. Only override may exceed the cap.

    Serialize selection so a normal job cannot use a gap while overflow jobs
    still occupy the full allowance. Owner records keep orphaned workers counted.
    """

    available = None
    with file_lock(folder / "selection.lock", timeout=30):
        indices = sorted(int(path.stem) for path in folder.glob("*.lock") if path.stem.isdecimal())
        busy = 0
        try:
            for index in indices:
                path = folder / f"{index}.lock"
                handle = _take_slot(path)
                if handle is None:
                    busy += 1
                elif available is None:
                    available = (handle, path)
                else:
                    unlock(handle)
            if not override and busy >= cap:
                return None
            if available is not None:
                result, available = available, None
                return result
            index = next((i for i, used in enumerate(indices) if i != used), len(indices))
            path = folder / f"{index}.lock"
            handle = _take_slot(path)
            return (handle, path) if handle is not None else None
        finally:
            if available is not None:
                unlock(available[0])


def try_lease(runtime: RuntimeConfig, account: Account, *, override: str | None = None) -> Lease | None:
    """Take one of the account's job slots, or None if all are busy."""

    slot = _take_capped_slot(_slot_path(runtime, account, 0).parent, account_cap(runtime, account), override=bool(override))
    return Lease(runtime.runtime, account, *slot) if slot is not None else None


def _lease_pool(lease: Lease, runtime: RuntimeConfig, pool: QuotaPool, *, override: str | None = None) -> bool:
    root = state_dir() / "leases" / runtime.runtime / "pools" / pool.name
    for folder, cap in ((root / _home_key(lease.account.home), pool.per_account), (root / "total", pool.total)):
        if cap is None:
            continue
        slot = _take_capped_slot(folder, cap, override=bool(override))
        if slot is None:
            lease.release()
            return False
        lease.pool_slots.append(slot)
    return True


def busy_slots(runtime: RuntimeConfig, account: Account) -> int:
    busy = 0
    folder = _slot_path(runtime, account, 0).parent
    for path in folder.glob("*.lock"):
        if not path.stem.isdecimal():
            continue
        handle = try_lock(path)
        if handle is None:
            busy += 1
            continue
        if _orphan_alive(path):
            busy += 1
        unlock(handle)
    return busy


def ranked_accounts(
    runtime: RuntimeConfig,
    *,
    pinned: str | None = None,
    exclude: set[str] | None = None,
    prefer: str | None = None,
    model: str | None = None,
    pool: QuotaPool | None = None,
    now: float | None = None,
) -> tuple[list[Account], float | None]:
    """Usable accounts best-first, plus the earliest cooldown end if none are usable."""

    now = now or time.time()
    usage = load_usage(runtime.runtime)
    candidates = runtime.worker_accounts()
    if pinned:
        account = runtime.find(pinned)
        if account is None:
            raise ValueError(f"no {runtime.runtime} account named '{pinned}'")
        candidates = [account]
    exclude = exclude or set()
    usable: list[Account] = []
    earliest: float | None = None
    for account in candidates:
        if account.name in exclude:
            continue
        if not account.enabled:
            continue
        if model and not model_allowed(runtime, account_limits(runtime, account), model):
            continue  # the account's model list does not include it
        record = usage.get(account.name)
        ends = [cooldown_until(record, now), pool_cooldown_until(record, pool.name, now) if pool else None]
        until = max((end for end in ends if end is not None), default=None)
        if until is not None:
            earliest = until if earliest is None else min(earliest, until)
            continue
        usable.append(account)

    def key(account: Account) -> tuple[bool, bool, bool, float, float]:
        record = usage.get(account.name) or {}
        utilization = max_utilization(record)
        nearly_full = utilization is not None and utilization >= runtime.usage_ceiling
        return (
            account.fallback,  # fallback accounts only after every regular one
            nearly_full,
            account.name != prefer,
            utilization if utilization is not None else 0.0,
            float(record.get("last_used") or 0.0),
        )

    usable.sort(key=key)
    return usable, (earliest if not usable else None)


def _acquire(
    runtime: RuntimeConfig,
    *,
    pinned: str | None = None,
    exclude: set[str] | None = None,
    prefer: str | None = None,
    model: str | None = None,
    job_id: str | None = None,
    override: str | None = None,
) -> tuple[Lease | None, float | None, bool]:
    """Lease the best free account.

    Returns (lease, earliest_cooldown_end, all_busy). A None lease with
    all_busy=True means accounts exist but every slot is taken right now.
    """

    from cli_subagents import limits, quota, spend

    pool = quota.pool_for(runtime, model)
    state = quota.pool_state(runtime, limits.count_active(), time.time(), exclude_job=job_id) if pool else None
    accounts, earliest = ranked_accounts(
        runtime, pinned=pinned, exclude=exclude, prefer=prefer, model=model or runtime.default_model, pool=pool
    )
    if not override and pool and pool.total is not None and state.executing_totals.get(pool.name, 0) >= pool.total:
        return None, earliest, bool(accounts)
    budget_blocked = 0
    spend_blocked = []
    for account in accounts:
        if spend.configured(account):
            summary = spend.snapshot(runtime, account)
            if summary["remaining_usd"] < spend.MIN_BUDGET_USD:
                budget_blocked += 1
                spend_blocked.append(spend.refusal(account, summary))
                continue
        if not override and pool and pool.weekly_jobs is not None and state.budget_used(account.name, pool.name) >= pool.weekly_jobs:
            budget_blocked += 1
            continue
        if not override and pool and pool.per_account is not None and state.executing.get((account.name, pool.name), 0) >= pool.per_account:
            continue
        lease = try_lease(runtime, account, override=override)
        if lease is not None:
            if pool and not _lease_pool(lease, runtime, pool, override=override):
                continue
            if pool and job_id:
                from cli_subagents.jobs import update_status

                try:
                    update_status(job_id, account=None, assigned_account=account.name, pool=pool.name)
                except OSError:
                    lease.release()
                    raise
            update_usage(runtime.runtime, account.name, last_used=time.time())
            return lease, None, False
    if spend_blocked and len(accounts) == len(spend_blocked):
        raise spend.SpendLimitError(" ".join(spend_blocked))
    return None, earliest, len(accounts) > budget_blocked


def acquire(runtime: RuntimeConfig, **kwargs: Any) -> tuple[Lease | None, float | None, bool]:
    if runtime.pools:
        # Share admission's lock: a lease and a new job cannot both reserve the last weekly start.
        with file_lock(state_dir() / "admission.lock", timeout=30):
            return _acquire(runtime, **kwargs)
    return _acquire(runtime, **kwargs)
