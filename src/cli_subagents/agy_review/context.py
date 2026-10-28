"""The per-job context file: what the gate needs to know about one job.

Written by the caller (CLI Subagents) before it starts agy, read by every hook
call. Its path reaches the hook through AGY_REVIEW_CONTEXT in agy's
environment. Keys:

    version              1
    access               "read" or "edit"
    sandbox              true when agy runs with --sandbox
    review_network       optional network class a reviewed command gets if approved
    secret_paths         extra login/service credential paths (including custom XDG locations)
    workspaces           folders the job works in (reads and edits allowed)
    read_paths           extra files and folders it may read (instruction files, skills)
    protected_paths      folders it may never touch (beyond the built-in list)
    agent_home           the HOME agy runs with
    user_home            the user's real home folder
    trusted_mcp_servers  MCP servers whose tools need no review
    reviewers            the review chain, tried in order: [{"kind": "agy"|"claude"|"codex", "command": [...],
                          "args": [...], "model": ..., "account": ..., "env": {...}, "unset_env": [...],
                          "timeout": 75, "work_dir": ...}, ...]; empty = no reviewer
    reviewer             the older one-reviewer form ({"kind": "off"} or one entry), used without `reviewers`
    review_budget        seconds the whole chain may take for one check (default 80, inside agy's 90 s hook limit)
    log, cache           files for the decision log and the decision cache
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VERSION = 1
MAX_BYTES = 256 * 1024
ACCESS = ("read", "edit")
KINDS = ("agy", "claude", "codex")


class ContextError(Exception):
    pass


def _strings(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ContextError(f"{key} must be a list of strings")
    return value


def validate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("version") != VERSION:
        raise ContextError("unknown context version")
    if data.get("access") not in ACCESS:
        raise ContextError("access must be read or edit")
    if not _strings(data.get("workspaces"), "workspaces"):
        raise ContextError("workspaces is empty")
    for key in ("read_paths", "protected_paths", "secret_paths", "trusted_mcp_servers"):
        _strings(data.get(key), key)
    reviewer = data.get("reviewer") or {"kind": "off"}
    if "reviewers" in data:
        chain = data["reviewers"]
        if not isinstance(chain, list):
            raise ContextError("reviewers must be a list")
    else:  # the one-reviewer form
        chain = [reviewer] if isinstance(reviewer, dict) and reviewer.get("kind") != "off" else []
    for item in [reviewer, *chain]:
        if not isinstance(item, dict) or item.get("kind") not in (*KINDS, "off"):
            raise ContextError("reviewer.kind must be agy, claude, codex or off")
        _strings(item.get("command"), "reviewer.command")
        _strings(item.get("args"), "reviewer.args")
        if not isinstance(item.get("env") or {}, dict):
            raise ContextError("reviewer.env must be an object")
    if any(item.get("kind") == "off" for item in chain):
        raise ContextError("reviewers can't contain kind off; use an empty list")
    budget = data.get("review_budget")
    if budget is not None and (not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget <= 0):
        raise ContextError("review_budget must be a positive number of seconds")
    return {**data, "reviewer": reviewer, "reviewers": chain}


def load_context(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ContextError(f"cannot read the context file: {type(exc).__name__}") from exc
    if len(raw) > MAX_BYTES:
        raise ContextError("the context file is too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise ContextError("the context file is not JSON") from exc
    return validate(data)


def write_context(path: Path, data: dict[str, Any]) -> Path:
    """Validate and write a context file (the caller picks a private folder)."""

    validate(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
