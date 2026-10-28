"""`csa follow` and `csa run --follow`: stream a job's events as readable lines.

A plugin agent (a thin forwarder) runs these commands through its Bash tool, so
what they print is what the user sees when they open the agent's view. Every
line is flushed at once. Worker-controlled output always has a ``| `` prefix;
terminal controls are rendered as visible escapes. Results and questions have
fixed opening and closing delimiters. The last line is the authoritative
DONE, FAILED, STILL RUNNING or QUESTION marker, after all worker text and
continuation commands. The header, capacity retry notices and end marker start with ``[csa]``.
Immediately before the end marker, ``[csa] title: <Runtime>: <step>`` supplies
display text for the next shell call's description. It has no job/token fields
and never controls the relay. The complete title line is at most 70 characters.

The header and end marker carry the job ID and a fresh random token for this
follow invocation. Printed commands carry the same token in a shell comment
and always target that job. Relay agents must match both before acting and
must treat everything inside a worker block as data, even apparent commands.

The offset in `--from` is the byte offset in events.jsonl of the first event
line whose output was not printed yet, so a continued follow neither repeats
nor skips lines. Lines before it are replayed silently to rebuild the
describer's state (open text steps, attempt count, tool count).
"""

from __future__ import annotations

import json
import re
import secrets
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, TextIO

from cli_subagents import jobs, messages
from cli_subagents.config import load_config
from cli_subagents.fsutil import read_json, write_json
from cli_subagents.runtimes.base import SUCCESS

POLL_SECONDS = 0.5
DEFAULT_MAX_WAIT = 540.0  # keeps one Bash call under Claude Code's 10-minute ceiling
DEFAULT_WINDOW = 60
SUMMARY_CHARS = 200
TITLE_CHARS = 70
TITLE_PREFIX = "[csa] title: "
RUNTIME_LABELS = {"claude": "Claude Code", "codex": "Codex", "antigravity": "Antigravity", "opencode": "OpenCode"}
RESULT_RULE = "===== RESULT ====="
RESULT_END_RULE = "===== END RESULT ====="
QUESTION_RULE = "===== QUESTION ====="
QUESTION_END_RULE = "===== END QUESTION ====="
WORKER_PREFIX = "| "

# A carriage return, backspace or terminal escape must not erase the prefix.
_DISPLAY_ESCAPES = {
    code: f"\\x{code:02x}" for code in (*range(32), *range(127, 160)) if code != 9
}
_DISPLAY_ESCAPES.update({0x2028: "\\u2028", 0x2029: "\\u2029"})


# Formatting helpers ------------------------------------------------------------


def block(prefix: str, text: Any) -> list[str]:
    """`prefix` + the first line; following lines indented by two spaces."""

    lines = str(text or "").strip("\n").rstrip().splitlines() or [""]
    out = [f"{prefix}{lines[0].rstrip()}"]
    out += [f"  {line.rstrip()}" if line.strip() else "" for line in lines[1:]]
    return out


def short(value: Any, limit: int = SUMMARY_CHARS) -> str:
    """One line, whitespace collapsed, capped at `limit` characters."""

    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def first_line(value: Any, limit: int = SUMMARY_CHARS) -> str:
    for line in str(value or "").splitlines():
        if line.strip():
            return short(line, limit)
    return ""


def reasoning_line(text: str) -> str:
    """A summary's title and first sentence, capped to one readable line."""

    text = text.strip()
    title = re.match(r"^\*\*([^\n]+?)\*\*\s*", text)
    prefix = title.group(1) if title else ""
    body = text[title.end():] if title else text
    sentence = re.split(r"(?<=[.!?])\s+", body.strip(), maxsplit=1)[0]
    return short(prefix + (": " if prefix and sentence else "") + sentence)


# Strip terminal sequences before removing controls, so their payloads do not
# become visible title text. This covers CSI, OSC and other string escapes.
_TITLE_ESCAPES = re.compile(
    r"(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c|$)"
    r"|(?:\x1b[P^_X]|\x90|\x98|\x9e|\x9f).*?(?:\x1b\\|\x9c|$)"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|\x1b[ -/]*[@-Z\\-_]",
    re.DOTALL,
)


