"""The relay's first line: which model relays for which worker model, read from logs.

Nothing here comes from the prompt or the relay's instructions. The relay's own
model and effort come from the Claude Code transcript of the agent that runs
csa, where Claude Code records the model that answered each request and the
effort it sent. The worker's come from its CLI's own records: Claude Code's
init event and session transcript, Codex's rollout file (`turn_context`) and
Antigravity's init event. OpenCode's JSON output does not name the model, so
the line says so. Every value is reduced to a short plain token before display.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from cli_subagents.fsutil import read_json

TAIL_BYTES = 4_000_000  # read from the end of a transcript or rollout file
SEARCH_BYTES = 64_000_000  # read from one relay transcript when looking for its marker
CANDIDATES = 40  # newest subagent transcripts of the session that are searched
LABELS = {"claude": "Claude Code", "codex": "Codex", "antigravity": "Antigravity", "opencode": "OpenCode"}
SOURCES = {"claude": "the worker's transcript", "codex": "Codex's session log", "antigravity": "the worker's start event"}

_NOT_TOKEN = re.compile(r"[^A-Za-z0-9._:/@+-]")
_CLAUDE_MODEL = re.compile(r"claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?")


def clean(value: Any) -> str | None:
    """A model or effort name as a short plain token, or None."""

    text = str(value or "").strip()
    if not text or text.startswith("<"):  # Claude Code's "<synthetic>" entries name no model
        return None
    return _NOT_TOKEN.sub("", text.split("[", 1)[0])[:80] or None


def display_model(model: str) -> str:
    """`claude-haiku-5-5` reads as "Haiku 5.5"; other names stay as reported."""

    match = _CLAUDE_MODEL.fullmatch(model)
    if not match:
        return model
    family, major, minor = match.groups()
    return f"{family.capitalize()} {major}" + (f".{minor}" if minor else "")


def _letters(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _tail_lines(path: Path, limit: int = TAIL_BYTES) -> list[bytes]:
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        lines = handle.read().splitlines()
    return lines[1:] if size > limit else lines  # the first line may be cut


def transcript_model(path: Path) -> tuple[str, str | None] | None:
    """(model, effort) of the last answered request in a Claude Code transcript."""

    try:
        lines = _tail_lines(path)
    except OSError:
        return None
    for raw in reversed(lines):
        if b'"assistant"' not in raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        model = clean(message.get("model")) if isinstance(message, dict) else None
        if model:
            return model, clean(entry.get("effort"))
    return None


def rollout_settings(path: Path) -> tuple[str, str | None] | None:
    """(model, effort) from the last `turn_context` in a Codex rollout file."""

    try:
        lines = _tail_lines(path)
    except OSError:
        return None
    for raw in reversed(lines):
        if b'"turn_context"' not in raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        payload = entry.get("payload") if isinstance(entry, dict) and entry.get("type") == "turn_context" else None
        model = clean(payload.get("model")) if isinstance(payload, dict) else None
        if model:
            return model, clean(payload.get("effort"))
    return None


def relay_transcript(markers: list[str], env: Mapping[str, str] | None = None) -> Path | None:
    """The transcript of the Claude Code subagent that runs csa: the newest one naming a marker.

    Claude Code stores a session's subagents under
    `<config>/projects/<project>/<session>/subagents/agent-<id>.jsonl`. The
    markers (the relay's inbox file name, the job ID) appear in the relay's own
    tool calls and results, so only that agent's transcript contains them.
    """

    env = os.environ if env is None else env
    session = env.get("CLAUDE_CODE_SESSION_ID") or ""
    needles = [marker.encode() for marker in markers if marker and re.fullmatch(r"[A-Za-z0-9._-]+", marker)]
    if not needles or not re.fullmatch(r"[A-Za-z0-9-]{8,80}", session):
        return None
    root = Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    try:
        found = [(path.stat().st_mtime, path) for path in root.glob(f"*/{session}/subagents/agent-*.jsonl")]
    except OSError:
        return None
    for _mtime, path in sorted(found, reverse=True)[:CANDIDATES]:
        meta = read_json(path.with_name(path.name[: -len(".jsonl")] + ".meta.json"), {}) or {}
        agent_type = str(meta.get("agentType") or "") if isinstance(meta, dict) else ""
        if agent_type and not agent_type.startswith("cli-subagents:"):
            continue
        try:
            with open(path, "rb") as handle:
                data = handle.read(SEARCH_BYTES)
        except OSError:
            continue
        if any(needle in data for needle in needles):
            return path
    return None


def relay_identity(markers: list[str], env: Mapping[str, str] | None = None) -> tuple[str, str | None] | None:
    path = relay_transcript(markers, env)
    return transcript_model(path) if path else None


class WorkerRecord:
    """What the worker's own events say about it, collected while following."""

    def __init__(self, runtime: str) -> None:
        self.runtime = runtime
        self.model: str | None = None
        self.session = ""
        self.answered = False  # the worker produced output after starting

    def observe(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if self.runtime == "claude":
            if kind == "system" and event.get("subtype") == "init" and not self.session:
                self.model = clean(event.get("model"))
                self.session = str(event.get("session_id") or "")
            elif kind == "assistant" and self.session:
                self.answered = True
        elif self.runtime == "codex":
            if kind == "thread.started" and not self.session:
                self.session = str(event.get("thread_id") or "")
            elif kind in ("item.started", "item.completed") and self.session:
                self.answered = True
        elif self.runtime == "antigravity":
            if event.get("event") == "init" and not self.answered:
                init = event.get("init")
                self.model = clean(init.get("model")) if isinstance(init, dict) else None
                self.answered = True


@dataclass
class Worker:
    model: str | None
    effort: str | None
    source: str | None


def _account_home(runtime: str, account: str | None) -> Path | None:
    from cli_subagents.config import load_config

    try:
        found = load_config().runtime(runtime).find(account or "primary")
    except Exception:  # identity is display text; a config problem never stops a follow
        return None
    return found.home if found else None


def worker_identity(record: WorkerRecord, account: str | None, final: bool = False) -> Worker | None:
    """The worker's model and effort, or None while its records aren't written yet."""

    runtime = record.runtime
    if runtime == "opencode":
        return Worker(None, None, None)
    if runtime == "claude":
        if not (record.answered or final):
            return None  # the transcript entry follows the first answer
        home = _account_home(runtime, account) if record.session else None
        if home and re.fullmatch(r"[A-Za-z0-9-]+", record.session):
            for path in (home / "projects").glob(f"*/{record.session}.jsonl"):
                found = transcript_model(path)
                if found:
                    return Worker(found[0], found[1], SOURCES["claude"])
        return Worker(record.model, None, "the worker's start event" if record.model else None)
    if runtime == "codex":
        if record.session:
            from cli_subagents.runtimes.codex import find_rollout

            home = _account_home(runtime, account)
            rollout = find_rollout(home, record.session) if home else None
            found = rollout_settings(rollout) if rollout else None
            if found:
                return Worker(found[0], found[1], SOURCES["codex"])
        return Worker(None, None, None) if (record.answered or final) else None
    if runtime == "antigravity":
        if record.model:
            return Worker(record.model, None, SOURCES["antigravity"])
        return Worker(None, None, None) if final else None
    return Worker(None, None, None)


def sentence(relay: tuple[str, str | None] | None, worker: Worker, runtime: str,
             asked_model: str | None, asked_effort: str | None) -> str:
    """One plain line for the relay to copy as its first message."""

    label = LABELS.get(runtime, runtime)
    me = "I'm the relay agent"
    if relay:
        me = f"I'm {display_model(relay[0])}" + (f" (effort {relay[1]})" if relay[1] else "") + ", the relay agent"
    target = label
    if worker.model:
        target += f" {display_model(worker.model)}" + (f" (effort {worker.effort})" if worker.effort else "")
    text = f"{me} for {target}."
    sources = (["my transcript"] if relay else []) + ([worker.source] if worker.model and worker.source else [])
    if sources:
        text += " Read from " + " and ".join(sources) + "."
    asked_model, asked_effort = clean(asked_model), clean(asked_effort)
    if not worker.model:
        text += f" {label} didn't report which model it runs" + (f"; asked for {asked_model}." if asked_model else ".")
    else:
        if asked_model and _letters(asked_model) not in _letters(worker.model):
            text += f" Asked for {asked_model}, but the worker reports {worker.model}."
        if asked_effort and worker.effort and asked_effort.lower() != worker.effort.lower():
            text += f" Asked for effort {asked_effort}, but the worker reports {worker.effort}."
    if not relay:
        text += " My own model isn't visible from here."
    return text
