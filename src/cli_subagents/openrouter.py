"""Soft weekly cap on an account's OpenRouter key (openrouter_weekly_cap_usd).

For a Claude account whose own settings.json points Claude Code at OpenRouter, csa asks
OpenRouter what the key has spent this UTC week (Monday to Sunday) before a new start.
The figure is the key's total, so spending outside csa counts too. At or above the cap,
new starts are refused like a quota step and override lets one through; running jobs
are never touched. When OpenRouter can't be reached, the start goes ahead with a warning.

Claude Code's own cost estimate is not used: it prices unknown models far too high.
"""

from __future__ import annotations

import json
import time
from typing import Any
import urllib.request

from cli_subagents.config import Account, state_dir
from cli_subagents.fsutil import file_lock, read_json, write_json

KEY_URL = "https://openrouter.ai/api/v1/key"
CACHE_SECONDS = 60
TIMEOUT_SECONDS = 5


def api_key(account: Account) -> str | None:
    """The OpenRouter key the account's own settings.json gives Claude Code, if it uses OpenRouter."""

    settings = read_json(account.home / "settings.json", {}) or {}
    env = settings.get("env") if isinstance(settings, dict) else None
    if not isinstance(env, dict) or "openrouter.ai" not in str(env.get("ANTHROPIC_BASE_URL") or ""):
        return None
    key = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
    return str(key) if key else None


def _fetch(key: str) -> dict[str, Any]:
    request = urllib.request.Request(KEY_URL, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.load(response)["data"]


def weekly_usage(account: Account, now: float | None = None) -> float | None:
    """USD the account's OpenRouter key spent this UTC week, or None when unknown. Cached briefly."""

    now = time.time() if now is None else now
    path = state_dir() / "openrouter-usage.json"
    with file_lock(path.with_suffix(".lock"), timeout=30):
        cache = read_json(path, {}) or {}
        entry = cache.get(account.name) if isinstance(cache, dict) else None
        if isinstance(entry, dict) and 0 <= now - float(entry.get("at") or 0) < CACHE_SECONDS:
            return entry.get("usage_weekly")
    key = api_key(account)
    if not key:
        return None
    try:
        usage = float(_fetch(key)["usage_weekly"])
    except Exception:
        return None
    with file_lock(path.with_suffix(".lock"), timeout=30):
        cache = read_json(path, {}) or {}
        cache = cache if isinstance(cache, dict) else {}
        cache[account.name] = {"at": now, "usage_weekly": usage}
        write_json(path, cache)
    return usage


def describe(account: Account, used: float | None) -> str:
    spent = f"${used:.2f}" if used is not None else "unknown"
    return f"OpenRouter key {spent} / ${account.openrouter_weekly_cap_usd:.2f} this week, soft cap"


def refusal(account: Account, used: float) -> str:
    return (f"Account '{account.name}': its OpenRouter key has spent ${used:.2f} this week, at or over the "
            f"${account.openrouter_weekly_cap_usd:.2f} soft cap (openrouter_weekly_cap_usd; the week resets Monday "
            "00:00 UTC). Use another pool, or only if the user explicitly asks to go over it, start again with "
            "override=<their words>.")