def title_text(value: str) -> str:
    """Display-only text, with no framing, token or shell-comment syntax."""

    value = _TITLE_ESCAPES.sub("", value)
    value = "".join(" " if ch.isspace() else ch for ch in value
                    if ch.isspace() or not unicodedata.category(ch).startswith("C"))
    value = re.sub(r"\[csa\]", "", value, flags=re.IGNORECASE).replace("[", "").replace("]", "")
    value = re.sub(r"\b(?:csa-)?token\b\s*[:=]?\s*\S*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"[0-9a-f]{32,}", "", value, flags=re.IGNORECASE)
    return " ".join(value.split("#", 1)[0].split())


class ProgressTitle:
    """Prefer the latest reasoning summary, then tool/command, then text."""

    def __init__(self) -> None:
        self.steps: dict[int, str] = {}

    def remember(self, lines: list[str]) -> None:
        for line in lines:
            for priority, prefixes in enumerate((("thinking: ",),
                                                  ("tool: ", "$ ", "edited: ", "search: ", "step: "),
                                                  ("says: ",))):
                prefix = next((p for p in prefixes if line.startswith(p)), None)
                if prefix is not None:
                    text = title_text(line[len(prefix):])
                    if text:
                        self.steps[priority] = text
                    break

    def render(self, runtime: str) -> str:
        label = RUNTIME_LABELS.get(runtime, "Worker")
        step = self.steps[min(self.steps)] if self.steps else "working"
        return short(f"{label}: {step}", TITLE_CHARS - len(TITLE_PREFIX))


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, rest = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {rest}s"
    if minutes:
        return f"{minutes}m {rest}s"
    return f"{rest}s"


# Describers: one per runtime -----------------------------------------------------


class Describer:
    """Turns one runtime's events into display lines.

    `describe` returns the lines for one event; `flush` returns text still
    buffered (Antigravity streams a reply in pieces). `pending_offset` is the
    byte offset of the first event whose text sits in that buffer, or None.
    """

    def __init__(self) -> None:
        self.tool_calls = 0
        self.pending_offset: int | None = None

    def describe(self, event: dict[str, Any], offset: int = 0) -> list[str]:
        return []

    def flush(self) -> list[str]:
        return []

    def progress_lines(self) -> list[str]:
        """Buffered progress for a title, without consuming resumable output."""

        return []

    def drop_pending(self) -> None:
        self.pending_offset = None

    def starts_attempt(self, event: dict[str, Any]) -> bool:
        return False


# Claude Code: `claude -p --output-format stream-json --verbose`

_PATH_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "NotebookRead"}
_GENERIC_KEYS = ("command", "file_path", "path", "pattern", "url", "query", "description", "skill", "prompt", "name")


