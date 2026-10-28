"""Two-way messages between the main session and a running subagent.

Each job's `channel` folder holds two append-only logs:

    outbox.jsonl   worker -> main session: progress notes and questions
    inbox.jsonl    main session -> worker: follow-ups and answers

plus small JSON files that remember which entries each side has already seen,
so every message is delivered once. The csa-parent tools run on the host, like
OpenCode and the review gate. Sandboxed shell commands cannot access the job
folder, including its channel, status, spec and review files.
Jobs created before the channel folder existed keep their files in the job folder.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

from cli_subagents.fsutil import file_lock, make_private_dir, read_json, write_json
from cli_subagents.jobs import job_dir

MAX_TEXT = 20_000
CHANNEL = "channel"
LEGACY_FILES = ("inbox.jsonl", "outbox.jsonl")


def channel_dir(job_id: str) -> Path:
    """Where a job's message files live (created on first write)."""

    folder = job_dir(job_id)
    channel = folder / CHANNEL
    if not channel.is_dir() and any((folder / name).exists() for name in LEGACY_FILES):
        return folder
    return channel


def _append(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    with file_lock(path.with_suffix(".lock")):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _take_unseen(job_id: str, log: str, seen_file: str) -> list[dict[str, Any]]:
    folder = channel_dir(job_id)
    seen_path = folder / seen_file
    with file_lock(seen_path.with_suffix(".lock")):
        seen = set(read_json(seen_path, []) or [])
        fresh = [record for record in _read(folder / log) if record.get("id") not in seen]
        if fresh:
            write_json(seen_path, sorted(seen | {record["id"] for record in fresh}))
    return fresh


def _mark_seen(job_id: str, seen_file: str, ids: set[str]) -> None:
    seen_path = channel_dir(job_id) / seen_file
    with file_lock(seen_path.with_suffix(".lock")):
        seen = set(read_json(seen_path, []) or [])
        if not ids <= seen:
            write_json(seen_path, sorted(seen | ids))


def _record(kind: str, text: str, **extra: Any) -> dict[str, Any]:
    text = str(text or "").strip()
    if not text:
        raise ValueError("message text is empty")
    return {"id": secrets.token_hex(4), "time": time.time(), "kind": kind, "text": text[:MAX_TEXT], **extra}


# Worker side ------------------------------------------------------------------


def post_from_worker(job_id: str, text: str, kind: str = "message") -> dict[str, Any]:
    return _append(channel_dir(job_id) / "outbox.jsonl", _record(kind, text))


def take_for_worker(job_id: str) -> list[dict[str, Any]]:
    """Follow-ups and answers the worker hasn't read yet."""

    return _take_unseen(job_id, "inbox.jsonl", "inbox-read.json")


def close_or_continue(job_id: str, session_id: str | None, account: str | None,
                      can_continue: bool = False) -> list[dict[str, Any]]:
    """Atomically take a follow-up turn or close acceptance before the final drain."""

    folder = job_dir(job_id)
    with file_lock(folder / "inbox.lock"):
        if can_continue:
            pending = take_for_worker(job_id)
            if pending:
                return pending
        if not (folder / "inbox-closed.json").exists():
            write_json(folder / "inbox-closed.json", {"session_id": session_id, "account": account})
    return []


def queue_for_worker(job_id: str, text: str, reply_to: str | None = None) -> dict[str, Any] | None:
    """Accept a message only while the runner can still drain its inbox."""

    folder = job_dir(job_id)
    record = _record("answer" if reply_to else "follow_up", text, reply_to=reply_to)
    with file_lock(folder / "inbox.lock"):
        if (folder / "inbox-closed.json").exists():
            return None
        inbox = channel_dir(job_id) / "inbox.jsonl"
        make_private_dir(inbox.parent)
        with inbox.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def take_for_hook(job_id: str) -> list[dict[str, Any]]:
    """Inbox entries the worker's PostToolUse hook hasn't shown yet (doesn't mark them read)."""

    read = set(read_json(channel_dir(job_id) / "inbox-read.json", []) or [])
    return [item for item in _take_unseen(job_id, "inbox.jsonl", "inbox-hooked.json") if item.get("id") not in read]


def wait_for_reply(job_id: str, question_id: str, timeout: float) -> dict[str, Any] | None:
    """The answer to one question. Marks only that answer read: other inbox entries
    stay for check_parent_messages, the inbox hook and the runner's follow-up turn."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for record in _read(channel_dir(job_id) / "inbox.jsonl"):
            if record.get("reply_to") == question_id:
                _mark_seen(job_id, "inbox-read.json", {record["id"]})
                return record
        time.sleep(1.0)
    return None


# Main-session side --------------------------------------------------------------


def post_to_worker(job_id: str, text: str, reply_to: str | None = None) -> dict[str, Any]:
    return _append(channel_dir(job_id) / "inbox.jsonl", _record("answer" if reply_to else "follow_up", text, reply_to=reply_to))


def take_for_host(job_id: str) -> list[dict[str, Any]]:
    """Worker messages the main session hasn't been shown yet."""

    return _take_unseen(job_id, "outbox.jsonl", "outbox-delivered.json")


def has_new_for_host(job_id: str) -> bool:
    folder = channel_dir(job_id)
    seen = set(read_json(folder / "outbox-delivered.json", []) or [])
    return any(record.get("id") not in seen for record in _read(folder / "outbox.jsonl"))


def outbox(job_id: str) -> list[dict[str, Any]]:
    """Every worker message, oldest first."""

    return _read(channel_dir(job_id) / "outbox.jsonl")


def answers(job_id: str) -> dict[str, str]:
    """Answers the main session sent, by question id."""

    return {
        str(record["reply_to"]): str(record.get("text") or "")
        for record in _read(channel_dir(job_id) / "inbox.jsonl")
        if record.get("reply_to")
    }


def followed(job_id: str) -> set[str]:
    """Outbox entries a `csa follow` already printed (its own cursor, beside the host's)."""

    return set(read_json(channel_dir(job_id) / "outbox-followed.json", []) or [])


def mark_followed(job_id: str, ids: set[str]) -> None:
    """A follower printed these entries: the host has them now, so they count as delivered too."""

    if ids:
        _mark_seen(job_id, "outbox-followed.json", ids)
        _mark_seen(job_id, "outbox-delivered.json", ids)


def open_questions(job_id: str) -> list[dict[str, Any]]:
    folder = channel_dir(job_id)
    answered = {record.get("reply_to") for record in _read(folder / "inbox.jsonl")}
    return [record for record in _read(folder / "outbox.jsonl") if record.get("kind") == "question" and record["id"] not in answered]
