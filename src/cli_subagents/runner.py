"""Background runner: executes one job, including queueing and account failover.

Started as a detached process (`python -m cli_subagents _runner <job_id>`), so a
job keeps running if the MCP server or the host restarts.
"""

from __future__ import annotations

import dataclasses
import json
import os
import queue
import subprocess
import threading
import time
from typing import Any

from cli_subagents import accounts, limits, messages, plans, quota, spend
from cli_subagents.config import OPT_IN_FAILOVER, Account, RuntimeConfig, load_config
from cli_subagents.fsutil import kill_tree, open_private, process_start_time, unlock, worker_process, write_text
from cli_subagents.jobs import job_dir, mark_pending, read_spec, update_status
from cli_subagents.runtimes import get_runtime
from cli_subagents.runtimes.base import (
    AUTH,
    BUDGET,
    CANCELLED,
    ERROR,
    NO_ACCOUNT,
    SUCCESS,
    TIMEOUT,
    USAGE_LIMIT,
    AttemptResult,
    JobSpec,
)

QUEUE_TIMEOUT_SECONDS = 15 * 60
MAX_FOLLOW_UP_TURNS = 5
STATUS_INTERVAL = 2.0
EARLY_DRAIN_SECONDS = 3.0  # most we wait for already-written stdout to be parsed after an early stop
STDERR_WINDOW = 8000  # characters of recent stderr a runtime's check_stderr sees
EARLY_STOP = "early_stop"
FINAL_STATUS_RETRY_SECONDS = 5.0

CONTINUE_NOTE = (
    "Note from CLI Subagents: the account this conversation ran on reached its usage limit, so the "
    "conversation moved to another account. Continue the task from where you stopped. Check the "
    "current state of the files before redoing anything that may already be done."
)

CAPACITY_CONTINUE_NOTE = (
    "Note from CLI Subagents: the model was temporarily at capacity. Continue the task from where "
    "you stopped. Check the current state of the files before redoing anything that may already be done."
)


def handoff_prompt(original: str, previous: AttemptResult, account: str) -> str:
    partial = (previous.text or "").strip()
    note = (
        "Note from CLI Subagents: an earlier attempt at this task ran on another account "
        f"({account}) and stopped because that account reached its usage limit."
    )
    if partial:
        note += f" Its last output was:\n\n<previous_output>\n{partial[-4000:]}\n</previous_output>\n"
    note += (
        "\nCheck the current state of the files before redoing anything that may already be done, "
        "then finish the task."
    )
    return f"{original}\n\n{note}"


