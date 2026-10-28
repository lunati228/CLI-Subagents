"""The review gate's reviewer chain, shared by the Antigravity and OpenCode runtimes.

The gate (cli_subagents.agy_review) calls the reviewers itself; this module turns
the configured chain (config.ReviewerEntry, see RuntimeConfig.review_chain) into
the `reviewers` list of a job's review context: which CLI, model, account folder
and environment each tool-less reviewer call uses.

A reviewer whose CLI isn't installed or set up is left out. For the built-in
defaults that happens quietly (`csa doctor` lists it); for a reviewer the user
configured, the job gets a warning.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable

from cli_subagents.config import DEFAULT_REVIEWERS, ReviewerEntry, RuntimeConfig, load_config
from cli_subagents.fsutil import make_private_dir

REVIEW_TIMEOUT = 75  # seconds for one reviewer call, before the chain's time budget cuts it
CLAUDE_REVIEWER_MODEL = "haiku"
AGY_REVIEWER_MODEL = "gemini-3.8-flash-low"
# Seconds the whole chain may take for one gate check. agy gives its hook 90 s
# (agy_review.install.HOOK_TIMEOUT); OpenCode's plugin waits REVIEW_HOOK_SECONDS.
AGY_REVIEW_BUDGET = 80
OPENCODE_REVIEW_BUDGET = 240

# (command, reviewer folder or None) for an agy entry without `account` in an Antigravity
# job: the job's own account, whose reviewer folder its prepare step made.
OwnAgy = tuple[list[str], str | None]


class Unavailable(Exception):
    """This reviewer can't run here (CLI missing, account unknown, login not usable)."""


def label(entry: ReviewerEntry) -> str:
    text = entry.provider + (f" {entry.model}" if entry.model else "")
    return text + (f" (account {entry.account})" if entry.account else "")


def _claude(entry: ReviewerEntry, base: dict[str, Any], *, home: str | None, unset_env: Iterable[str],
            dry: bool) -> dict[str, Any]:
    from cli_subagents.runtimes.claude import ClaudeRuntime

    claude_config = load_config().runtime("claude")
    account = claude_config.find(entry.account or "primary")
    if account is None:
        raise Unavailable(f"'{entry.account}' is not a Claude account")
    runtime = ClaudeRuntime()
    command = runtime.find_binary(claude_config)
    if not command:
        raise Unavailable("the claude CLI isn't installed")
    if dry:
        return {}
    env = {key: value for key, value in runtime.account_env(claude_config, account).items() if key == "CLAUDE_CONFIG_DIR"}
    if home:
        env.update(HOME=home, USERPROFILE=home)
    result = {**base, "command": command, "model": entry.model or CLAUDE_REVIEWER_MODEL, "env": env}
    unset = list(unset_env)
    if unset:
        result["unset_env"] = unset
    return result


def _agy(entry: ReviewerEntry, base: dict[str, Any], *, own: OwnAgy | None, dry: bool,
         warnings: list[str]) -> dict[str, Any]:
    model = entry.model or AGY_REVIEWER_MODEL
    if entry.account is None and own is not None:
        command, folder = own
        if not command:
            raise Unavailable("the agy CLI isn't installed")
        env = {"HOME": folder, "USERPROFILE": folder} if folder else {}
        return {} if dry else {**base, "command": command, "model": model, "env": env}
    from cli_subagents.runtimes.antigravity import AntigravityRuntime

    agy_config = load_config().runtime("antigravity")
    account = agy_config.find(entry.account or "primary")
    if account is None:
        raise Unavailable(f"'{entry.account}' is not an Antigravity account")
    runtime = AntigravityRuntime()
    command = runtime.find_binary(agy_config)
    if not command:
        raise Unavailable("the agy CLI isn't installed")
    if runtime.worker_home(agy_config, account) is None:
        raise Unavailable("its login is a token file in your home folder, so it can't run from a reviewer folder")
    if dry:
        return {}
    folder = runtime.standalone_reviewer_home(agy_config, account, warnings)
    if folder is None:
        raise Unavailable("its reviewer folder could not be prepared")
    return {**base, "command": command, "model": model, "env": {"HOME": str(folder), "USERPROFILE": str(folder)}}


