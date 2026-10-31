"""Admission control: decide at job creation whether a job can start now.

All jobs on the machine share `~/.cli-subagents/jobs`, so counting the running jobs there
covers nested subagents and other chat sessions. Nothing here calls a model.

A refusal is one short line that names the cap, where it comes from, and what to do instead.
Caps exist only where the user or a plan preset set them; see plans.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any

from cli_subagents import accounts, quota, spend
from cli_subagents.config import Config, RUNTIMES, jobs_dir, state_dir
from cli_subagents.fsutil import read_json, recorded_process_alive, unlock
from cli_subagents.plans import Limits, account_limits, cap_effort, model_allowed

ACTIVE_STATES = ("queued", "starting", "running")
LIMIT = "limit"
STALE_RUNNER_SECONDS = 15
IGNORE_JOBS_OLDER_THAN = 7 * 3600  # a job can run 6 hours at most
NAMES = {"claude": "Claude", "codex": "Codex", "antigravity": "Antigravity", "opencode": "OpenCode"}
WAIT = "wait for one to finish"
OVERRIDE_HINT = (
    "Only if the user explicitly asked for more parallel jobs, start again with override=<their words>; "
    "otherwise ask the user (say how much of the 5-hour window is left) or use the home model."
)


def validate_override(override: str | None) -> None:
    if override is not None and (not isinstance(override, str) or not override.strip()):
        raise ValueError("override must be a non-empty string containing the user's own words")


class LimitError(ValueError):
    """A job was refused because a limit is reached. The message says what to do instead."""

    outcome = LIMIT


@dataclass
class Counts:
    total: int = 0
    by_runtime: dict[str, int] = field(default_factory=dict)
    running: dict[tuple[str, str], int] = field(default_factory=dict)  # (runtime, account) -> jobs
    unassigned: dict[str, int] = field(default_factory=dict)  # runtime -> unpinned jobs still waiting for an account

    records: list[tuple[dict, dict]] = field(default_factory=list)

    def on_account(self, runtime: str, account: str) -> int:
        return self.running.get((runtime, account), 0)


def _read_text(path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def count_active(now: float | None = None) -> Counts:
    """Count active jobs by runtime and account. A job whose runner died does not count."""

    now = now or time.time()
    counts = Counts()
    folder = jobs_dir()
    if not folder.is_dir():
        return counts
    for path in folder.iterdir():
        status = read_json(path / "status.json") if path.is_dir() else None
        if not isinstance(status, dict) or status.get("state") not in ACTIVE_STATES:
            continue
        if (path / "inbox-closed.json").exists():
            continue  # execution ended; final status may still be retrying its write
        if now - float(status.get("created_at") or 0) > IGNORE_JOBS_OLDER_THAN:
            continue
        try:
            pid = int(_read_text(path / "runner.pid"))
        except ValueError:
            continue  # never started (start=False): no runner, no worker
        started = status.get("runner_started") if status.get("runner_pid") == pid else None
        if now - float(status.get("updated_at") or 0) > STALE_RUNNER_SECONDS and not recorded_process_alive(pid, started):
            continue  # the runner died; its PID may belong to another process by now
        runtime = str(status.get("runtime") or "")
        counts.total += 1
        counts.by_runtime[runtime] = counts.by_runtime.get(runtime, 0) + 1
        spec = read_json(path / "spec.json", {})
        spec = spec if isinstance(spec, dict) else {}
        status.setdefault("job_id", path.name)
        counts.records.append((status, spec))
        account = status.get("account") or status.get("assigned_account") or spec.get("account")
        if account:
            key = (runtime, str(account))
            counts.running[key] = counts.running.get(key, 0) + 1
        else:
            counts.unassigned[runtime] = counts.unassigned.get(runtime, 0) + 1
    return counts


def utilization(record: dict[str, Any] | None, now: float, windows: tuple[str, ...] | None = None) -> float | None:
    """Highest share used across the windows still open. None when no usage was recorded.

    With `windows`, only those window names count (the keys of the record's "windows" dict).
    """

    values = []
    for key, window in ((record or {}).get("windows") or {}).items():
        if windows is not None and key not in windows:
            continue
        if not isinstance(window, dict) or not isinstance(window.get("utilization"), (int, float)):
            continue
        resets = window.get("resets_at")
        if isinstance(resets, (int, float)) and resets <= now:
            continue  # that window has reset since the figure was recorded
        values.append(float(window["utilization"]))
    return max(values) if values else None


@dataclass
class Room:
    """What a runtime can take right now."""

    accounts: list = field(default_factory=list)  # (account, limits) pairs that can take a job, best first
    running: int = 0
    cap: int = 0
    source: str = ""
    cooldown_end: float | None = None
    ceiling_hit: float | None = None  # highest utilization among accounts held back by the ceiling
    model_blocked: bool = False
    allowed_models: list[str] = field(default_factory=list)
    models_source: str = ""
    full: bool = False
    pool: Any = None
    pool_total_hit: bool = False
    pool_full: bool = False
    pool_budget_hit: bool = False
    pool_budget_used: int = 0
    spend_blocked: list = field(default_factory=list)


def _source(limits: Limits) -> str:
    if limits.max_jobs_source == "max_jobs_per_account":
        return f"cap from max_jobs_per_account = {limits.max_jobs}"
    if limits.max_jobs_source == "max_jobs":
        return f"cap from max_jobs = {limits.max_jobs} on the account"
    return f"{limits.max_jobs_source}: {limits.max_jobs} per account; change with max_jobs"


def room(config: Config, runtime_name: str, counts: Counts, *, pinned: str | None, model: str | None,
         now: float, override: str | None = None) -> Room:
    runtime = config.runtime(runtime_name)
    usage = accounts.load_usage(runtime_name)
    ranked, earliest = accounts.ranked_accounts(runtime, pinned=pinned, now=now)
    pool = quota.pool_for(runtime, model)
    state = quota.pool_state(runtime, counts, now)
    result = Room(cooldown_end=earliest, pool=pool)
    if not override and pool and pool.total is not None and state.totals.get(pool.name, 0) >= pool.total:
        result.pool_total_hit = True
    allowed = []
    for account in ranked:
        limits = account_limits(runtime, account)
        if model_allowed(runtime, limits, model):
            allowed.append((account, limits))
        else:
            result.allowed_models = limits.models or []
            result.models_source = limits.models_source or ""
    if ranked and not allowed:
        result.model_blocked = True
        return result
    if not override and pool and allowed and pool.weekly_jobs is not None:
        budget_counts = [state.budget_used(account.name, pool.name) for account, _ in allowed]
        result.pool_budget_hit = all(used >= pool.weekly_jobs for used in budget_counts)
        result.pool_budget_used = min(budget_counts)
    free_total = 0
    for account, limits in allowed:
        if spend.configured(account):
            summary = spend.snapshot(runtime, account, now=now)
            if summary["remaining_usd"] < spend.MIN_BUDGET_USD:
                result.spend_blocked.append((account, summary))
                continue
        if pool:
            if not override and pool.weekly_jobs is not None and state.budget_used(account.name, pool.name) >= pool.weekly_jobs:
                continue
            until = accounts.pool_cooldown_until(usage.get(account.name), pool.name, now)
            if until:
                result.cooldown_end = min(result.cooldown_end or until, until)
                continue
            if not override and pool.per_account is not None and state.running.get((account.name, pool.name), 0) >= pool.per_account:
                result.pool_full = True
                continue
            if result.pool_total_hit:
                continue
        used = utilization(usage.get(account.name), now, windows=runtime.usage_ceiling_windows)
        if not override and used is not None and used >= runtime.usage_ceiling:
            result.ceiling_hit = max(result.ceiling_hit or 0.0, used)
            continue
        running = counts.on_account(runtime_name, account.name)
        result.running += running
        result.cap += limits.max_jobs
        if not result.source:
            result.source = _source(limits)
        # Only a cap the user or a plan set refuses jobs. The plain max_jobs_per_account default
        # keeps its old behaviour: the job starts and waits in the runner for a slot.
        explicit = limits.max_jobs_source != "max_jobs_per_account"
        free = limits.max_jobs - running if explicit and not override else 1 << 20
        if free > 0:
            free_total += free
            result.accounts.append((account, limits))
    if not override and not pinned and result.accounts and free_total <= counts.unassigned.get(runtime_name, 0):
        result.accounts = []  # jobs that already wait for an account will take the free slots
    result.full = not result.accounts and result.cap > 0
    return result


def _available_runtime(runtime_name: str) -> bool:
    """True if the runtime's CLI is installed (an alternative must be usable)."""

    from cli_subagents.config import load_config
    from cli_subagents.runtimes import get_runtime

    try:
        return bool(get_runtime(runtime_name).find_binary(load_config().runtime(runtime_name)))
    except Exception:
        return False


def _alternatives(config: Config, runtime_name: str, counts: Counts, now: float) -> list[str]:
    """Other installed runtimes that could take a job right now."""

    found = []
    for other in RUNTIMES:
        if other == runtime_name or not _available_runtime(other):
            continue
        runtime = config.runtime(other)
        if runtime.max_jobs and counts.by_runtime.get(other, 0) >= runtime.max_jobs:
            continue
        cap, _left = quota.room_cap(runtime, now)
        if cap is not None and counts.by_runtime.get(other, 0) >= cap:
            continue
        try:
            if room(config, other, counts, pinned=None, model=None, now=now).accounts:
                found.append(other)
        except ValueError:
            continue
    return found


def _tail(found: list[str], wait: str) -> str:
    if found:
        return f"Start this with runtime {' or '.join(found)}, or {wait}."
    if wait == WAIT:
        return "Wait for one to finish, then start this again."
    return f"{wait[0].upper()}{wait[1:]}."


def _refusal(config: Config, message: str, *, overridable: bool = True) -> LimitError:
    """The refusal with the one-line "Room: ..." summary under it, when usage is known."""

    from cli_subagents import usage_view

    if overridable:
        message += " " + OVERRIDE_HINT
    try:
        line = usage_view.room_line(config)
    except Exception:
        line = ""
    return LimitError(f"{message}\n{line}" if line else message)


@dataclass
class Admission:
    account: str | None = None  # best eligible account right now (the runner leases its own)
    effort: str | None = None
    warnings: list[str] = field(default_factory=list)


def acquire_execution_slots(config: Config, runtime_name: str, model: str | None = None,
                            *, override: str | None = None) -> list | None:
    """Lease the global and runtime caps at execution, including jobs admitted to queue.

    These slots join the account lease, so its owner tracking and cleanup cover
    them too. Waiting jobs hold no slots and cannot keep each other queued forever.
    """

    runtime = config.runtime(runtime_name)
    room_cap, _ = quota.room_cap(runtime, time.time(), model)
    runtime_caps = [cap for cap in (runtime.max_jobs, room_cap) if cap is not None]
    caps = [("global", config.max_jobs), (runtime_name, min(runtime_caps) if runtime_caps else None)]
    slots = []
    try:
        for name, cap in caps:
            if cap is None:
                continue
            slot = accounts._take_capped_slot(state_dir() / "leases" / "execution" / name, cap, override=bool(override))
            if slot is None:
                return None
            slots.append(slot)
        result, slots = slots, []
        return result
    finally:
        for handle, _path in slots:
            unlock(handle)


def admit(
    config: Config,
    runtime_name: str,
    *,
    account: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    override: str | None = None,
    now: float | None = None,
) -> Admission:
    """Raise LimitError if the job can't start now; else say which account is expected to take it."""

    from cli_subagents import usage_view

    validate_override(override)
    now = now or time.time()
    runtime = config.runtime(runtime_name)
    if runtime_name == "codex":
        usage_view.refresh_codex(config)  # reads local files only
    counts = count_active(now)
    queue = runtime.when_full == "queue" and not override
    name = NAMES.get(runtime_name, runtime_name)
    if not queue and not override:
        if config.max_jobs and counts.total >= config.max_jobs:
            raise _refusal(config, 
                f"csa is at its global limit: {counts.total} of {config.max_jobs} subagents running "
                f"(max_jobs in config.toml). {_tail([], WAIT)}"
            )
        running = counts.by_runtime.get(runtime_name, 0)
        if runtime.max_jobs and running >= runtime.max_jobs:
            found = _alternatives(config, runtime_name, counts, now)
            raise _refusal(config, 
                f"{name} is at its limit: {running} of {runtime.max_jobs} subagents running "
                f"(max_jobs under [{runtime_name}]). {_tail(found, WAIT)}"
            )
    cap, left = quota.room_cap(runtime, now, model)
    running = counts.by_runtime.get(runtime_name, 0)
    if not override and cap is not None and running >= cap:
        found = _alternatives(config, runtime_name, counts, now)
        detail = f"has {left:g}% of its quota left" if left is not None else "has no quota data"
        tail = (f"Run this on {' or '.join(NAMES[other] for other in found)}, or {WAIT}."
                if found else "Wait for quota to reset or a running job to finish.")
        raise _refusal(config, f"{name} {detail}, so {cap} jobs at a time (max_jobs_by_room); {running} are running. {tail}")
    current = room(config, runtime_name, counts, pinned=account, model=model, now=now, override=override)
    admission = Admission(effort=effort)
    if current.accounts:
        chosen, limits = current.accounts[0]
        admission.account = chosen.name
        capped = cap_effort(effort, limits)
        if capped != effort:
            admission.warnings.append(
                f"effort lowered from {effort} to {capped} ({chosen.name}: {limits.max_effort_source})"
            )
        admission.effort = capped
        return admission
    if current.model_blocked:
        found = [] if account else _alternatives(config, runtime_name, counts, now)
        listed = ", ".join(current.allowed_models)
        raise _refusal(config, 
            f"No {runtime_name} account allows model '{model or runtime.default_model}' "
            f"(allowed: {listed}; from {current.models_source}). {_tail(found, 'use one of those models')}",
            overridable=False,
        )
    if current.spend_blocked:
        raise _refusal(config, " ".join(spend.refusal(account, summary)
                                       for account, summary in current.spend_blocked), overridable=False)
    if current.pool_budget_hit:
        pool = current.pool
        found = [] if account else _alternatives(config, runtime_name, counts, now)
        tail = "Use Opus instead." if "claude" in found else _tail(found, "try again after the weekly budget resets")
        where = f"account '{account}'" if account else "every account"
        raise _refusal(config, f"{pool.name.capitalize()} budget used up: {current.pool_budget_used} of {pool.weekly_jobs} jobs this week on {where} (weekly_jobs in pool {pool.name}). {tail}")
    if queue:
        return admission  # the runner waits for a free slot
    where = f"{name} account '{account}'" if account else name
    found = [] if account else _alternatives(config, runtime_name, counts, now)
    if current.pool_total_hit or current.pool_full:
        pool = current.pool
        if current.pool_total_hit:
            detail = f"{pool.total} jobs across accounts (total in pool {pool.name})"
        else:
            detail = f"{pool.per_account} jobs per account (per_account in pool {pool.name})"
        raise _refusal(config, f"{pool.name.capitalize()} is at its limit: {detail}. {_tail(found, WAIT)}")
    if current.full:
        tail = _tail(found, WAIT)
        if account:
            tail = "Omit account to let csa pick another, or wait for one to finish."
        raise _refusal(config, 
            f"{where} is at its limit: {current.running} of {current.cap} subagents running ({current.source}). {tail}"
        )
    if current.ceiling_hit is not None:
        raise _refusal(config, 
            f"{where} is at {round(current.ceiling_hit * 100)}% of its usage window "
            f"(usage_ceiling {round(runtime.usage_ceiling * 100)}%). {_tail(found, 'try again after the window resets')}"
        )
    if current.cooldown_end:
        until = time.strftime("%H:%M", time.localtime(current.cooldown_end))
        raise _refusal(config, f"{where} is at its usage limit until {until}. {_tail(found, f'try again after {until}')}",
                       overridable=False)
    return admission  # no accounts at all: the runner reports that as before