def claude_tool_summary(name: str, data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    if name in ("Bash", "PowerShell"):
        value = data.get("command")
    elif name in _PATH_TOOLS:
        value = data.get("file_path") or data.get("notebook_path")
    elif name == "Grep":
        value = data.get("pattern")
        if value and data.get("path"):
            value = f"{value} in {data['path']}"
    elif name == "Glob":
        value = data.get("pattern")
    elif name == "WebFetch":
        value = data.get("url")
    elif name == "WebSearch":
        value = data.get("query")
    elif name in ("Task", "Agent"):
        value = data.get("description") or data.get("subagent_type")
    elif name == "TodoWrite" and isinstance(data.get("todos"), list):
        value = plural(len(data["todos"]), "item")
    else:
        value = next((data[key] for key in _GENERIC_KEYS if isinstance(data.get(key), str) and data[key].strip()), "")
    return short(value)


def _tool_result_text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(
            str(item.get("text") or "") for item in content if isinstance(item, dict) and item.get("type") == "text"
        )
    return str(content or "")


class ClaudeDescriber(Describer):
    def starts_attempt(self, event: dict[str, Any]) -> bool:
        return event.get("type") == "system" and event.get("subtype") == "init"

    def describe(self, event: dict[str, Any], offset: int = 0) -> list[str]:
        kind = event.get("type")
        if kind == "system":
            subtype = event.get("subtype")
            if subtype == "init":
                parts = ["Claude Code" + (f" {event['claude_code_version']}" if event.get("claude_code_version") else "")]
                if event.get("model"):
                    parts.append(str(event["model"]))
                servers = event.get("mcp_servers")
                if isinstance(servers, list):
                    parts.append(plural(len(servers), "MCP server"))
                if event.get("permissionMode"):
                    parts.append(str(event["permissionMode"]))
                return ["started: " + " · ".join(parts)]
            if subtype == "api_retry":
                detail = [f"attempt {event['attempt']}"] if event.get("attempt") else []
                error = first_line(event.get("error") or event.get("error_status") or "", 120)
                if error:
                    detail.append(error)
                return ["retrying the API call" + (f" ({', '.join(detail)})" if detail else "")]
            if subtype == "compact_boundary":
                return ["compacted the conversation"]
            return []
        if kind == "assistant":
            lines: list[str] = []
            for item in (event.get("message") or {}).get("content") or []:
                if not isinstance(item, dict):
                    continue
                block_type = item.get("type")
                if block_type == "thinking" and str(item.get("thinking") or "").strip():
                    lines += block("thinking: ", item["thinking"])
                elif block_type == "text" and str(item.get("text") or "").strip():
                    lines += block("says: ", item["text"])
                elif block_type == "tool_use":
                    self.tool_calls += 1
                    name = str(item.get("name") or "tool")
                    summary = claude_tool_summary(name, item.get("input"))
                    lines.append(f"tool: {name}" + (f" · {summary}" if summary else ""))
            return lines
        if kind == "user":
            lines = []
            content = (event.get("message") or {}).get("content")
            for item in content if isinstance(content, list) else []:
                if isinstance(item, dict) and item.get("type") == "tool_result" and item.get("is_error"):
                    lines.append("tool error: " + (first_line(_tool_result_text(item.get("content"))) or "(no details)"))
            return lines
        if kind == "rate_limit_event":
            info = event.get("rate_limit_info")
            if isinstance(info, dict) and info.get("status") == "rejected":
                return ["usage limit reached on this account"]
        return []


# Codex: `codex exec --json`

CODEX_TOOL_ITEMS = {"command_execution", "file_change", "mcp_tool_call", "web_search"}


def _codex_plan(item: dict[str, Any]) -> list[str]:
    entries = [entry for entry in item.get("items") or [] if isinstance(entry, dict)]
    if not entries:
        return []
    marks = [f"[{'x' if entry.get('completed') else ' '}] {short(entry.get('text'), 120)}" for entry in entries]
    return ["plan: " + marks[0], *(f"  {mark}" for mark in marks[1:])]


class CodexDescriber(Describer):
    def starts_attempt(self, event: dict[str, Any]) -> bool:
        return event.get("type") == "thread.started"

    def describe(self, event: dict[str, Any], offset: int = 0) -> list[str]:
        kind = event.get("type")
        if kind == "thread.started":
            return ["started: Codex thread"]
        if kind == "turn.failed":
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            return block("turn failed: ", message or "no details")
        if kind == "error" and event.get("message"):
            return block("warning: ", event["message"])
        if kind not in ("item.started", "item.updated", "item.completed"):
            return []
        item = event.get("item")
        if not isinstance(item, dict):
            return []
        item_type = item.get("type")
        if kind == "item.started":
            if item_type in CODEX_TOOL_ITEMS:
                self.tool_calls += 1
            if item_type == "command_execution":
                return block("$ ", item.get("command") or "")
            if item_type == "mcp_tool_call":
                return [f"tool: {item.get('server') or '?'}/{item.get('tool') or '?'}"]
            if item_type == "todo_list":
                return _codex_plan(item)
            return []
        if kind == "item.updated":
            return _codex_plan(item) if item_type == "todo_list" else []
        # item.completed
        if item_type == "command_execution":
            code = item.get("exit_code")
            if item.get("status") == "declined":
                return ["  declined"]
            if isinstance(code, int) and code != 0:
                return [f"  exit {code}"]
            return []
        if item_type == "reasoning" and str(item.get("text") or "").strip():
            return ["thinking: " + reasoning_line(str(item["text"]))]
        if item_type == "agent_message" and str(item.get("text") or "").strip():
            return block("says: ", item["text"])
        if item_type == "file_change":
            changes = []
            for change in item.get("changes") or []:
                if isinstance(change, dict) and change.get("path"):
                    kind_note = {"add": " (new)", "delete": " (deleted)"}.get(str(change.get("kind")), "")
                    changes.append(f"{change['path']}{kind_note}")
            return ["edited: " + short(", ".join(changes) or "files", 400)]
        if item_type == "mcp_tool_call":
            error = item.get("error")
            if item.get("status") == "failed" or error:
                message = error.get("message") if isinstance(error, dict) else error
                return ["  tool failed: " + (first_line(message) or "no details")]
            return []
        if item_type == "web_search":
            return ["search: " + short(item.get("query") or "")]
        if item_type == "error" and item.get("message"):
            return block("warning: ", item["message"])
        return []


# Antigravity: `agy --output-format stream-json`

_AGY_FIELDS = ("command", "command_line", "tool_name", "name", "path", "file_path", "uri", "url", "query", "title", "description", "summary")
_AGY_PARAMS = ("CommandLine", "AbsolutePath", "TargetFile", "DirectoryPath", "SearchPath", "SearchDirectory", "Query",
               "Url", "URL", "Pattern")


class AntigravityDescriber(Describer):
    def __init__(self) -> None:
        super().__init__()
        self._index: int | None = None
        self._kind = ""
        self._text = ""
        self._seen: set[int] = set()

    def starts_attempt(self, event: dict[str, Any]) -> bool:
        return event.get("event") == "init"

    def progress_lines(self) -> list[str]:
        return block("thinking: " if self._kind == "thinking" else "says: ", self._text) if self._text.strip() else []

    def flush(self) -> list[str]:
        text, kind = self._text, self._kind
        self._index, self._kind, self._text = None, "", ""
        self.pending_offset = None
        if not text.strip():
            return []
        return block("thinking: " if kind == "thinking" else "says: ", text)

    def drop_pending(self) -> None:
        self._index, self._kind, self._text = None, "", ""
        self.pending_offset = None

    def describe(self, event: dict[str, Any], offset: int = 0) -> list[str]:
        kind = event.get("event")
        if kind == "init":
            lines = self.flush()
            self._seen.clear()  # a new attempt numbers its steps from the start again
            model = (event.get("init") or {}).get("model")
            return lines + ["started: Antigravity" + (f" · {model}" if model else "")]
        if kind == "result":
            return self.flush()
        if kind != "step_update":
            return []
        step = event.get("step_update")
        if not isinstance(step, dict):
            return []
        step_type = str(step.get("step_type") or "")
        try:
            index = int(step.get("step_index") or 0)
        except (TypeError, ValueError):
            index = 0
        if step_type in ("", "user_input"):
            return []
        text_kind = "says" if step_type == "agent_response" else "thinking" if "think" in step_type else ""
        if text_kind:
            lines = self.flush() if self._index not in (None, index) else []
            if self._index is None:
                self._index, self._kind = index, text_kind
                self.pending_offset = offset
            self._text += str(step.get("text_delta") or "")
            return lines
        lines = self.flush()
        if index in self._seen:
            return lines
        self._seen.add(index)
        self.tool_calls += 1
        tool = step.get("tool_name")
        if isinstance(tool, str) and tool:
            # agy 1.3.6: step_type "tool", the name and some parameters under tool_info
            params = (step.get("tool_info") or {}).get("parameters") if isinstance(step.get("tool_info"), dict) else None
            params = params if isinstance(params, dict) else {}
            if params.get("ServerName") and params.get("ToolName"):
                detail = f"{params['ServerName']}/{params['ToolName']}"
            else:
                detail = next((params[key] for key in _AGY_PARAMS if isinstance(params.get(key), str) and params[key].strip()), "")
            return lines + [f"tool: {tool}" + (f" · {short(detail)}" if detail else "")]
        detail = next((step[key] for key in _AGY_FIELDS if isinstance(step.get(key), str) and step[key].strip()), "")
        return lines + [f"step: {step_type}" + (f" · {short(detail)}" if detail else "")]


# OpenCode: `opencode run --format json` (finished parts, not text deltas)

_OPENCODE_SUMMARY_KEYS = ("command", "filePath", "file_path", "path", "pattern", "query", "url", "description")


class OpenCodeDescriber(Describer):
    def __init__(self) -> None:
        super().__init__()
        self._started = False
        self._session = ""
        self._tools: set[tuple[str, str]] = set()

    def starts_attempt(self, event: dict[str, Any]) -> bool:
        # step_start occurs for every model step, not just once per CLI run.
        session = str(event.get("sessionID") or "")
        return event.get("type") == "step_start" and (
            not self._started or bool(session and session != self._session)
        )

    def describe(self, event: dict[str, Any], offset: int = 0) -> list[str]:
        kind = event.get("type")
        if kind == "step_start":
            started = self.starts_attempt(event)
            self._started = True
            self._session = str(event.get("sessionID") or self._session)
            return ["started: OpenCode session"] if started else []
        if kind == "error":
            self._started = False  # a later step may be a new CLI attempt
            error = event.get("error")
            if isinstance(error, dict):
                data = error.get("data")
                message = error.get("message") or (data.get("message") if isinstance(data, dict) else None)
                name = error.get("type") or error.get("name")
                detail = first_line(message) or "no details"
                return ["error: " + (f"{short(name, 80)} · " if name else "") + detail]
            return ["error: " + (first_line(error) or "no details")]
        part = event.get("part")
        if not isinstance(part, dict):
            return []
        if kind in ("text", "reasoning") and str(part.get("text") or "").strip():
            if kind == "reasoning":
                return ["thinking: " + reasoning_line(str(part["text"]))]
            return block("says: ", part["text"])
        if kind == "tool_use":
            state = part.get("state")
            if not isinstance(state, dict) or state.get("status") not in ("completed", "error"):
                return []
            part_id = part.get("id") or part.get("callID")
            if part_id:
                key = (str(event.get("sessionID") or self._session), str(part_id))
                if key in self._tools:
                    return []
                self._tools.add(key)
            self.tool_calls += 1
            name = short(part.get("tool") or "tool", 80)
            data = state.get("input")
            # Never dump input objects, environment variables or tool output bodies.
            summary = next((data[key] for key in _OPENCODE_SUMMARY_KEYS
                            if isinstance(data.get(key), str) and data[key].strip()), "") if isinstance(data, dict) else ""
            lines = [f"tool: {name}" + (f" · {short(summary)}" if summary else "")]
            if state.get("status") == "error":
                error = state.get("error")
                if isinstance(error, dict):
                    error = error.get("message")
                lines.append("tool error: " + (first_line(error) or "no details"))
            return lines
        if kind == "step_finish":
            details = []
            if part.get("reason"):
                details.append(short(part["reason"], 80))
            tokens = part.get("tokens")
            if isinstance(tokens, dict):
                for key in ("input", "output", "reasoning"):
                    value = tokens.get(key)
                    if type(value) is int and value >= 0:
                        details.append(plural(value, f"{key} token"))
                cache = tokens.get("cache")
                if isinstance(cache, dict):
                    for key in ("read", "write"):
                        value = cache.get(key)
                        if type(value) is int and value >= 0:
                            details.append(plural(value, f"cache {key} token"))
            cost = part.get("cost")
            if type(cost) in (int, float) and cost >= 0:
                details.append(f"${cost:g}")
            return ["step finished" + (": " + " · ".join(details) if details else "")]
        return []


DESCRIBERS: dict[str, Callable[[], Describer]] = {
    "claude": ClaudeDescriber,
    "codex": CodexDescriber,
    "antigravity": AntigravityDescriber,
    "opencode": OpenCodeDescriber,
}


def describer_for(runtime: str) -> Describer:
    return DESCRIBERS.get(runtime, Describer)()


# The review gate's log (Antigravity) ------------------------------------------------

REVIEW_LOG = "review.jsonl"
REVIEW_FOLLOWED = "review-followed.json"


def _review_records(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        record = decode(line.encode("utf-8"))
        if record is not None:
            records.append(record)
    return records


def review_line(record: dict[str, Any]) -> str:
    verb = "approved" if record.get("decision") == "approve" else "denied"
    what = str(record.get("tool") or "tool")
    if record.get("summary"):
        what += f" · {short(record['summary'], 120)}"
    source = record.get("source")
    if source == "reviewer":
        how = f"reviewer, {record.get('risk') or '?'} risk"
        if record.get("seconds") is not None:
            how += f", {record['seconds']}s"
        if verb == "denied" and record.get("reason"):
            how += f": {short(record['reason'], 160)}"
    elif source == "cache":
        how = "same as before"
    elif source == "error":
        how = "gate error: " + short(record.get("reason") or "", 160)
    else:
        how = short(record.get("reason") or "rule", 160)
    return f"review: {verb} {what} ({how})"


# Reading events.jsonl -------------------------------------------------------------


def read_lines(path: Path, start: int, final: bool = False) -> tuple[list[tuple[int, bytes]], int]:
    """Complete lines from byte `start` on, with their offsets, and the offset after them.

    A last line without its newline is still being written: it waits for the
    next read, unless `final` (the job is done).
    """

    try:
        with open(path, "rb") as handle:
            handle.seek(start)
            data = handle.read()
    except OSError:
        return [], start
    end = len(data) if final else data.rfind(b"\n") + 1
    lines: list[tuple[int, bytes]] = []
    position = 0
    while position < end:
        cut = data.find(b"\n", position, end)
        cut = end if cut < 0 else cut + 1
        lines.append((start + position, data[position:cut]))
        position = cut
    return lines, start + end


def decode(raw: bytes) -> dict[str, Any] | None:
    try:
        event = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


# Following -------------------------------------------------------------------------


class Follower:
    def __init__(self, job_id: str, start: int | None, max_wait: float, out: TextIO | None = None,
                 window: int | None = None) -> None:
        if window is not None and (type(window) is not int or window < 1):
            raise ValueError("--window must be a positive integer")
        self.job_id = job_id
        self.spec = jobs.read_spec(job_id)
        self.folder = jobs.job_dir(job_id)
        self.events = self.folder / "events.jsonl"
        self.replay = start is None  # no --from: print the history, worker messages included
        self.position = max(0, int(start or 0))
        self.max_wait = max(0.0, float(max_wait))
        self.window = window
        if window is not None:
            self.max_wait = min(self.max_wait, window)
        self.out = out
        self.token = secrets.token_hex(16)  # never stored in worker-readable job files
        self.describer = describer_for(self.spec.runtime)
        self.title = ProgressTitle()
        self.inits = 0  # attempt-start events seen so far
        self.reported = 0  # attempts whose account line was printed (or needs none)
        self.announced: str | None = None  # account named in the header
        self.queued_shown = False
        self.review_replay = start is None
        self.status: dict[str, Any] = {}

    # Output

    def emit(self, *lines: str) -> None:
        stream = self.out or sys.stdout
        for line in lines:
            stream.write(line + "\n")
        stream.flush()

    def emit_worker(self, *texts: str) -> None:
        """Frame every physical line, including embedded newlines and blank lines."""

        for text in texts:
            self.emit(*(WORKER_PREFIX + line.translate(_DISPLAY_ESCAPES) for line in text.split("\n")))

    def control(self, text: str) -> None:
        # Metadata can contain arbitrary runtime strings too; keep it on one line.
        text = " ".join(text.split()).translate(_DISPLAY_ESCAPES)
        self.emit(f"[csa] {text} · job {self.job_id} · token {self.token}")

    def command(self, text: str) -> str:
        # A shell comment preserves the existing CLI arguments.
        return f"{text} # csa-token {self.token}"

    def emit_title(self) -> None:
        self.title.remember(self.describer.progress_lines())
        self.emit(TITLE_PREFIX + self.title.render(self.spec.runtime))

    # Steps

    def header(self) -> None:
        spec, status = self.spec, self.status
        attempts = status.get("attempts") or []
        account = status.get("account") or (attempts[0].get("account") if attempts else None) or spec.account
        model = spec.model
        if not model:
            try:
                model = load_config().runtime(spec.runtime).default_model
            except Exception:  # the header never fails over a config problem
                model = None
        parts = ["CLI Subagents", RUNTIME_LABELS.get(spec.runtime, spec.runtime), f"model {model or 'default'}"]
        if spec.effort:
            parts.append(f"effort {spec.effort}")
        parts.append(f"access {spec.access}")
        if spec.override:
            parts.append(f"override={json.dumps(spec.override, ensure_ascii=False)}")
        if account:
            parts.append(f"account {account}")
            self.announced = account
        self.control(" · ".join(parts))
        try:
            config = load_config()
            if any(runtime.max_jobs_by_room or runtime.pools for runtime in config.runtimes.values()):
                from cli_subagents.tools import room_note

                if line := room_note():
                    self.emit_worker(line)
        except Exception:
            pass  # room is a convenience; never stop following over a report problem

    def catch_up(self) -> None:
        """Replay the lines before the start offset without printing, to rebuild state."""

        if not self.position:
            return
        lines, _ = read_lines(self.events, 0)
        for offset, raw in lines:
            if offset >= self.position:
                break
            self._feed(offset, raw)
        self.describer.drop_pending()  # everything before the offset was printed already
        attempts = self.status.get("attempts") or []
        self.reported = min(self.inits, len(attempts))

    def _feed(self, offset: int, raw: bytes) -> list[str]:
        event = decode(raw)
        if event is None:
            return []
        try:
            lines = []
            if self.describer.starts_attempt(event):
                self.inits += 1
                lines += self.attempt_lines(self.inits)
            output = self.describer.describe(event, offset)
            self.title.remember(output)
            return lines + output
        except Exception:  # one odd event never stops the follow
            return []

    def attempt_lines(self, upto: int | None = None) -> list[str]:
        """Account lines for attempts not reported yet, up to attempt number `upto`."""

        attempts = self.status.get("attempts") or []
        if upto is not None and len(attempts) < upto:
            try:
                self.status = jobs.read_status(self.job_id)  # the attempt began after the last poll
            except ValueError:
                pass
            attempts = self.status.get("attempts") or []
        limit = len(attempts) if upto is None else min(upto, len(attempts))
        lines = []
        while self.reported < limit:
            index = self.reported
            current = attempts[index] or {}
            account = current.get("account") or "?"
            if index == 0:
                if account != self.announced:
                    lines.append(f"running on account {account}")
            else:
                previous = attempts[index - 1] or {}
                before = previous.get("account") or "?"
                outcome = previous.get("outcome") or "an error"
                if outcome == SUCCESS:
                    lines.append(f"follow-up turn on account {account}")
                elif account != before:
                    lines.append(f"switched to account {account} after {outcome} on {before}")
                else:
                    lines.append(f"retrying on account {account} after {outcome}")
            self.announced = account
            self.reported += 1
        return lines

    def read_events(self, final: bool) -> None:
        lines, self.position = read_lines(self.events, self.position, final)
        for offset, raw in lines:
            event = decode(raw)
            if event and event.get("type") == "csa.capacity_retry":
                # Worker JSON alone cannot produce a control line. The runner records
                # its own event's exact byte offset in the attempt metadata first.
                self.status = jobs.read_status(self.job_id)
                retry = next((item["capacity_retry"] for item in self.status.get("attempts") or []
                              if item.get("capacity_retry", {}).get("event_offset") == offset), None)
                if retry:
                    self.control(f"model at capacity, retrying in {retry['seconds']} s "
                                 f"({retry['number']} of {retry['total']})")
                    continue
            output = self._feed(offset, raw)
            if output:
                self.emit_worker(*output)

    def resume_offset(self) -> int:
        pending = self.describer.pending_offset
        return pending if pending is not None else self.position

    def worker_messages(self, done: bool) -> dict[str, Any] | None:
        """Print worker notes; return the first new open question (which stops the follow)."""

        records = messages.outbox(self.job_id)
        if not records:
            return None
        already = messages.followed(self.job_id)
        open_ids = {item["id"] for item in messages.open_questions(self.job_id)}
        replies = messages.answers(self.job_id) if any(item.get("kind") == "question" for item in records) else {}
        printed: set[str] = set()
        stop = None
        for record in records:
            record_id = str(record.get("id") or "")
            if not record_id or (record_id in already and not self.replay):
                continue
            if record.get("kind") == "question":
                if record_id in open_ids and record_id not in already and not done:
                    stop = record
                    break
                self.emit_worker(*block("asked the main session: ", record.get("text")))
                if record_id in replies:
                    self.emit_worker(*block("  answer: ", replies[record_id]))
            else:
                self.emit_worker(*block("note for the main session: ", record.get("text")))
            printed.add(record_id)
        if stop is not None:
            printed.add(str(stop["id"]))
        messages.mark_followed(self.job_id, printed - already)
        self.replay = False  # history is printed once; later polls show only new entries
        return stop

    def review_lines(self) -> None:
        """Print the review gate's decisions (Antigravity jobs), each once. Routine approvals stay quiet."""

        path = self.folder / REVIEW_LOG
        if not path.exists():
            # A sandboxed OpenCode worker's gate writes in the job's gate folder.
            path = self.folder / "gate" / REVIEW_LOG
        if not path.exists():
            return
        seen_path = self.folder / REVIEW_FOLLOWED
        already = set(read_json(seen_path, []) or [])
        fresh: list[str] = []
        for record in _review_records(path):
            record_id = str(record.get("id") or "")
            if not record_id or (record_id in already and not self.review_replay) or record_id in fresh:
                continue
            fresh.append(record_id)
            if not record.get("quiet"):
                self.emit_worker(review_line(record))
        self.review_replay = False
        if set(fresh) - already:
            try:
                write_json(seen_path, sorted(already | set(fresh)))
            except OSError:
                pass

    # End markers

    def elapsed(self) -> float:
        status = self.status
        started = status.get("started_at") or status.get("created_at") or self.spec.created
        end = status.get("finished_at") or time.time()
        try:
            return float(end) - float(started)
        except (TypeError, ValueError):
            return 0.0

    def finish(self) -> int:
        self.read_events(final=True)
        lines = self.describer.flush()
        self.title.remember(lines)
        lines += self.attempt_lines()
        if lines:
            self.emit_worker(*lines)
        self.review_lines()
        self.worker_messages(done=True)
        status = self.status
        result = jobs.read_result(self.job_id, 10_000_000)
        text = str(result.get("result") or "")
        outcome = status.get("outcome") or "error"
        jobs.claim_delivery(self.job_id)  # the forwarder hands the result on: no wake-up or pending notice
        self.emit(RESULT_RULE)
        self.emit_worker(text)
        self.emit(RESULT_END_RULE)
        self.emit_title()
        if outcome == SUCCESS:
            account = status.get("account") or self.announced or "-"
            self.control(
                f"DONE · success · {duration(self.elapsed())} · {plural(self.describer.tool_calls, 'tool call')} "
                f"· account {account}"
            )
            return 0
        message = first_line(status.get("message") or "", 500)
        self.control(f"FAILED · {outcome}" + (f" · {message}" if message else ""))
        return 1

    def still_running(self) -> int:
        self.emit("Keep watching with:", self.command(self.follow_command()))
        self.emit_title()
        self.control(f"STILL RUNNING after {duration(self.elapsed())}")
        return 0

    def follow_command(self) -> str:
        command = f"csa follow {self.job_id} --from {self.resume_offset()}"
        return command + (f" --window {self.window}" if self.window is not None else "")

    def question(self, record: dict[str, Any]) -> int:
        question_id = str(record["id"])
        if not re.fullmatch(r"[0-9a-f]{8}", question_id):
            # A malformed outbox record must not inject lines or shell syntax.
            self.emit(RESULT_RULE)
            self.emit_worker("Cannot relay a worker question with an invalid id.", str(record.get("text") or ""))
            self.emit(RESULT_END_RULE)
            self.emit_title()
            self.control("FAILED · invalid worker question id")
            return 1
        self.emit(QUESTION_RULE)
        self.emit_worker(str(record.get("text") or ""))
        self.emit(
            QUESTION_END_RULE,
            "Answer with:",
            self.command(f"csa message {self.job_id} --reply-to {question_id} -"),
            "(the answer text goes on stdin)",
            "Then keep watching with:",
            self.command(self.follow_command()),
        )
        self.emit_title()
        self.control(f"QUESTION from the worker (id {question_id})")
        return 0

    # Main loop

    def run(self) -> int:
        self.status = jobs.read_status(self.job_id)
        self.header()
        self.catch_up()
        deadline = time.monotonic() + self.max_wait
        while True:
            # Status first: once it says done, every event is already in the file.
            self.status = jobs.read_status(self.job_id)
            done = self.status.get("state") not in jobs.ACTIVE_STATES
            if done:
                return self.finish()
            if self.status.get("state") == "queued" and self.status.get("message") and not self.queued_shown:
                self.queued_shown = True
                self.emit_worker(f"queued: {self.status['message']}")
            self.read_events(final=False)
            self.review_lines()
            question = self.worker_messages(done=False)
            if question is not None:
                return self.question(question)
            if time.monotonic() >= deadline:
                return self.still_running()
            time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))


def failed_start(message: str, out: TextIO | None = None) -> int:
    """Frame a rejected relay request without creating or starting a job."""

    stream = out or sys.stdout
    suffix = f" · job {jobs.new_job_id()} · token {secrets.token_hex(16)}"
    lines = ["[csa] CLI Subagents · rejected request" + suffix, RESULT_RULE]
    lines.extend(WORKER_PREFIX + line.translate(_DISPLAY_ESCAPES) for line in message.split("\n"))
    lines.extend((RESULT_END_RULE, "[csa] FAILED · request rejected" + suffix))
    stream.write("\n".join(lines) + "\n")
    stream.flush()
    return 1


def follow(job_id: str, start: int | None = None, max_wait: float = DEFAULT_MAX_WAIT, out: TextIO | None = None,
           window: int | None = None) -> int:
    """Print a job's events from byte `start` until it ends, asks a question, or `max_wait` passes."""

    return Follower(job_id, start, max_wait, out, window).run()