class Runner:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.folder = job_dir(job_id)
        self.spec: JobSpec = read_spec(job_id)
        self.config = load_config()
        self.runtime_config: RuntimeConfig = self.config.runtime(self.spec.runtime)
        self.runtime = get_runtime(self.spec.runtime)
        self.pool = quota.pool_for(self.runtime_config, self.spec.model)
        # The whole job, failovers and queueing included, ends by this time.
        self.deadline = self.spec.created + self.spec.timeout_seconds + QUEUE_TIMEOUT_SECONDS
        self.attempts: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self._early: AttemptResult | None = None  # set when check_stderr stops a worker early
        self._spend_error: str | None = None

    # Helpers ---------------------------------------------------------------

    def cancelled(self) -> bool:
        return (self.folder / "cancel").exists()

    def _write_status(self, **fields: Any) -> None:
        update_status(self.job_id, runner_pid=os.getpid(), attempts=self.attempts, warnings=self.warnings, **fields)

    def status(self, **fields: Any) -> bool:
        """Best-effort heartbeat or progress write: a locked or busy status file must not end the job."""

        try:
            self._write_status(**fields)
            return True
        except OSError:
            return False

    def finish(self, outcome: str, message: str = "", result: AttemptResult | None = None) -> int:
        messages.close_or_continue(
            self.job_id, result.session_id if result else None,
            self.attempts[-1]["account"] if self.attempts else self.spec.resume_account,
        )
        text = result.text if result else ""
        if text:
            write_text(self.folder / "result.md", text)
        total_cost = sum(float(item.get("cost_usd") or 0) for item in self.attempts) or None
        fields = dict(
            state="done",
            outcome=outcome,
            message=message,
            finished_at=time.time(),
            session_id=result.session_id if result else None,
            cost_usd=total_cost,
            child_pid=None,
            child_started=None,
        )
        try:
            mark_pending(self.job_id)  # before the final status: whoever sees "done" also sees the marker
        except OSError as exc:
            self.warnings.append(f"could not mark the result for delivery: {exc}")
        # The final write is the one that matters, so it keeps trying for a while.
        deadline = time.monotonic() + FINAL_STATUS_RETRY_SECONDS
        while True:
            try:
                self._write_status(**fields)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        return 0 if outcome == SUCCESS else 1

    # Main loop -------------------------------------------------------------

    def run(self) -> int:
        self.status(state="starting", started_at=time.time())
        prompt = self.spec.prompt
        excluded: set[str] = set()
        last: AttemptResult | None = None
        failover = self.runtime_config.failover == "on-limit" and not self.spec.account
        # The conversation to continue and the account folder that holds it.
        session = self.spec.resume_session
        owner = self.spec.resume_account
        follow_ups = 0

        while True:
            if self.cancelled():
                return self.finish(CANCELLED, "cancelled", last)
            pinned = owner if session and not hasattr(self.runtime, "transfer_session") else self.spec.account
            lease = self._wait_for_account(excluded, prefer=owner, pinned=pinned)
            if lease is None:
                if self.cancelled():
                    return self.finish(CANCELLED, "cancelled", last)
                if self._spend_error:
                    return self.finish(BUDGET, self._spend_error, last)
                if last is not None:
                    return self.finish(last.outcome, last.message, last)
                return self.finish(NO_ACCOUNT, self._no_account_message(excluded))
            try:
                fallback = handoff_prompt(self.spec.prompt, last, owner or "another account") if last else self.spec.prompt
                last = self._attempt_with_capacity_retries(lease, prompt, session, owner, fallback)
            finally:
                lease.release()

            if last.outcome == SUCCESS:
                pending = messages.close_or_continue(
                    self.job_id, last.session_id, lease.account.name,
                    can_continue=bool(last.session_id) and follow_ups < MAX_FOLLOW_UP_TURNS,
                )
                if pending:
                    # Follow-ups sent while the worker was busy: continue the same conversation.
                    follow_ups += 1
                    session, owner = last.session_id, lease.account.name
                    prompt = (
                        "Follow-up from the main session (you may already have seen it during your run; "
                        "if you already handled it, just confirm briefly):\n\n"
                        + "\n\n".join(item["text"] for item in pending)
                    )
                    self.status(follow_up_turns=follow_ups)
                    continue
            if last.outcome == AUTH and failover and last.before_start:
                # The account has no login yet and nothing ran: try the next account.
                excluded.add(lease.account.name)
                self.warnings.append(f"{lease.account.name}: {last.message}")
                continue
            if last.outcome != USAGE_LIMIT:
                return self.finish(last.outcome, last.message, last)
            # A model pool's cooldown is scoped to its models; an account-wide one is skipped when
            # the limit covers only some models (OpenCode's free tier), so paid models keep working.
            if self.pool or (last.account_cooldown and (
                    self.spec.runtime not in OPT_IN_FAILOVER or self.runtime_config.failover == "on-limit")):
                # Unpooled opt-in runtimes keep their old behaviour; pool cooldowns always apply.
                minutes = (self.pool.cooldown_minutes if self.pool else None) or self.runtime_config.cooldown_minutes
                until = accounts.cooldown_end(
                    "" if self.pool and last.resets_at else "\n".join(part for part in (last.message, last.error_text) if part),
                    last.resets_at,
                    minutes,
                )
                if self.pool:
                    accounts.set_pool_cooldown(self.spec.runtime, lease.account.name, self.pool.name, until, last.message or USAGE_LIMIT)
                else:
                    accounts.set_cooldown(self.spec.runtime, lease.account.name, until, last.message or USAGE_LIMIT)
            if not failover or last.no_rotation:
                return self.finish(USAGE_LIMIT, f"{lease.account.name}: {last.message}", last)
            excluded.add(lease.account.name)
            if last.session_id and hasattr(self.runtime, "transfer_session"):
                # Continue the same conversation on the next account.
                session, owner, prompt = last.session_id, lease.account.name, CONTINUE_NOTE
            else:
                session, owner = None, None
                prompt = handoff_prompt(self.spec.prompt, last, lease.account.name)
            self.warnings.append(f"{lease.account.name} hit its usage limit; continuing on another account")

    def _attempt_with_capacity_retries(
        self, lease: accounts.Lease, prompt: str, session: str | None,
        owner: str | None, fallback: str,
    ) -> AttemptResult:
        # Keep the account and execution slots throughout the wait. Reacquiring an account
        # could select a different login or count a retry as another pool job.
        active_deadline = min(self.deadline, time.time() + self.spec.timeout_seconds)
        retries = 0
        while True:
            result = self._attempt(lease, prompt, session, owner, fallback)
            if result.outcome != ERROR or not result.at_capacity:
                return result
            if retries >= self.runtime_config.capacity_retries:
                return dataclasses.replace(result, message=f"model at capacity after {retries} retries: {result.message}")
            # Attempts and their waits share the task's timeout, including the time
            # already spent inside the CLI's own retry loop.
            self.deadline = min(self.deadline, active_deadline)
            retries += 1
            seconds = self.runtime_config.capacity_retry_seconds * 2 ** (retries - 1)
            retry = {"number": retries, "total": self.runtime_config.capacity_retries,
                     "seconds": seconds, "retry_at": time.time() + seconds}
            message = f"model at capacity, retrying in {seconds} s ({retries} of {retry['total']})"
            with open_private(self.folder / "events.jsonl") as events:
                retry["event_offset"] = events.tell()
                self.attempts[-1]["capacity_retry"] = retry
                # Publish metadata before the event. follow authenticates its control line
                # against this offset, rather than trusting worker-supplied event text.
                self.status(state="running", message=message, session_id=result.session_id)
                events.write((json.dumps({"type": "csa.capacity_retry", **retry, "message": message}) + "\n").encode())
            stopped = self._wait_for_capacity(retry["retry_at"])
            if stopped:
                return dataclasses.replace(result, outcome=stopped, at_capacity=False,
                                           message="cancelled" if stopped == CANCELLED else "timed out during capacity retry wait")
            session = result.session_id or session
            if session:
                owner, prompt = lease.account.name, CAPACITY_CONTINUE_NOTE

    def _wait_for_capacity(self, retry_at: float) -> str | None:
        last_status = time.monotonic()
        while True:
            if self.cancelled():
                return CANCELLED
            now = time.time()
            if now >= self.deadline:
                return TIMEOUT
            if now >= retry_at:
                return None
            if time.monotonic() - last_status >= STATUS_INTERVAL:
                self.status()
                last_status = time.monotonic()
            time.sleep(min(0.2, retry_at - now, self.deadline - now))

    def _no_account_message(self, excluded: set[str]) -> str:
        usage = accounts.load_usage(self.spec.runtime)
        ends = [
            max((end for end in (
                accounts.cooldown_until(usage.get(account.name)),
                accounts.pool_cooldown_until(usage.get(account.name), self.pool.name) if self.pool else None,
            ) if end is not None), default=None)
            for account in self.runtime_config.worker_accounts()
            if account.name not in excluded
        ]
        ends = [end for end in ends if end]
        if ends:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(min(ends)))
            return f"every {self.spec.runtime} account is at its usage limit; the earliest resets around {when}"
        if time.time() >= self.deadline:
            return "the job ran out of time before an account was free"
        return f"no {self.spec.runtime} account was free within {QUEUE_TIMEOUT_SECONDS // 60} minutes"

    def _wait_for_account(self, excluded: set[str], prefer: str | None = None,
                          pinned: str | None = None) -> accounts.Lease | None:
        queued_since = time.time()
        while True:
            slots = limits.acquire_execution_slots(self.config, self.spec.runtime, self.spec.model, override=self.spec.override)
            try:
                if slots is None:
                    lease, all_busy = None, True
                else:
                    lease, _earliest, all_busy = accounts.acquire(
                        self.runtime_config, pinned=pinned or self.spec.account, exclude=excluded,
                        prefer=prefer, model=self.spec.model, job_id=self.job_id, override=self.spec.override,
                    )
                    if lease is not None:
                        lease.pool_slots.extend(slots)
                        slots = []
            except spend.SpendLimitError as exc:
                self._spend_error = str(exc)
                return None
            except ValueError as exc:
                self.warnings.append(str(exc))
                return None
            finally:
                for handle, _path in slots or []:
                    unlock(handle)
            if lease is not None:
                if time.time() >= self.deadline:
                    lease.release()
                    return None
                return lease
            if not all_busy:
                return None
            now = time.time()
            if self.cancelled() or now - queued_since > QUEUE_TIMEOUT_SECONDS or now >= self.deadline:
                return None
            self.status(state="queued", message="waiting for a free account slot")
            time.sleep(3)

    def _attempt(
        self,
        lease: accounts.Lease,
        prompt: str,
        session: str | None = None,
        owner: str | None = None,
        fallback_prompt: str | None = None,
    ) -> AttemptResult:
        index = len(self.attempts)
        try:
            budget = spend.reserve(self.runtime_config, lease.account, self.job_id, index)
        except spend.SpendLimitError as exc:
            return AttemptResult(BUDGET, message=str(exc), before_start=True)
        try:
            result = self._execute_attempt(lease, prompt, session, owner, fallback_prompt, budget)
        except Exception:
            attempt = self.attempts[index] if len(self.attempts) > index else {}
            spend.settle(self.runtime_config, lease.account, self.job_id, index,
                         AttemptResult(ERROR, before_start=bool(attempt.get("before_start", True))))
            raise
        spend.settle(self.runtime_config, lease.account, self.job_id, index, result)
        return result

    def _execute_attempt(
        self,
        lease: accounts.Lease,
        prompt: str,
        session: str | None = None,
        owner: str | None = None,
        fallback_prompt: str | None = None,
        budget: float | None = None,
    ) -> AttemptResult:
        account = lease.account
        attempt: dict[str, Any] = {"account": account.name, "attempted_at": time.time(), "before_start": True}
        if budget is not None:
            attempt["budget_usd"] = budget
        if self.pool:
            attempt["pool"] = self.pool.name
        self.attempts.append(attempt)
        if self.pool:
            self.status(account=account.name, assigned_account=account.name, pool=self.pool.name)
        if session and owner and owner != account.name:
            source = self.runtime_config.find(owner)
            transfer = getattr(self.runtime, "transfer_session", None)
            moved = bool(source) and transfer is not None and transfer(source, account, session, self.spec.cwd)
            if moved:
                attempt["continued_from"] = owner
            else:
                self.warnings.append(f"could not move the session from {owner}; starting a new one with a summary")
                session, prompt = None, fallback_prompt or prompt
        spec = dataclasses.replace(self.spec, resume_session=session, max_budget_usd=budget)
        limits = plans.account_limits(self.runtime_config, account)
        capped = plans.cap_effort(spec.effort, limits)
        if capped != spec.effort:
            # The account's effort cap holds on failover too, not only at admission.
            note = f"effort lowered from {spec.effort} to {capped} ({account.name}: {limits.max_effort_source})"
            if note not in self.warnings:
                self.warnings.append(note)
            spec = dataclasses.replace(spec, effort=capped)
        preflight = getattr(self.runtime, "preflight", None)
        blocked = preflight(self.runtime_config, account) if preflight else None
        if blocked is not None:
            # A problem the runtime can see before starting (e.g. no login yet): end at once.
            blocked.before_start = True
            attempt.update(ended_at=time.time(), outcome=blocked.outcome, message=blocked.message)
            return blocked
        try:
            setup = self.runtime.prepare(self.runtime_config, account, spec)
        except Exception as exc:  # setup problems must not kill the job silently
            setup = {}
            self.warnings.append(f"shared setup for {account.name} failed: {type(exc).__name__}: {exc}")
        for key in ("conflicts", "warnings"):
            for message in setup.get(key) or []:
                if message not in self.warnings:
                    self.warnings.append(message)
        try:
            launch = self.runtime.launch(self.runtime_config, account, spec, prompt, setup)
        except Exception as exc:
            result = AttemptResult(ERROR, message=f"could not start the worker: {exc}", session_id=session, before_start=True)
            attempt.update(ended_at=time.time(), outcome=result.outcome, message=result.message)
            return result
        attempt["session_id"] = launch.session_id
        for message in launch.warnings:
            if message not in self.warnings:
                self.warnings.append(message)
        parser = self.runtime.new_parser()
        time_limit = max(1.0, min(float(self.spec.timeout_seconds), self.deadline - time.time()))

        events_path = self.folder / "events.jsonl"
        stderr_path = self.folder / f"stderr-{len(self.attempts)}.log"
        stop_reason: str | None = None
        exit_code: int | None = None
        with open_private(events_path) as events, open_private(stderr_path, append=False) as stderr:
            try:
                with worker_process(
                    launch.argv, cwd=self.spec.cwd, env=launch.env, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=stderr,
                ) as process:
                    attempt.update(started_at=time.time(), before_start=False)
                    started = process_start_time(process.pid)
                    spend.started(self.runtime_config, self.job_id, len(self.attempts) - 1, process.pid, started)
                    try:
                        lease.set_owner(process.pid, started, self.job_id)
                    except OSError as exc:
                        self.warnings.append(f"could not record the worker in its account slot: {exc}")
                    self.status(
                        state="running", account=account.name, child_pid=process.pid, child_started=started,
                        session_id=launch.session_id, message=None,
                    )
                    stop_reason = self._pump(process, launch.stdin, parser, account, events, time_limit, stderr_path)
                    try:
                        exit_code = process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        kill_tree(process.pid)
                        exit_code = None
            except OSError as exc:
                if not attempt["before_start"]:
                    raise
                result = AttemptResult(ERROR, message=f"could not start the worker: {exc}", session_id=session,
                                       before_start=True)
                attempt.update(ended_at=time.time(), outcome=result.outcome, message=result.message)
                return result
            finally:
                self.status(child_pid=None, child_started=None)

        stderr_tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:] if stderr_path.exists() else ""
        if stop_reason == CANCELLED:
            result = AttemptResult(CANCELLED, message="cancelled", text=parser.last_text, session_id=parser.session_id)
        elif stop_reason == EARLY_STOP and self._early is not None:
            result = dataclasses.replace(
                self._early, text=self._early.text or parser.last_text, session_id=self._early.session_id or parser.session_id
            )
            self._early = None
        elif stop_reason == TIMEOUT:
            result = AttemptResult(
                TIMEOUT,
                message=f"stopped after {int(time_limit)} seconds",
                text=parser.last_text,
                session_id=parser.session_id,
            )
        else:
            try:
                result = parser.finish(exit_code, stderr_tail)
            except Exception as exc:
                result = AttemptResult(ERROR, message=f"could not read the result: {exc}", text=parser.last_text)
        if result.session_id is None:
            result.session_id = launch.session_id
        if stop_reason:
            # A result may have arrived before a hung CLI was cancelled or timed
            # out. Preserve its reported cost while keeping the stop outcome.
            try:
                reported = parser.finish(exit_code, stderr_tail)
                result.cost_usd, result.usage = reported.cost_usd, reported.usage
            except Exception:
                pass
        attempt.update(
            ended_at=time.time(),
            outcome=result.outcome,
            message=result.message,
            exit_code=exit_code,
            session_id=result.session_id,
            cost_usd=result.cost_usd,
            num_turns=result.num_turns,
        )
        self.status(progress=parser.progress(), init=getattr(parser, "init", None))
        return result

    def _pump(
        self, process: subprocess.Popen, stdin_text: str, parser: Any, account: Account, events, time_limit: float,
        stderr_path: Any = None,
    ) -> str | None:
        """Feed the prompt, stream events until the worker exits; return a stop reason or None."""

        assert process.stdin is not None and process.stdout is not None
        stdin = process.stdin

        def writer() -> None:
            # A separate thread: a worker that doesn't read its stdin must not block the
            # timeout and cancel checks below.
            try:
                stdin.write(stdin_text.encode("utf-8"))
            except (OSError, ValueError):
                pass
            finally:
                try:
                    stdin.close()
                except (OSError, ValueError):
                    pass

        threading.Thread(target=writer, daemon=True).start()
        lines: queue.Queue[bytes | None] = queue.Queue()

        def reader() -> None:
            try:
                for raw in process.stdout:  # type: ignore[union-attr]
                    lines.put(raw)
            except (OSError, ValueError):
                pass
            lines.put(None)

        threading.Thread(target=reader, daemon=True).start()
        started = time.monotonic()
        last_status = 0.0
        check_stderr = getattr(self.runtime, "check_stderr", None) if stderr_path is not None else None
        stderr_offset = 0
        stderr_recent = ""
        while True:
            try:
                raw = lines.get(timeout=0.5)
            except queue.Empty:
                raw = b""
            if check_stderr is not None:
                # Some CLIs print a problem on stderr and then wait (agy asks for a login code
                # until its timeout). The runtime decides from the new stderr text whether to stop.
                stderr_offset, fresh = _read_from(stderr_path, stderr_offset)
                if fresh:
                    stderr_recent = (stderr_recent + fresh)[-STDERR_WINDOW:]
                    try:
                        early = check_stderr(stderr_recent, account)
                    except Exception:
                        early = None
                    if early is not None:
                        self._early = early
                        kill_tree(process.pid)
                        # Output the worker already wrote must still reach the parser.
                        pending = [raw] if raw else []
                        self._drain(lines, pending, parser, account, events)
                        return EARLY_STOP
            if raw is None:
                return None
            if raw:
                events.write(raw if raw.endswith(b"\n") else raw + b"\n")
                events.flush()
                self._handle_line(raw, parser, account)
            elif process.poll() is not None:
                # A descendant can inherit stdout after the CLI exits. Do not wait
                # for it to close the pipe before the process-group cleanup runs.
                return None
            if time.monotonic() - last_status > STATUS_INTERVAL:
                last_status = time.monotonic()
                self.status(state="running", account=account.name, progress=parser.progress(), init=getattr(parser, "init", None))
            if self.cancelled():
                kill_tree(process.pid)
                return CANCELLED
            if time.monotonic() - started > time_limit:
                kill_tree(process.pid)
                return TIMEOUT

    def _drain(self, lines: "queue.Queue[bytes | None]", pending: list[bytes], parser: Any, account: Account, events) -> None:
        """Parse `pending` and what is queued, until the pipe closes or EARLY_DRAIN_SECONDS pass."""

        deadline = time.monotonic() + EARLY_DRAIN_SECONDS
        while True:
            for raw in pending:
                events.write(raw if raw.endswith(b"\n") else raw + b"\n")
                events.flush()
                self._handle_line(raw, parser, account)
            pending = []
            try:
                raw = lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                return
            if raw is None:
                return
            if raw:
                pending = [raw]

    def _handle_line(self, raw: bytes, parser: Any, account: Account) -> None:
        try:
            event = json.loads(raw.decode("utf-8", errors="replace"))
            if not isinstance(event, dict):
                return
            usage = parser.feed(event)
            if usage:
                accounts.record_rate_limit_event(self.spec.runtime, account.name, usage)
        except Exception:
            # One malformed event must not end the job or orphan the worker.
            return


def _read_from(path: Any, offset: int) -> tuple[int, str]:
    """New text in a file from `offset` on, and the new offset."""

    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            data = handle.read(1_000_000)
    except OSError:
        return offset, ""
    return offset + len(data), data.decode("utf-8", errors="replace")


def main(job_id: str) -> int:
    try:
        runner = Runner(job_id)
    except Exception as exc:
        mark_pending(job_id)
        update_status(job_id, state="done", outcome=ERROR, message=f"runner could not start: {exc}", finished_at=time.time())
        return 1
    try:
        return runner.run()
    except Exception as exc:
        runner.warnings.append(f"{type(exc).__name__}: {exc}")
        return runner.finish(ERROR, f"runner failed: {type(exc).__name__}: {exc}")
