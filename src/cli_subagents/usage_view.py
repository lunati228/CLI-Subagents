"""Usage that is already known, shown as one line, plus a free refresh for Codex.

Nothing here calls a model or starts a job. Codex usage is read from the newest rollout
file in each Codex account's home (only its last `rate_limits` object). Claude usage
comes from the rate-limit events of jobs that already ran. Antigravity exposes none.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from cli_subagents import accounts, quota, spend
from cli_subagents.config import Config, RuntimeConfig
from cli_subagents.limits import NAMES, utilization

STALE_AFTER_SECONDS = 1800
MAX_ROLLOUTS_TRIED = 5
WINDOW_LABELS = {"five_hour": "5h", "seven_day": "week", "primary": "5h", "secondary": "week"}


def newest_rollouts(home: Path, limit: int = MAX_ROLLOUTS_TRIED) -> list[Path]:
    """Newest rollout files first: <home>/sessions/YYYY/MM/DD/rollout-*.jsonl, newest day folders first."""

    found: list[Path] = []
    root = home / "sessions"
    try:
        for year in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
            for month in sorted((p for p in year.iterdir() if p.is_dir()), reverse=True):
                for day in sorted((p for p in month.iterdir() if p.is_dir()), reverse=True):
                    files = sorted(day.glob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
                    found.extend(files)
                    if len(found) >= limit:
                        return found[:limit]
    except OSError:
        pass
    return found[:limit]


def refresh_codex(config: Config) -> None:
    """Pick up Codex usage from each account's newest rollout file (also the user's own interactive use)."""

    from cli_subagents.runtimes.codex import last_rate_limits, usage_windows

    runtime = config.runtime("codex")
    usage = accounts.load_usage("codex")
    for account in runtime.listed_accounts():
        try:
            for path in newest_rollouts(account.home):
                limits = last_rate_limits(path)
                windows = usage_windows(limits) if limits else {}
                if not windows:
                    continue
                observed = path.stat().st_mtime
                record = usage.get(account.name) or {}
                if observed > float(record.get("observed_at") or record.get("updated") or 0):
                    accounts.update_usage("codex", account.name, windows=windows, observed_at=observed)
                break
        except Exception:
            continue  # a convenience: never fail a start over an unreadable file
    # TODO: Antigravity (agy) exposes no usage; see plans.md 3f.


def _age(record: dict[str, Any], now: float) -> float:
    return now - float(record.get("observed_at") or record.get("updated") or now)


def _hours(seconds: float) -> str:
    return f"{round(seconds / 60)} min" if seconds < 5400 else f"{round(seconds / 3600)} h"


def reset_text(resets_at: float, now: float) -> str:
    """When a usage window resets, in local time, e.g. '2026-10-18 01:26 (in 4 h 15 min)'."""

    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(resets_at))
    if resets_at <= now:
        return f"{when} (passed; that window's figure is out of date)"
    days, rest = divmod(round((resets_at - now) / 60), 1440)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"{when} (in {days} d {hours} h)"
    return f"{when} (in {hours} h {minutes} min)" if hours else f"{when} (in {minutes} min)"


def _account_text(runtime: RuntimeConfig, account: Any, record: dict[str, Any], now: float) -> str | None:
    cooling = accounts.cooldown_until(record, now)
    tags = []
    if account.fallback:
        tags.append("fallback")
    if account.explicit_only:
        tags.append("explicit-only")
    if not account.enabled:
        tags.append("disabled")
    tag = (" " + " ".join(tags)) if tags else ""
    if cooling:
        return f"{account.name}{tag} at its limit until {time.strftime('%H:%M', time.localtime(cooling))}"
    open_windows = {
        name: window
        for name, window in (record.get("windows") or {}).items()
        if isinstance(window, dict)
        and isinstance(window.get("utilization"), (int, float))
        and not (isinstance(window.get("resets_at"), (int, float)) and window["resets_at"] <= now)
    }
    if not open_windows or utilization(record, now) is None:
        return f"{account.name}{tag}" if tags else None
    name, window = max(open_windows.items(), key=lambda item: item[1]["utilization"])
    left = max(0, round((1 - float(window["utilization"])) * 100))
    detail = [WINDOW_LABELS.get(name, name)]
    resets = window.get("resets_at")
    if isinstance(resets, (int, float)):
        detail.append(f"resets {time.strftime('%H:%M', time.localtime(resets))}")
    age = _age(record, now)
    if age > STALE_AFTER_SECONDS:
        detail.append(f"as of {_hours(age)} ago")
    return f"{account.name}{tag} {left}% left ({', '.join(detail)})"


def room_line(config: Config, runtimes: list[str] | None = None, now: float | None = None) -> str:
    """One line: the room each runtime's accounts have left, from what is known. Empty if nothing is."""

    from cli_subagents.limits import _available_runtime, count_active

    now = now or time.time()
    parts = []
    counts = count_active(now)
    for name in runtimes or list(NAMES):
        runtime = config.runtime(name)
        usage = accounts.load_usage(name)
        listed = runtime.listed_accounts()
        state = quota.pool_state(runtime, counts, now)
        texts = []
        for account in listed:
            record = usage.get(account.name) or {}
            text = _account_text(runtime, account, record, now)
            if spend.configured(account):
                text = (text or account.name) + " [" + spend.describe(spend.snapshot(runtime, account, now=now)) + "]"
            pools = quota.pool_report(runtime, account.name, state, record, now)
            if pools:
                text = (text or account.name) + " [" + "; ".join(pool["state"] for pool in pools) + "]"
            if text:
                texts.append(text)
        cap, left = quota.room_cap(runtime, now)
        if cap is not None:
            detail = f"{left:g}% left" if left is not None else "no usage data"
            effective = min(cap, runtime.max_jobs) if runtime.max_jobs is not None else cap
            texts.append(f"{cap} jobs at a time (max_jobs_by_room, {detail}; {counts.by_runtime.get(name, 0)} running; effective runtime cap {effective})")
        if texts or runtime.pools or cap is not None:
            parts.append(f"{NAMES[name]} " + ", ".join(texts))
        elif _available_runtime(name):
            count = len(runtime.worker_accounts())
            noun = "account" if count == 1 else "accounts"
            parts.append(f"{NAMES[name]} {count} {noun} (no usage data)")
    return ("Room: " + " · ".join(parts)) if parts else ""
