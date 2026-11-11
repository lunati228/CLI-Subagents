"""Plan presets and the effective limits of one account.

A preset is a recommended default. It applies only when the user sets `plan = "..."`
on an account (or on the runtime, for the primary account). Without a plan nothing
new is capped. Explicit keys (`max_jobs`, `max_effort`, `models`) always win over a preset.

Vendors publish usage windows, not concurrency numbers. Where a preset's job count is
our estimate and not a published figure, docs/research/limits.md says so.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from cli_subagents.config import Account, RuntimeConfig

EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class Preset:
    max_jobs: int
    max_effort: str | None = None  # highest reasoning effort the plan offers; None = no cap
    models: tuple[str, ...] | None = None  # models the plan offers; None = no list


PRESETS: dict[str, dict[str, Preset]] = {
    "codex": {
        "free": Preset(1, max_effort="high"),
        "go": Preset(1, max_effort="high"),
        "plus": Preset(4),
        "pro": Preset(8),
        "business": Preset(6),
        "enterprise": Preset(8),
    },
    "claude": {
        "free": Preset(1, max_effort="medium"),
        "pro": Preset(2),
        "max-5x": Preset(4),
        "max-20x": Preset(8),
        "team": Preset(3),
        "enterprise": Preset(6),
    },
    "antigravity": {
        "free": Preset(1),
        "pro": Preset(2),
        "ultra": Preset(4),
    },
}

ALIASES = {
    "max": "max-5x", "max5": "max-5x", "max5x": "max-5x", "max-5": "max-5x",
    "max20": "max-20x", "max20x": "max-20x", "max-20": "max-20x",
    "chatgpt-plus": "plus", "chatgpt-pro": "pro", "chatgpt-free": "free",
    "ai-pro": "pro", "ai-ultra": "ultra", "google-ai-pro": "pro", "google-ai-ultra": "ultra",
}


def normalize_plan(plan: str | None) -> str | None:
    if not plan:
        return None
    key = str(plan).strip().lower().replace(" ", "-").replace("_", "-")
    return ALIASES.get(key, key) or None


def preset_for(runtime: str, plan: str | None) -> Preset | None:
    # A remote account is a Claude account on a server: same plans, same presets.
    return PRESETS.get("claude" if runtime == "remote" else runtime, {}).get(normalize_plan(plan) or "")


@dataclass
class Limits:
    """What one account may do, and where each number comes from."""

    max_jobs: int
    max_jobs_source: str
    max_effort: str | None = None
    max_effort_source: str | None = None
    models: list[str] | None = None
    models_source: str | None = None

    def describe(self) -> dict[str, object]:
        data: dict[str, object] = {"max_jobs": self.max_jobs, "max_jobs_from": self.max_jobs_source}
        if self.max_effort:
            data.update(max_effort=self.max_effort, max_effort_from=self.max_effort_source)
        if self.models:
            data.update(models=self.models, models_from=self.models_source)
        return data


CLAUDE_TIERS = (("max_20x", "max-20x"), ("max_5x", "max-5x"), ("enterprise", "enterprise"), ("team", "team"), ("pro", "pro"))


def detect_claude_plan(home: Path) -> str | None:
    """A Claude account's plan from its login info, read from two fields only.

    Reads `oauthAccount.userRateLimitTier` and `oauthAccount.organizationRateLimitTier`
    of `<config dir>/.claude.json` (for the default folder, `~/.claude.json`). Nothing else
    in that file is looked at.
    """

    home = Path(home).expanduser()
    candidates = [home / ".claude.json"]
    if home == Path.home() / ".claude":
        candidates.append(Path.home() / ".claude.json")
    for path in candidates:
        try:
            account = json.loads(path.read_text(encoding="utf-8")).get("oauthAccount")
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(account, dict):
            continue
        for key in ("userRateLimitTier", "organizationRateLimitTier"):
            tier = account.get(key)
            if isinstance(tier, str):
                for needle, plan in CLAUDE_TIERS:
                    if re.search(rf"(^|_){needle}($|_)", tier.lower()):
                        return plan
    return None


def effective_plan(runtime: RuntimeConfig, account: Account) -> tuple[str | None, bool]:
    """(plan name, auto-detected). `plan = "auto"` asks for detection (Claude only)."""

    plan = normalize_plan(account.plan)
    if plan == "auto":
        return (detect_claude_plan(account.home) if runtime.runtime == "claude" else None), True
    return plan, False


def account_limits(runtime: RuntimeConfig, account: Account) -> Limits:
    plan, auto = effective_plan(runtime, account)
    preset = preset_for(runtime.runtime, plan)
    label = f"plan preset {plan}" + (" (auto-detected)" if auto else "")
    if account.max_jobs:
        jobs, jobs_source = account.max_jobs, "max_jobs"
    elif preset:
        jobs, jobs_source = preset.max_jobs, label
    else:
        jobs, jobs_source = runtime.max_jobs_per_account, "max_jobs_per_account"
    limits = Limits(jobs, jobs_source)
    if account.max_effort:
        limits.max_effort, limits.max_effort_source = account.max_effort, "max_effort"
    elif preset and preset.max_effort:
        limits.max_effort, limits.max_effort_source = preset.max_effort, label
    if account.models:
        limits.models, limits.models_source = list(account.models), "models"
    elif preset and preset.models:
        limits.models, limits.models_source = list(preset.models), label
    return limits


def account_cap(runtime: RuntimeConfig, account: Account) -> int:
    return account_limits(runtime, account).max_jobs


def model_allowed(runtime: RuntimeConfig, limits: Limits, model: str | None) -> bool:
    """A job without a model gets the runtime's default model; unknown means allowed."""

    if not limits.models:
        return True
    chosen = model or runtime.default_model
    if not chosen:
        return True
    return chosen.strip().lower() in {item.lower() for item in limits.models}


def cap_effort(effort: str | None, limits: Limits) -> str | None:
    """The effort lowered to the account's cap; unchanged when it is within it or unknown."""

    if not effort or not limits.max_effort:
        return effort
    if effort in EFFORTS and limits.max_effort in EFFORTS and EFFORTS.index(effort) > EFFORTS.index(limits.max_effort):
        return limits.max_effort
    return effort