def _codex(entry: ReviewerEntry, base: dict[str, Any], *, home: str | None, unset_env: Iterable[str],
           dry: bool) -> dict[str, Any]:
    from cli_subagents.runtimes.codex import CodexRuntime

    codex_config = load_config().runtime("codex")
    account = codex_config.find(entry.account or "primary")
    if account is None:
        raise Unavailable(f"'{entry.account}' is not a Codex account")
    runtime = CodexRuntime()
    command = runtime.find_binary(codex_config)
    if not command:
        raise Unavailable("the codex CLI isn't installed")
    if not Path(account.home).expanduser().is_dir():
        raise Unavailable(f"its folder {account.home} doesn't exist")
    if dry:
        return {}
    env = {"CODEX_HOME": str(Path(account.home).expanduser())}
    if home:
        env.update(HOME=home, USERPROFILE=home)
    result = {**base, "command": command, "args": runtime._auth_args(account), "env": env}
    if entry.model:
        result["model"] = entry.model
    unset = [key for key in unset_env if key != "CODEX_HOME"]
    if unset:
        result["unset_env"] = unset
    return result


def _resolve(entry: ReviewerEntry, base: dict[str, Any], *, own_agy: OwnAgy | None, home: str | None,
             unset_env: Iterable[str], dry: bool, warnings: list[str]) -> dict[str, Any]:
    if entry.kind == "claude":
        return _claude(entry, base, home=home, unset_env=unset_env, dry=dry)
    if entry.kind == "agy":
        return _agy(entry, base, own=own_agy, dry=dry, warnings=warnings)
    if entry.kind == "codex":
        return _codex(entry, base, home=home, unset_env=unset_env, dry=dry)
    raise Unavailable(f"{entry.kind} can't be a reviewer")


def build_chain(config: RuntimeConfig, job: Path, warnings: list[str], *, own_agy: OwnAgy | None = None,
                home: str | None = None, unset_env: Iterable[str] = ()) -> list[dict[str, Any]]:
    """The context's `reviewers` list for one job. `home` and `unset_env` give the reviewers the
    real home folder when the worker runs with a managed one (Antigravity)."""

    entries, defaults = config.review_chain()
    if not entries:
        return []
    work = job / "reviewer"
    make_private_dir(work)
    unset = list(unset_env)
    chain = []
    for entry in entries:
        base = {"kind": entry.kind, "timeout": entry.timeout or REVIEW_TIMEOUT, "work_dir": str(work)}
        if entry.account:
            base["account"] = entry.account
        try:
            chain.append(_resolve(entry, base, own_agy=own_agy, home=home, unset_env=unset, dry=False, warnings=warnings))
        except Unavailable as exc:
            if not defaults:
                warnings.append(f"reviewer {label(entry)} is left out: {exc}")
    if not chain:
        warnings.append("no reviewer is available, so actions the review gate's fixed rules can't settle are denied"
                        + ("" if not defaults else " (install and log in to claude or agy, or configure [review])"))
    return chain


def describe_chain(config: RuntimeConfig, *, own_agy: Callable[[], OwnAgy | None] | None = None) -> list[str]:
    """One line per configured reviewer for `csa doctor`: ready, or why it is skipped."""

    entries, defaults = config.review_chain()
    if not entries:
        return ["reviewer: off (actions the fixed rules can't settle are denied)"]
    lines = [f"reviewer chain ({'defaults' if defaults else 'configured'}):"]
    for index, entry in enumerate(entries, 1):
        try:
            own = own_agy() if own_agy and entry.kind == "agy" and entry.account is None else None
            _resolve(entry, {}, own_agy=own, home=None, unset_env=(), dry=True, warnings=[])
            state = "ready"
        except Unavailable as exc:
            state = f"skipped, {exc}"
        except Exception as exc:  # doctor must not fail on a broken config
            state = f"skipped, {type(exc).__name__}: {exc}"
        lines.append(f"  {index}. {label(entry)}: {state}")
    return lines


__all__ = ["AGY_REVIEW_BUDGET", "CLAUDE_REVIEWER_MODEL", "DEFAULT_REVIEWERS", "OPENCODE_REVIEW_BUDGET", "REVIEW_TIMEOUT",
           "build_chain", "describe_chain"]
