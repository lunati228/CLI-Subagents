"""Persistent per-attempt spend and atomic reservations for account budgets.

The ledger lives outside job folders so retention cannot replenish an allowance.
Unknown costs consume the granted budget; we deliberately do not guess prices
from stream tokens (models, caching and provider billing can differ).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
import re
from typing import Any

from cli_subagents.config import NAME_RE, Account, RuntimeConfig, jobs_dir, state_dir
from cli_subagents.fsutil import file_lock, process_start_time, read_json, read_text, same_process, write_json
from cli_subagents.runtimes.base import AttemptResult

MIN_BUDGET_USD = 1.0


def configured(account: Account) -> bool:
    return account.spend_cap_usd is not None or bool(account.spend_caps)


def _caps(account: Account) -> list[tuple[float, str]]:
    if account.spend_caps:
        return [(cap.amount_usd, cap.window) for cap in account.spend_caps]
    return [(account.spend_cap_usd, account.spend_period)] if account.spend_cap_usd is not None else []


class SpendLimitError(ValueError):
    """A spend limit is binding; override never bypasses it."""


def _name(runtime: RuntimeConfig | str) -> str:
    return runtime.runtime if isinstance(runtime, RuntimeConfig) else runtime


def _path(runtime: RuntimeConfig | str):
    return state_dir() / f"{_name(runtime)}-spend.json"


def _cost(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _load(runtime: RuntimeConfig | str) -> dict[str, Any]:
    path = _path(runtime)
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "attempts": {}}
    except (ValueError, UnicodeError) as exc:
        raise SpendLimitError(f"Cannot read spend ledger {path}; repair it before starting capped jobs") from exc
    if not isinstance(ledger, dict) or ledger.get("version") != 1 or not isinstance(ledger.get("attempts"), dict):
        raise SpendLimitError(f"Invalid spend ledger {path}; repair it before starting capped jobs")
    for key, entry in ledger["attempts"].items():
        valid = isinstance(entry, dict)
        if valid:
            valid = (isinstance(entry.get("account"), str) and NAME_RE.fullmatch(entry["account"])
                     and entry.get("state") in ("reserved", "settled")
                     and type(entry.get("before_start")) is bool
                     and type(entry.get("started_at")) in (int, float) and _cost(entry["started_at"]) is not None)
        if valid and entry["state"] == "settled":
            valid = type(entry.get("ended_at")) in (int, float) and _cost(entry["ended_at"]) is not None
        if valid:
            valid = all(entry.get(field) is None or (type(entry[field]) in (int, float) and _cost(entry[field]) is not None)
                        for field in ("cost_usd", "budget_usd"))
        if not valid:
            raise SpendLimitError(f"Invalid attempt {key!r} in spend ledger {path}; repair it before starting capped jobs")
    return ledger


def _time(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()


def period(account: Account, now: float, window: str | None = None) -> tuple[float, float | None]:
    """UTC calendar bounds, with an optional later accounting start."""

    date = datetime.fromtimestamp(now, timezone.utc)
    midnight = date.replace(hour=0, minute=0, second=0, microsecond=0)
    window = window or account.spend_period
    rolling = re.fullmatch(r"([1-9][0-9]*)(h|d)", window)
    if rolling:
        seconds = int(rolling[1]) * (3600 if rolling[2] == "h" else 86400)
        begin, end = date - timedelta(seconds=seconds), None
    elif window == "day":
        begin, end = midnight, midnight + timedelta(days=1)
    elif window == "week":
        begin = midnight - timedelta(days=midnight.weekday())
        end = begin + timedelta(days=7)
    elif window == "month":
        begin = midnight.replace(day=1)
        end = begin.replace(year=begin.year + 1, month=1) if begin.month == 12 else begin.replace(month=begin.month + 1)
    elif window == "total":
        begin, end = datetime(1970, 1, 1, tzinfo=timezone.utc), None
    else:
        raise SpendLimitError(f"Unknown spend window {window!r}")
    start = begin.timestamp()
    if account.spend_since:
        start = max(start, _time(account.spend_since))
    return start, end.timestamp() if end else None


def _import_attempts(runtime: RuntimeConfig | str, ledger: dict[str, Any], now: float) -> bool:
    """Import existing history idempotently, including records from older csa versions."""

    changed = False
    for path in jobs_dir().glob("*/status.json"):
        try:
            status = json.loads(read_text(path))
        except FileNotFoundError:
            continue  # normal retention may have removed an already-persisted job
        except (ValueError, UnicodeError) as exc:
            spec = read_json(path.parent / "spec.json", {})
            if isinstance(spec, dict) and spec.get("runtime") not in (None, _name(runtime)):
                continue
            raise SpendLimitError(f"Cannot import spend from {path}; repair the job history before starting capped jobs or cleaning it") from exc
        if not isinstance(status, dict) or status.get("runtime") != _name(runtime):
            continue
        for index, attempt in enumerate(status.get("attempts") or []):
            if not isinstance(attempt, dict) or not attempt.get("account"):
                continue
            key = f"{path.parent.name}:{index}"
            previous = ledger["attempts"].get(key)
            # A terminal ledger write is authoritative. Import only legacy records or
            # recover a reservation after a runner died before its settlement write.
            if previous and previous.get("state") == "settled":
                continue
            if previous and (same_process(previous.get("runner_pid"), previous.get("runner_started"))
                             or same_process(previous.get("worker_pid"), previous.get("worker_started"))):
                continue  # only the live owner can atomically settle its reservation
            if previous and not attempt.get("ended_at"):
                continue
            entry = dict(previous or {})
            ended = attempt.get("ended_at")
            if not ended and status.get("state") not in ("queued", "starting", "running"):
                ended = status.get("finished_at") or status.get("updated_at") or now
            entry.update(
                account=attempt["account"], state="settled" if ended else "reserved",
                started_at=_cost(attempt.get("started_at") or attempt.get("attempted_at") or status.get("created_at")) or now,
                ended_at=(_cost(ended) if _cost(ended) is not None else now) if ended else None,
                cost_usd=_cost(attempt.get("cost_usd")),
                budget_usd=_cost(attempt.get("budget_usd", attempt.get("max_budget_usd"))) if not previous else previous.get("budget_usd"),
                before_start=attempt.get("before_start") is True,
            )
            if not ended:
                entry.update(runner_pid=status.get("runner_pid"), runner_started=status.get("runner_started"),
                             worker_pid=status.get("child_pid"), worker_started=status.get("child_started"))
            ledger["attempts"][key] = entry
            changed = True
    return changed


def _recover(ledger: dict[str, Any], now: float) -> bool:
    changed = False
    for entry in ledger["attempts"].values():
        if entry.get("state") != "reserved":
            continue
        if same_process(entry.get("runner_pid"), entry.get("runner_started")):
            continue
        if same_process(entry.get("worker_pid"), entry.get("worker_started")):
            continue
        # A runner may die just after spawning its worker. Keep the entire grant
        # debited even if no cost or worker PID ever reached disk.
        entry.update(state="settled", ended_at=now, cost_usd=None)
        changed = True
    return changed


def _amount(entry: dict[str, Any], account: Account) -> float:
    cost = _cost(entry.get("cost_usd"))
    if cost is not None:
        return cost
    if entry.get("before_start"):
        return 0.0
    budget = _cost(entry.get("budget_usd"))
    # Old costless attempts have no saved grant. A newly added cap must fail
    # closed for that unknown spend instead of claiming it was free.
    return budget if budget is not None else max((cap for cap, _ in _caps(account)), default=0.0)


def _window_snapshot(ledger: dict[str, Any], account: Account, cap: float | None,
                     window: str, now: float) -> dict[str, Any]:
    begin, end = period(account, now, window)
    rolling = bool(re.fullmatch(r"[1-9][0-9]*(h|d)", window))
    rolling_begin = now - int(window[:-1]) * (3600 if window[-1] == "h" else 86400) if rolling else None
    spent, reserved = 0.0, 0.0
    for entry in ledger["attempts"].values():
        if entry.get("account") != account.name:
            continue
        if entry.get("state") == "reserved":
            # Carry running commitments across calendar boundaries and manual resets.
            reserved += _amount(entry, account)
        else:
            when = entry.get("ended_at") or entry.get("started_at") or 0
            if (begin <= when <= now and (end is None or when < end)
                    and (rolling_begin is None or when > rolling_begin)):
                spent += _amount(entry, account)
    remaining = max(0.0, cap - spent - reserved) if cap is not None else None
    if remaining is not None and begin > now:
        remaining = 0.0  # a future reset must not make today's attempts unaccounted and unlimited
    return {
        "spent_usd": spent, "reserved_usd": reserved, "spend_cap_usd": cap,
        "remaining_usd": remaining,
        "spend_period": window, "window": window, "spend_since": account.spend_since,
        "period_start": begin, "period_end": end,
    }


def _snapshot(ledger: dict[str, Any], account: Account, now: float) -> dict[str, Any]:
    caps = [_window_snapshot(ledger, account, cap, window, now) for cap, window in _caps(account)]
    if not caps:
        return {**_window_snapshot(ledger, account, None, account.spend_period, now), "spend_caps": []}
    binding = min(caps, key=lambda cap: cap["remaining_usd"])
    return {**binding, "spend_caps": caps}


def _refresh(runtime: RuntimeConfig | str, ledger: dict[str, Any], now: float) -> None:
    changed = _import_attempts(runtime, ledger, now)
    changed = _recover(ledger, now) or changed
    if changed:
        write_json(_path(runtime), ledger)


def snapshot(runtime: RuntimeConfig | str, account: Account, now: float | None = None) -> dict[str, Any]:
    import time

    path = _path(runtime)
    with file_lock(path.with_suffix(".lock"), timeout=30):
        explicit_time = now is not None
        now = time.time() if now is None else now
        ledger = _load(runtime)
        _refresh(runtime, ledger, now)
        now = now if explicit_time else time.time()
        return _snapshot(ledger, account, now)


def flush(runtime: RuntimeConfig | str) -> None:
    """Persist legacy history before job retention removes its source records."""

    import time

    path = _path(runtime)
    with file_lock(path.with_suffix(".lock"), timeout=30):
        ledger = _load(runtime)
        _refresh(runtime, ledger, time.time())


def describe(summary: dict[str, Any]) -> str:
    caps = summary.get("spend_caps") or []
    if len(caps) > 1:
        return "; ".join(describe(cap) for cap in caps)
    if summary["spend_cap_usd"] is None:
        return ""
    reserved = f", ${summary['reserved_usd']:.2f} reserved" if summary["reserved_usd"] else ""
    return (f"${summary['spent_usd']:.2f} / ${summary['spend_cap_usd']:.2f} {summary['spend_period']}"
            f"{reserved}, ${summary['remaining_usd']:.2f} remaining")


def refusal(account: Account, summary: dict[str, Any]) -> str:
    start = datetime.fromtimestamp(summary["period_start"], timezone.utc).isoformat()
    return (f"Account '{account.name}' spend cap for {summary['spend_period']}: {describe(summary)} (UTC period from {start}); "
            f"at least ${MIN_BUDGET_USD:.2f} is needed for a new attempt. "
            "Wait for running reservations to settle or the window to replenish, or change spend_caps, spend_cap_usd, "
            "spend_period or spend_since on the account in config.toml. Override cannot bypass a spend cap.")


def reserve(runtime: RuntimeConfig | str, account: Account, job_id: str, attempt_index: int,
            now: float | None = None) -> float | None:
    import time

    if configured(account) and _name(runtime) != "claude":
        raise SpendLimitError("Spend caps are supported only for Claude until this runtime can enforce a dollar budget")
    path = _path(runtime)
    with file_lock(path.with_suffix(".lock"), timeout=30):
        explicit_time = now is not None
        now = time.time() if now is None else now
        ledger = _load(runtime)
        _refresh(runtime, ledger, now)
        now = now if explicit_time else time.time()
        summary = _snapshot(ledger, account, now)
        key = f"{job_id}:{attempt_index}"
        if key in ledger["attempts"]:
            raise SpendLimitError(f"Attempt {key} already has a spend record")
        budget = summary["remaining_usd"]
        if budget is not None:
            # Round down so formatting for the CLI cannot grant more than the remainder.
            budget = math.floor(budget * 1_000_000) / 1_000_000
            if budget < MIN_BUDGET_USD:
                raise SpendLimitError(refusal(account, summary))
        ledger["attempts"][key] = {
            "account": account.name, "state": "reserved", "started_at": now,
            "budget_usd": budget, "cost_usd": None, "before_start": False,
            "runner_pid": os.getpid(), "runner_started": process_start_time(os.getpid()),
        }
        write_json(path, ledger)
        return budget


def started(runtime: RuntimeConfig | str, job_id: str, attempt_index: int, pid: int, stamp: int | None) -> None:
    path = _path(runtime)
    with file_lock(path.with_suffix(".lock"), timeout=30):
        ledger = _load(runtime)
        ledger["attempts"][f"{job_id}:{attempt_index}"].update(worker_pid=pid, worker_started=stamp)
        write_json(path, ledger)


def settle(runtime: RuntimeConfig | str, account: Account, job_id: str, attempt_index: int,
           result: AttemptResult, now: float | None = None) -> None:
    import time

    path = _path(runtime)
    with file_lock(path.with_suffix(".lock"), timeout=30):
        now = time.time() if now is None else now
        ledger = _load(runtime)
        key = f"{job_id}:{attempt_index}"
        entry = ledger["attempts"].get(key)
        if entry is None:
            entry = {"account": account.name, "started_at": now, "budget_usd": None}
            ledger["attempts"][key] = entry
        entry.update(state="settled", ended_at=now, cost_usd=_cost(result.cost_usd), before_start=result.before_start)
        write_json(path, ledger)
