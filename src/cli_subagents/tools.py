"""Tool definitions and handlers shared by the MCP server and the CLI."""

from __future__ import annotations

import time
from typing import Any

from cli_subagents import accounts, jobs, limits, messages, plans, quota, spend, usage_view
from cli_subagents.config import load_config
from cli_subagents.hosts import detect_host
from cli_subagents.runtimes import AVAILABLE as RUNTIMES, get_runtime
from cli_subagents.runtimes.base import ACCESS_LEVELS

MAX_WAIT_SECONDS = 600
MAX_LONG_WAIT_SECONDS = 3600
# Codex ends an MCP tool call after its tool timeout (300 s in its source, 60 s in
# its docs), so a Codex host never blocks longer than this.
CODEX_MAX_WAIT_SECONDS = 240
MAX_RESULT_CHARS = 80000
PING_SECONDS = 15
WAIT_PING_SECONDS = 3


class ToolError(Exception):
    """A user-facing error returned to the host as a failed tool call."""


def _schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


JOB_ID = {"type": "string", "description": "Job ID returned by subagent_start."}
WAIT = {
    "type": "integer",
    "minimum": 0,
    "maximum": MAX_WAIT_SECONDS,
    "description": "Wait up to this many seconds for the job to finish before returning.",
}

DEFINITIONS = [
    {
        "name": "subagent_start",
        "description": (
            "Start a subagent: another installed agent CLI works on one task in the background, "
            "under one of your accounts, with your shared MCP servers, plugins, skills and settings. "
            "Returns a job_id at once (or the result, if it finishes within wait_seconds). "
            "Give it a complete, self-contained task: goal, scope, files, and how to check the result. "
            "csa enforces the account limits itself: if a start is refused, follow the suggestion in the message. "
            "In Claude Code, use the Agent tool with a cli-subagents-* agent type instead, so the user sees the job."
        ),
        "inputSchema": _schema(
            {
                "prompt": {"type": "string", "description": "The full task for the subagent."},
                "cwd": {"type": "string", "description": "Absolute path of the project folder to work in."},
                "runtime": {"type": "string", "enum": list(RUNTIMES), "default": "claude"},
                "account": {"type": "string", "description": "Run on this account only. Omit to let the pool pick."},
                "model": {"type": "string", "description": "Model alias or ID, e.g. 'sonnet', 'opus', 'haiku'."},
                "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max"]},
                "override": {
                    "type": "string", "minLength": 1, "pattern": "\\S",
                    "description": "The user's own words authorizing more parallel jobs, e.g. 'user asked for 6 Codex agents'. "
                    "Bypasses only csa's caps and queueing, never cooldowns, model or effort restrictions, login, depth or access. "
                    "Never set this on your own judgment. Use it only when the user asked for more jobs in this conversation "
                    "or confirmed after you warned them and told them how much of the 5-hour window is left.",
                },
                "access": {
                    "type": "string",
                    "enum": list(ACCESS_LEVELS),
                    "default": "edit",
                    "description": "read: no edits (read-only tools and commands only). edit: edits with the CLI's own safety checks (auto mode). full: skip all permission checks; only when the user asked for it.",
                },
                "max_turns": {"type": "integer", "minimum": 1},
                "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 21600, "default": 1800},
                "add_dirs": {"type": "array", "items": {"type": "string"}, "description": "Extra absolute folders the subagent may use."},
                "resume_job": {"type": "string", "description": "Continue the session of an earlier job (same account)."},
                "wait_seconds": WAIT,
            },
            ["prompt", "cwd"],
        ),
    },
    {
        "name": "subagent_wait",
        "description": (
            "Wait until one of the given jobs finishes, or a subagent sends a message or question, then "
            "return what happened. In Claude Code a long wait moves to the background on its own and you "
            "get a notification when it returns, so call this after starting jobs instead of polling."
        ),
        "inputSchema": _schema(
            {
                "job_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_LONG_WAIT_SECONDS, "default": 1800},
            },
            ["job_ids"],
        ),
    },
    {
        "name": "subagent_message",
        "description": (
            "Send a message to a subagent. While it runs: a follow-up or an answer to its question "
            "(set reply_to to the question's id). After it finished: continues the same conversation as a "
            "new job and returns that job."
        ),
        "inputSchema": _schema(
            {
                "job_id": JOB_ID,
                "text": {"type": "string"},
                "reply_to": {"type": "string", "description": "Id of the question you are answering."},
            },
            ["job_id", "text"],
        ),
    },
    {
        "name": "subagent_status",
        "description": "State and progress of a job. Use wait_seconds instead of polling in a tight loop.",
        "inputSchema": _schema({"job_id": JOB_ID, "wait_seconds": WAIT}, ["job_id"]),
    },
    {
        "name": "subagent_result",
        "description": "Final answer, outcome, account, session and cost of a finished job.",
        "inputSchema": _schema(
            {"job_id": JOB_ID, "max_chars": {"type": "integer", "minimum": 500, "maximum": MAX_RESULT_CHARS, "default": 20000}},
            ["job_id"],
        ),
    },
    {
        "name": "subagent_cancel",
        "description": "Stop a running job and every process it started.",
        "inputSchema": _schema({"job_id": JOB_ID}, ["job_id"]),
    },
    {
        "name": "subagent_list",
        "description": "Recent jobs, newest first.",
        "inputSchema": _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}}, []),
    },
    {
        "name": "subagent_accounts",
        "description": "Installed runtimes, their accounts, login state, last known usage and cooldowns. Makes no model calls.",
        "inputSchema": _schema({"runtime": {"type": "string", "enum": list(RUNTIMES)}}, []),
    },
]


