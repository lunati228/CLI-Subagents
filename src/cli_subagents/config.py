"""User configuration (`~/.cli-subagents/config.toml`) and state locations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import json
import math
import os
from pathlib import Path
import re
import tomllib
from typing import Any

from cli_subagents.fsutil import write_text

RUNTIMES = ("claude", "codex", "antigravity")
# Runtimes whose extra accounts are "use at your own risk" (Antigravity: Google's FAQ says
# consumer plans don't support automated account rotation). Failover is on by default, as for
# the others; with failover = "off", unpinned jobs run on the primary account only.
OPT_IN_FAILOVER = ("antigravity",)
FAILOVER_MODES = ("off", "on-limit")
WHEN_FULL_MODES = ("refuse", "queue")
REVIEWERS = ("agy", "claude", "off")
WEB_SEARCH_MODES = ("disabled", "cached", "indexed", "live")
WINDOWS_SANDBOX_MODES = ("unelevated", "elevated", "off")
REASONING_SUMMARY_MODES = ("detailed", "concise", "auto", "none", "off")
SPEND_PERIODS = ("day", "week", "month", "total")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def home() -> Path:
    """Root folder for config, job records and per-account homes."""

    override = os.environ.get("CLI_SUBAGENTS_HOME")
    return Path(override).expanduser() if override else Path.home() / ".cli-subagents"


def config_path() -> Path:
    return home() / "config.toml"


def jobs_dir() -> Path:
    return home() / "jobs"


def state_dir() -> Path:
    return home() / "state"


def cache_dir() -> Path:
    return home() / "cache"


def homes_dir() -> Path:
    return home() / "homes"


def default_primary(runtime: str) -> Path:
    if runtime == "claude":
        configured = os.environ.get("CLAUDE_CONFIG_DIR")
        return Path(configured).expanduser() if configured else Path.home() / ".claude"
    if runtime == "codex":
        configured = os.environ.get("CODEX_HOME")
        return Path(configured).expanduser() if configured else Path.home() / ".codex"
    if runtime == "antigravity":
        return Path.home() / ".gemini" / "antigravity-cli"
    raise ValueError(f"unknown runtime: {runtime}")


@dataclass
class SpendCap:
    amount_usd: float
    window: str  # rolling duration, e.g. "24h" or "7d"


@dataclass
class Account:
    name: str
    home: Path
    enabled: bool = True
    is_primary: bool = False
    fallback: bool = False  # used only when every other account is busy, cooling down or excluded
    # Limits (see plans.py). None means "not set": the plan preset, then the runtime default, decide.
    plan: str | None = None
    max_jobs: int | None = None
    max_effort: str | None = None
    models: list[str] | None = None
    explicit_only: bool = False  # only a start that names this account may use it
    spend_cap_usd: float | None = None
    spend_period: str = "month"
    spend_since: str | None = None  # UTC date or ISO datetime; starts a new accounting window
    spend_caps: list[SpendCap] = field(default_factory=list)


@dataclass
class QuotaPool:
    name: str
    models: list[str]
    per_account: int | None = None
    total: int | None = None
    weekly_jobs: int | None = None
    cooldown_minutes: int | None = None


@dataclass
class RuntimeConfig:
    runtime: str
    primary: Path
    binary: str | None = None
    include_primary: bool = False
    failover: str = "on-limit"
    max_jobs_per_account: int = 1
    connectors: str = "account"
    edit_permission_mode: str = "auto"
    default_model: str | None = None
    cooldown_minutes: int = 60
    capacity_retries: int = 3
    capacity_retry_seconds: int = 30  # initial delay; doubles for each retry
    windows_sandbox: str = "unelevated"  # [codex] only
    reasoning_summary: str = "detailed"  # [codex] only; off leaves the CLI default alone
    share_parent_mcp: bool = True  # [codex] and [antigravity]: give workers started from Claude Code its MCP servers
    # [antigravity] only: the review gate for read and edit jobs (see docs/gemini.md#safety)
    reviewer: str = "agy"  # "agy", "claude" or "off" (fixed rules only; the rest is denied)
    reviewer_model: str | None = None
    reviewer_account: str | None = None  # Claude account for reviewer = "claude"
    sandbox: bool = True  # run edit jobs with agy --sandbox
    # Limits. `plan` and the primary_* keys describe the primary account.
    plan: str | None = None
    primary_max_jobs: int | None = None
    primary_max_effort: str | None = None
    primary_models: list[str] | None = None
    max_jobs: int | None = None  # runtime-wide cap on running jobs
    usage_ceiling: float = 0.95  # accounts at or above this share of a usage window get no new jobs
    when_full: str = "refuse"
    max_jobs_by_room: list[list[float | int]] = field(default_factory=list)
    pools: list[QuotaPool] = field(default_factory=list)
    web_search: str | None = None  # [codex] only; unset preserves Codex's default
    network_access: bool = True  # [codex] only: network inside the sandbox for edit jobs (local test sockets too)
    accounts: list[Account] = field(default_factory=list)

    def primary_account(self) -> Account:
        return Account(
            name="primary", home=self.primary, is_primary=True, plan=self.plan, max_jobs=self.primary_max_jobs,
            max_effort=self.primary_max_effort, models=self.primary_models,
        )

    def worker_accounts(self) -> list[Account]:
        """Accounts that unpinned jobs may use, in configuration order."""

        if self.runtime in OPT_IN_FAILOVER and self.failover == "off":
            return [self.primary_account()]
        accounts = [account for account in self.accounts if account.enabled and not account.explicit_only]
        if self.include_primary or not accounts:
            accounts.insert(0, self.primary_account())
        return accounts

    def pool_accounts(self) -> list[Account]:
        """Accounts that may run workers, pinned or not (doctor --live checks these)."""

        if self.runtime in OPT_IN_FAILOVER:
            return [self.primary_account(), *(account for account in self.accounts if account.enabled)]
        accounts = [account for account in self.accounts if account.enabled]
        if self.include_primary or any(account.is_primary for account in self.worker_accounts()):
            accounts.insert(0, self.primary_account())
        return accounts

    def listed_accounts(self) -> list[Account]:
        """Configured accounts, including disabled and explicit-only accounts, plus the active primary."""

        show_primary = self.runtime in OPT_IN_FAILOVER or any(account.is_primary for account in self.worker_accounts())
        return ([self.primary_account()] if show_primary else []) + self.accounts

    def find(self, name: str) -> Account | None:
        if name == "primary":
            return self.primary_account()
        return next((account for account in self.accounts if account.name == name), None)


@dataclass
class Config:
    runtimes: dict[str, RuntimeConfig]
    job_retention_days: int = 7
    max_depth: int = 1
    max_jobs: int | None = None  # cap on running jobs across all runtimes

    def runtime(self, name: str) -> RuntimeConfig:
        if name not in self.runtimes:
            raise ValueError(f"unknown runtime '{name}'. Known: {', '.join(RUNTIMES)}")
        return self.runtimes[name]


def _path(value: Any, fallback: Path) -> Path:
    if isinstance(value, str) and value.strip():
        return Path(os.path.expandvars(value)).expanduser()
    return fallback


def _opt_int(value: Any, minimum: int = 1) -> int | None:
    return max(minimum, int(value)) if value is not None else None


def _opt_text(value: Any) -> str | None:
    return str(value).strip().lower() or None if value is not None else None


def _opt_models(value: Any) -> list[str] | None:
    if value is None:
        return None
    items = [value] if isinstance(value, str) else list(value)
    return [str(item).strip() for item in items if str(item).strip()] or None


def _room_steps(name: str, value: Any) -> list[list[float | int]]:
    if value is None:
        return []
    if not isinstance(value, list) or not value:
        raise ValueError(f"[{name}] max_jobs_by_room needs descending [percent, jobs] steps")
    previous, previous_cap = 101.0, None
    steps = []
    for step in value:
        if (not isinstance(step, list) or len(step) != 2
                or type(step[0]) not in (int, float) or not 0 <= step[0] <= 100
                or type(step[1]) is not int or step[1] < 0
                or step[0] >= previous or (previous_cap is not None and step[1] > previous_cap)):
            raise ValueError(f"[{name}] max_jobs_by_room needs descending percentages and nonnegative job caps")
        steps.append(list(step))
        previous, previous_cap = step
    if steps[-1][0] != 0:
        raise ValueError(f"[{name}] max_jobs_by_room must end with a 0 percent step")
    return steps


def _quota_pools(name: str, value: Any) -> list[QuotaPool]:
    if not isinstance(value, list):
        raise ValueError(f"[{name}] pools must be an array of tables")
    pools = []
    names = set()
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not NAME_RE.fullmatch(item["name"]):
            raise ValueError(f"[{name}] pool entries need a simple name")
        pool_name = item["name"]
        if pool_name in names:
            raise ValueError(f"[{name}] duplicate pool name '{pool_name}'")
        names.add(pool_name)
        models = item.get("models")
        if not isinstance(models, list) or not models or any(not isinstance(m, str) or not m.strip() for m in models):
            raise ValueError(f"[{name}] pool '{pool_name}' needs a nonempty models glob list")
        fields = {}
        for key in ("per_account", "total", "weekly_jobs", "cooldown_minutes"):
            value = item.get(key)
            if value is not None:
                if type(value) is not int or value < 1:
                    raise ValueError(f"[{name}] pool '{pool_name}' {key} must be a positive integer")
                fields[key] = value
        pools.append(QuotaPool(pool_name, [m.strip() for m in models], **fields))
    return pools


def _spend_amount(label: str, key: str, value: Any) -> float:
    try:
        if type(value) not in (int, float) or not math.isfinite(float(value)) or value <= 0:
            raise ValueError
        return float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{label} {key} must be a positive finite number") from None


def _account_spend(runtime: str, account: str, item: dict[str, Any]) -> dict[str, Any]:
    """Validate spend settings before any account can be selected or run."""

    label = f"[{runtime}] account '{account}'"
    keys = ("spend_cap_usd", "spend_period", "spend_since", "spend_caps")
    if runtime != "claude" and any(key in item for key in keys):
        raise ValueError(f"{label}: spend caps are supported for Claude accounts only")
    cap = item.get("spend_cap_usd")
    if cap is not None:
        cap = _spend_amount(label, "spend_cap_usd", cap)
    caps = []
    if "spend_caps" in item:
        if "spend_cap_usd" in item:
            raise ValueError(f"{label}: use spend_caps or spend_cap_usd, not both")
        values = item["spend_caps"]
        if not isinstance(values, list) or not values:
            raise ValueError(f"{label} spend_caps must be a nonempty array of amount_usd/window tables")
        for value in values:
            if not isinstance(value, dict) or set(value) != {"amount_usd", "window"}:
                raise ValueError(f"{label} spend_caps entries need amount_usd and window only")
            amount = _spend_amount(label, "spend_caps amount_usd", value["amount_usd"])
            window = value["window"]
            matched = re.fullmatch(r"([1-9][0-9]*)(h|d)", window) if isinstance(window, str) else None
            if not matched:
                raise ValueError(f"{label} spend_caps window must be a positive number of hours or days, for example '24h' or '7d'")
            if len(matched[1]) > 6 or int(matched[1]) * (24 if matched[2] == "d" else 1) > 36500 * 24:
                raise ValueError(f"{label} spend_caps window must be at most 36500 days")
            caps.append(SpendCap(amount, window))
    period = item.get("spend_period", "month")
    if period not in SPEND_PERIODS:
        raise ValueError(f"{label} spend_period must be one of {SPEND_PERIODS}")
    since = item.get("spend_since")
    if since is not None:
        try:
            if not isinstance(since, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T.+)?", since):
                raise ValueError
            if "T" in since:
                parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
                if parsed.utcoffset() not in (None, timedelta(0)):
                    raise ValueError
            else:
                date.fromisoformat(since)
        except (TypeError, ValueError):
            raise ValueError(f"{label} spend_since must be an ISO UTC date or datetime (for example 2026-10-23 or 2026-10-23T12:00:00Z)") from None
    return {"spend_cap_usd": cap, "spend_period": period, "spend_since": since, "spend_caps": caps}


def _runtime_from(name: str, data: dict[str, Any]) -> RuntimeConfig:
    accounts: list[Account] = []
    for item in data.get("accounts", []) or []:
        if not isinstance(item, dict) or not NAME_RE.match(str(item.get("name", ""))):
            raise ValueError(f"[{name}] account entries need a simple name")
        account_name = str(item["name"])
        if account_name == "primary":
            raise ValueError("'primary' is reserved for the main setup")
        explicit_only = item.get("explicit_only", False)
        if not isinstance(explicit_only, bool):
            raise ValueError(f"[{name}] account '{account_name}' explicit_only must be true or false")
        accounts.append(
            Account(
                name=account_name,
                home=_path(item.get("home"), homes_dir() / name / account_name),
                enabled=bool(item.get("enabled", True)),
                fallback=bool(item.get("fallback", False)),
                plan=_opt_text(item.get("plan")),
                max_jobs=_opt_int(item.get("max_jobs")),
                max_effort=_opt_text(item.get("max_effort")),
                models=_opt_models(item.get("models")),
                explicit_only=explicit_only,
                **_account_spend(name, account_name, item),
            )
        )
    opt_in = name in OPT_IN_FAILOVER
    # On by default everywhere; for Antigravity, "off" keeps unpinned jobs on the primary account.
    failover = str(data.get("failover", "on-limit"))
    if failover not in FAILOVER_MODES:
        raise ValueError(f"[{name}] failover must be one of {FAILOVER_MODES}")
    connectors = str(data.get("connectors", "account"))
    if connectors not in ("account", "off"):
        raise ValueError(f"[{name}] connectors must be 'account' or 'off'")
    windows_sandbox = str(data.get("windows_sandbox", "unelevated"))
    if windows_sandbox not in WINDOWS_SANDBOX_MODES:
        raise ValueError(f"[{name}] windows_sandbox must be one of {WINDOWS_SANDBOX_MODES}")
    reasoning_summary = data.get("reasoning_summary", "detailed")
    if reasoning_summary not in REASONING_SUMMARY_MODES or ("reasoning_summary" in data and name != "codex"):
        raise ValueError(f"[{name}] reasoning_summary must be under [codex] and one of {REASONING_SUMMARY_MODES}")
    when_full = str(data.get("when_full", "refuse"))
    if when_full not in WHEN_FULL_MODES:
        raise ValueError(f"[{name}] when_full must be one of {WHEN_FULL_MODES}")
    reviewer = str(data.get("reviewer", "agy"))
    if reviewer not in REVIEWERS:
        raise ValueError(f"[{name}] reviewer must be one of {REVIEWERS}")
    web_search = data.get("web_search")
    if web_search is not None and (name != "codex" or web_search not in WEB_SEARCH_MODES):
        raise ValueError(f"[{name}] web_search must be under [codex] and one of {WEB_SEARCH_MODES}")
    network_access = data.get("network_access", True)
    if not isinstance(network_access, bool) or ("network_access" in data and name != "codex"):
        raise ValueError(f"[{name}] network_access must be true or false, under [codex] only")
    ceiling = float(data.get("usage_ceiling", 0.95))
    if not 0 < ceiling <= 1:
        raise ValueError(f"[{name}] usage_ceiling must be above 0 and at most 1")
    capacity_retries = data.get("capacity_retries", 3)
    capacity_retry_seconds = data.get("capacity_retry_seconds", 30)
    for key, value, minimum in (("capacity_retries", capacity_retries, 0),
                                ("capacity_retry_seconds", capacity_retry_seconds, 1)):
        if type(value) is not int or value < minimum:
            raise ValueError(f"[{name}] {key} must be an integer at least {minimum}")
    return RuntimeConfig(
        runtime=name,
        primary=_path(data.get("primary"), default_primary(name)),
        binary=data.get("binary") or None,
        # These runtimes rotate through the primary account too, unless told otherwise.
        include_primary=bool(data.get("include_primary", opt_in)),
        failover=failover,
        max_jobs_per_account=max(1, int(data.get("max_jobs_per_account", 1))),
        connectors=connectors,
        edit_permission_mode=str(data.get("edit_permission_mode", "accept-edits" if name == "antigravity" else "auto")),
        default_model=data.get("default_model") or None,
        cooldown_minutes=max(1, int(data.get("cooldown_minutes", 60))),
        capacity_retries=capacity_retries,
        capacity_retry_seconds=capacity_retry_seconds,
        windows_sandbox=windows_sandbox,
        reasoning_summary=reasoning_summary,
        share_parent_mcp=bool(data.get("share_parent_mcp", True)),
        reviewer=reviewer,
        reviewer_model=str(data.get("reviewer_model") or "").strip() or None,
        reviewer_account=str(data.get("reviewer_account") or "").strip() or None,
        sandbox=bool(data.get("sandbox", True)),
        plan=_opt_text(data.get("plan")),
        primary_max_jobs=_opt_int(data.get("primary_max_jobs")),
        primary_max_effort=_opt_text(data.get("primary_max_effort")),
        primary_models=_opt_models(data.get("primary_models")),
        max_jobs=_opt_int(data.get("max_jobs")),
        usage_ceiling=ceiling,
        when_full=when_full,
        max_jobs_by_room=_room_steps(name, data.get("max_jobs_by_room")),
        pools=_quota_pools(name, data.get("pools", [])),
        web_search=web_search,
        network_access=network_access,
        accounts=accounts,
    )


def load_config(path: Path | None = None) -> Config:
    path = path or config_path()
    data: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    runtimes = {name: _runtime_from(name, data.get(name, {}) or {}) for name in RUNTIMES}
    return Config(
        runtimes=runtimes,
        job_retention_days=max(1, int(data.get("job_retention_days", 7))),
        max_depth=max(0, int(data.get("max_depth", 1))),
        max_jobs=_opt_int(data.get("max_jobs")),
    )


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def dump_config(config: Config) -> str:
    """Serialize the config back to TOML (only the keys this tool understands)."""

    lines = [
        "# CLI Subagents configuration. See docs/configuration.md.",
        f"job_retention_days = {config.job_retention_days}",
        f"max_depth = {config.max_depth}",
    ]
    if config.max_jobs:
        lines.append(f"max_jobs = {config.max_jobs}")
    for name in RUNTIMES:
        runtime = config.runtimes[name]
        lines += [
            "",
            f"[{name}]",
            f"primary = {_toml_value(str(runtime.primary))}",
            f"include_primary = {_toml_value(runtime.include_primary)}",
            f"failover = {_toml_value(runtime.failover)}",
            f"max_jobs_per_account = {runtime.max_jobs_per_account}",
            f"connectors = {_toml_value(runtime.connectors)}",
            f"edit_permission_mode = {_toml_value(runtime.edit_permission_mode)}",
            f"cooldown_minutes = {runtime.cooldown_minutes}",
            f"capacity_retries = {runtime.capacity_retries}",
            f"capacity_retry_seconds = {runtime.capacity_retry_seconds}",
        ]
        if runtime.binary:
            lines.append(f"binary = {_toml_value(runtime.binary)}")
        if runtime.default_model:
            lines.append(f"default_model = {_toml_value(runtime.default_model)}")
        if name == "codex" and runtime.windows_sandbox != "unelevated":
            lines.append(f"windows_sandbox = {_toml_value(runtime.windows_sandbox)}")
        if name == "codex" and runtime.reasoning_summary != "detailed":
            lines.append(f"reasoning_summary = {_toml_value(runtime.reasoning_summary)}")
        if name in ("codex", "antigravity") and not runtime.share_parent_mcp:
            lines.append("share_parent_mcp = false")
        if name == "antigravity":
            if runtime.reviewer != "agy":
                lines.append(f"reviewer = {_toml_value(runtime.reviewer)}")
            for key, value in (("reviewer_model", runtime.reviewer_model), ("reviewer_account", runtime.reviewer_account)):
                if value:
                    lines.append(f"{key} = {_toml_value(value)}")
            if not runtime.sandbox:
                lines.append("sandbox = false")
        for key, value in (
            ("plan", runtime.plan), ("primary_max_jobs", runtime.primary_max_jobs),
            ("primary_max_effort", runtime.primary_max_effort), ("max_jobs", runtime.max_jobs),
        ):
            if value is not None:
                lines.append(f"{key} = {_toml_value(value)}")
        if runtime.primary_models:
            lines.append(f"primary_models = {json.dumps(runtime.primary_models)}")
        if runtime.usage_ceiling != 0.95:
            lines.append(f"usage_ceiling = {runtime.usage_ceiling}")
        if runtime.when_full != "refuse":
            lines.append(f"when_full = {_toml_value(runtime.when_full)}")
        if runtime.max_jobs_by_room:
            lines.append(f"max_jobs_by_room = {json.dumps(runtime.max_jobs_by_room)}")
        if name == "codex" and runtime.web_search is not None:
            lines.append(f"web_search = {_toml_value(runtime.web_search)}")
        if name == "codex" and not runtime.network_access:
            lines.append("network_access = false")
        for pool in runtime.pools:
            lines += ["", f"[[{name}.pools]]", f"name = {_toml_value(pool.name)}", f"models = {json.dumps(pool.models)}"]
            for key in ("per_account", "total", "weekly_jobs", "cooldown_minutes"):
                value = getattr(pool, key)
                if value is not None:
                    lines.append(f"{key} = {value}")
        for account in runtime.accounts:
            lines += [
                "",
                f"[[{name}.accounts]]",
                f"name = {_toml_value(account.name)}",
                f"home = {_toml_value(str(account.home))}",
                f"enabled = {_toml_value(account.enabled)}",
            ]
            if account.fallback:
                lines.append("fallback = true")
            if account.explicit_only:
                lines.append("explicit_only = true")
            if account.spend_cap_usd is not None:
                lines.append(f"spend_cap_usd = {_toml_value(account.spend_cap_usd)}")
            if account.spend_period != "month":
                lines.append(f"spend_period = {_toml_value(account.spend_period)}")
            if account.spend_since is not None:
                lines.append(f"spend_since = {_toml_value(account.spend_since)}")
            if account.spend_caps:
                entries = ", ".join(
                    f"{{ amount_usd = {_toml_value(cap.amount_usd)}, window = {_toml_value(cap.window)} }}"
                    for cap in account.spend_caps
                )
                lines.append(f"spend_caps = [{entries}]")
            for key, value in (("plan", account.plan), ("max_jobs", account.max_jobs), ("max_effort", account.max_effort)):
                if value is not None:
                    lines.append(f"{key} = {_toml_value(value)}")
            if account.models:
                lines.append(f"models = {json.dumps(account.models)}")
    return "\n".join(lines) + "\n"


def save_config(config: Config, path: Path | None = None) -> None:
    write_text(path or config_path(), dump_config(config))