WORKER_DEFINITIONS = [
    {
        "name": "message_parent",
        "description": "Send a short progress note or an early finding to the main session that started you. It doesn't wait for a reply.",
        "inputSchema": _schema({"text": {"type": "string"}}, ["text"]),
    },
    {
        "name": "ask_parent",
        "description": (
            "Ask the main session a question and wait for the answer. Only for decisions you can't make "
            "from the task and the files; otherwise keep working."
        ),
        "inputSchema": _schema(
            {"question": {"type": "string"}, "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 1800, "default": 600}},
            ["question"],
        ),
    },
    {
        "name": "check_parent_messages",
        "description": "Read follow-up instructions the main session sent while you were working. Check between major steps.",
        "inputSchema": _schema({}, []),
    },
]


def definitions(worker_job: str | None = None) -> list[dict[str, Any]]:
    return WORKER_DEFINITIONS if worker_job else DEFINITIONS


def startup() -> None:
    try:
        jobs.clean_old_jobs(load_config().job_retention_days)
    except Exception:
        pass


def summarize(status: dict[str, Any]) -> dict[str, Any]:
    """The parts of a job status a host needs, without internal noise."""

    keys = (
        "job_id", "runtime", "state", "outcome", "message", "account", "session_id",
        "cwd", "progress", "cost_usd", "warnings", "override",
    )
    summary = {key: status.get(key) for key in keys if status.get(key) not in (None, [], {})}
    notes = [*(status.get("warnings") or []), *(status.get("limit_notes") or [])]
    if notes:
        summary["warnings"] = notes
    init = status.get("init") or {}
    if init:
        summary["worker"] = {
            "model": init.get("model"),
            "permission_mode": init.get("permission_mode"),
            "mcp_servers": len(init.get("mcp_servers") or []),
            "plugins": len(init.get("plugins") or []),
            "skills": len(init.get("skills") or []),
        }
    attempts = status.get("attempts") or []
    if len(attempts) > 1:
        summary["attempts"] = [
            {key: attempt.get(key) for key in ("account", "outcome", "message")} for attempt in attempts
        ]
    started = status.get("started_at") or status.get("created_at")
    if started:
        end = status.get("finished_at") or time.time()
        summary["elapsed_seconds"] = round(float(end) - float(started), 1)
    job_id = status.get("job_id")
    if job_id:
        new = messages.take_for_host(job_id)
        if new:
            summary["messages"] = [{key: item.get(key) for key in ("id", "kind", "text")} for item in new]
        questions = messages.open_questions(job_id)
        if questions:
            summary["open_questions"] = [{"id": item["id"], "text": item["text"]} for item in questions]
    return summary


def _is_codex(client: str | None) -> bool:
    """Hosts whose MCP calls time out after a few minutes (Codex, Antigravity)."""

    return detect_host(client, {}) in ("codex", "antigravity")


def _wait(arguments: dict[str, Any], client: str | None = None) -> float:
    limit = CODEX_MAX_WAIT_SECONDS if _is_codex(client) else MAX_WAIT_SECONDS
    return float(min(limit, max(0, int(arguments.get("wait_seconds") or 0))))


def call(name: str, arguments: dict[str, Any], progress=None, worker_job: str | None = None, client: str | None = None) -> Any:
    try:
        if worker_job:
            return _call_worker(worker_job, name, arguments, progress)
        return _call(name, arguments, progress, client)
    except ToolError:
        raise
    except (ValueError, RuntimeError, TimeoutError, OSError) as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:  # bad argument types and the like
        raise ToolError(f"{type(exc).__name__}: {exc}") from exc


def _ping(progress, count: int, message: str) -> None:
    if progress:
        try:
            progress(count, message)
        except Exception:
            pass


def progress_message(job_ids: list[str]) -> str:
    """One line per job for a progress ping: state, last tool and the start of its last text."""

    lines = []
    for job_id in job_ids:
        try:
            status = jobs.read_status(job_id)
        except ValueError:
            continue
        progress = status.get("progress") or {}
        parts = [str(status.get("state") or "?")]
        if status.get("account"):
            parts.append(f"on {status['account']}")
        if progress.get("tool_uses"):
            parts.append(f"{progress['tool_uses']} tool calls")
        if progress.get("last_tool"):
            parts.append(f"last tool {progress['last_tool']}")
        text = " ".join(str(progress.get("last_text") or "").split())
        if text:
            parts.append(f'"{text[:120]}{"..." if len(text) > 120 else ""}"')
        lines.append(f"{job_id}: " + ", ".join(parts))
    return "\n".join(lines) or f"waiting for {len(job_ids)} subagent job(s)"


def wait_any(job_ids: list[str], timeout: float, progress=None) -> dict[str, Any]:
    """Block until a job finishes or sends a message; report every job's state."""

    ids = [str(job_id) for job_id in job_ids if job_id]
    if not ids:
        raise ToolError("job_ids is empty")
    for job_id in ids:
        jobs.read_status(job_id)  # fail fast on unknown ids
    deadline = time.monotonic() + max(1.0, float(timeout))
    pings = 0
    last_ping = float("-inf")  # the first ping goes out right away
    while True:
        done = [job_id for job_id in ids if jobs.read_status(job_id).get("state") == "done"]
        talking = [job_id for job_id in ids if messages.has_new_for_host(job_id)]
        if done or talking or time.monotonic() >= deadline:
            break
        if time.monotonic() - last_ping >= WAIT_PING_SECONDS:
            pings += 1
            last_ping = time.monotonic()
            _ping(progress, pings, progress_message(ids))
        time.sleep(1.0)
    report: dict[str, Any] = {"finished": [], "updates": [], "still_running": []}
    for job_id in ids:
        if job_id in done:
            report["finished"].append(_result(job_id, 20000))
        elif job_id in talking:
            report["updates"].append(summarize(jobs.read_status(job_id)))
        else:
            report["still_running"].append(job_id)
    if not done and not talking:
        report["note"] = "Nothing finished before the timeout; call subagent_wait again to keep waiting."
    return report


def _call(name: str, arguments: dict[str, Any], progress=None, client: str | None = None) -> Any:
    if name == "subagent_wait":
        job_ids = arguments.get("job_ids") or ([arguments["job_id"]] if arguments.get("job_id") else [])
        limit = CODEX_MAX_WAIT_SECONDS if _is_codex(client) else MAX_LONG_WAIT_SECONDS
        timeout = min(limit, max(1, int(arguments.get("timeout_seconds") or 1800)))
        return wait_any(list(job_ids), timeout, progress)
    if name == "subagent_message":
        job_id = _job_id(arguments)
        status = jobs.read_status(job_id)
        text = str(arguments.get("text") or "")
        if status.get("state") in jobs.ACTIVE_STATES:
            record = messages.queue_for_worker(job_id, text, arguments.get("reply_to") or None)
            if record is not None:
                return {"job_id": job_id, "delivered": "queued for the running subagent", "message_id": record["id"]}
        spec = jobs.read_spec(job_id)
        follow_up = jobs.create_job(
            runtime=spec.runtime, prompt=text, cwd=spec.cwd, model=spec.model, effort=spec.effort,
            access=spec.access, max_turns=spec.max_turns, timeout_seconds=spec.timeout_seconds,
            add_dirs=spec.add_dirs, resume_job=job_id,
        )
        return {"job_id": job_id, "delivered": "continued the finished conversation as a new job", "new_job": summarize(follow_up)}
    if name == "subagent_start":
        status = jobs.create_job(
            runtime=str(arguments.get("runtime") or "claude"),
            prompt=str(arguments.get("prompt") or ""),
            cwd=str(arguments.get("cwd") or ""),
            account=arguments.get("account"),
            model=arguments.get("model"),
            effort=arguments.get("effort"),
            override=arguments.get("override"),
            access=str(arguments.get("access") or "edit"),
            max_turns=arguments.get("max_turns"),
            timeout_seconds=int(arguments.get("timeout_seconds") or 1800),
            add_dirs=list(arguments.get("add_dirs") or []),
            resume_job=arguments.get("resume_job"),
            host=detect_host(client),
        )
        wait = _wait(arguments, client)
        if wait:
            status = jobs.wait_for(status["job_id"], wait)
            if status.get("state") == "done":
                return _result(status["job_id"], 20000)
        started = summarize(jobs.read_status(status["job_id"]))
        if room := room_note():
            started["room"] = room
        return started
    if name == "subagent_status":
        return summarize(jobs.wait_for(_job_id(arguments), _wait(arguments, client)))
    if name == "subagent_result":
        return _result(_job_id(arguments), int(arguments.get("max_chars") or 20000))
    if name == "subagent_cancel":
        return summarize(jobs.cancel(_job_id(arguments)))
    if name == "subagent_list":
        return {"jobs": jobs.list_jobs(int(arguments.get("limit") or 20))}
    if name == "subagent_accounts":
        selected = arguments.get("runtime")
        reports = {"runtimes": [account_report(runtime) for runtime in RUNTIMES if not selected or runtime == selected]}
        if room := room_note():
            reports["room"] = room
        return reports
    raise ToolError(f"unknown tool: {name}")


def _call_worker(job_id: str, name: str, arguments: dict[str, Any], progress=None) -> Any:
    if name == "message_parent":
        record = messages.post_from_worker(job_id, str(arguments.get("text") or ""))
        return {"sent": True, "id": record["id"]}
    if name == "ask_parent":
        record = messages.post_from_worker(job_id, str(arguments.get("question") or ""), kind="question")
        timeout = min(1800, max(10, int(arguments.get("timeout_seconds") or 600)))
        deadline = time.monotonic() + timeout
        pings = 0
        while time.monotonic() < deadline:
            reply = messages.wait_for_reply(job_id, record["id"], min(PING_SECONDS, deadline - time.monotonic()))
            if reply:
                return {"answer": reply["text"]}
            pings += 1
            _ping(progress, pings, "waiting for the main session to answer")
        return {"answer": None, "note": f"No answer within {timeout} seconds. Continue with your best judgment and say what you assumed."}
    if name == "check_parent_messages":
        new = messages.take_for_worker(job_id)
        return {"messages": [{"kind": item.get("kind"), "text": item.get("text")} for item in new]}
    raise ToolError(f"unknown tool: {name}")


def _job_id(arguments: dict[str, Any]) -> str:
    job_id = str(arguments.get("job_id") or "")
    if not job_id:
        raise ToolError("job_id is required")
    return job_id


def _result(job_id: str, max_chars: int) -> dict[str, Any]:
    data = jobs.read_result(job_id, max(500, min(MAX_RESULT_CHARS, max_chars)))
    summary = summarize(data)
    if data.get("state") != "done":
        summary["note"] = "The job is still running; this is not a final result."
    else:
        jobs.claim_delivery(job_id)  # the model has the result now: the wake-up hook stays silent
    summary["result"] = data.get("result")
    if data.get("result_truncated"):
        summary["result_truncated"] = True
        summary["result_path"] = data.get("result_path")
    return summary


def account_limit_report(config: Any, account: Any) -> dict[str, Any]:
    """An account's effective caps and where each one comes from."""

    data = plans.account_limits(config, account).describe()
    plan, _auto = plans.effective_plan(config, account)
    if account.plan:
        data["plan"] = plan or account.plan
        if not plans.preset_for(config.runtime, plan):
            data["plan_note"] = "no preset for this plan name; only explicit keys apply"
    elif config.runtime == "claude":
        detected = plans.detect_claude_plan(account.home)
        if detected:
            data["suggested_plan"] = f'{detected} (not applied; set plan = "{detected}" or plan = "auto" to use its preset)'
    return data


def runtime_limits(config: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"usage_ceiling": config.usage_ceiling, "when_full": config.when_full}
    if config.max_jobs:
        data["max_jobs"] = config.max_jobs
    if config.max_jobs_by_room:
        cap, left = quota.room_cap(config, time.time())
        data.update(max_jobs_by_room=config.max_jobs_by_room, room_cap=cap, remaining_percent=left)
        data["effective_max_jobs"] = min(cap, config.max_jobs) if config.max_jobs is not None else cap
    return data


def account_report(runtime_name: str) -> dict[str, Any]:
    config = load_config().runtime(runtime_name)
    runtime = get_runtime(runtime_name)
    binary = runtime.find_binary(config)
    report: dict[str, Any] = {
        "runtime": runtime_name,
        "binary": binary[-1] if binary else None,
        "version": runtime.version(config) if binary else None,
        "primary_setup": str(config.primary),
        "failover": config.failover,
        "limits": runtime_limits(config),
        "accounts": [],
    }
    if runtime_name == "codex":
        usage_view.refresh_codex(load_config())
    report["limits"] = runtime_limits(config)
    counts = limits.count_active()
    usage = accounts.load_usage(runtime_name)
    now = time.time()
    pool_state = quota.pool_state(config, counts, now)
    for account in config.listed_accounts():
        record = usage.get(account.name) or {}
        entry: dict[str, Any] = {
            "name": account.name, "home": str(account.home), "enabled": account.enabled,
            "explicit_only": account.explicit_only,
        }
        entry.update(spend.snapshot(config, account, now=now))
        if binary:
            entry["auth"] = runtime.auth_status(config, account)
        until = accounts.cooldown_until(record, now)
        if until:
            entry["cooldown_until"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(until))
        windows = record.get("windows") or {}
        if windows:
            entry["usage"] = {
                name: round(float(window["utilization"]) * 100)
                for name, window in windows.items()
                if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float))
            }
            resets = {
                name: usage_view.reset_text(float(window["resets_at"]), now)
                for name, window in windows.items()
                if isinstance(window, dict) and isinstance(window.get("resets_at"), (int, float))
            }
            if resets:
                entry["usage_resets"] = resets
        entry["busy_slots"] = accounts.busy_slots(config, account)
        entry["running_jobs"] = counts.on_account(runtime_name, account.name)
        entry["limits"] = account_limit_report(config, account)
        if config.pools:
            entry["pools"] = quota.pool_report(config, account.name, pool_state, record, now)
        report["accounts"].append(entry)
    return report


def room_note(runtimes: list[str] | None = None) -> str:
    """The one-line "Room: ..." summary, or "" (it never fails a call)."""

    try:
        config = load_config()
        usage_view.refresh_codex(config)
        return usage_view.room_line(config, runtimes)
    except Exception:
        return ""
