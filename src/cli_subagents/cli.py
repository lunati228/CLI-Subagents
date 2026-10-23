"""`csa`: command-line front end for CLI Subagents."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

from cli_subagents import __version__, accounts, jobs, rules, setup, spend, sync, tools
from cli_subagents.config import NAME_RE, Account, homes_dir, load_config, save_config
from cli_subagents.fsutil import make_private_dir
from cli_subagents.runtimes import AVAILABLE as RUNTIMES, get_runtime
from cli_subagents.runtimes.base import ACCESS_LEVELS, JobSpec


def _print(value: Any) -> None:
    if isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _job_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("prompt", help="The task. Use '-' to read it from stdin.")
    parser.add_argument("--runtime", default="claude", choices=RUNTIMES)
    parser.add_argument("--account")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument(
        "--override", metavar="USER_WORDS",
        help="The user's own words authorizing more parallel jobs. Bypass csa's caps and queueing only; "
        "never set this on your own judgment. Use only when the user asked for more jobs in this conversation "
        "or confirmed after a warning explaining how much of the 5-hour window is left.",
    )
    parser.add_argument("--access", default="edit", choices=ACCESS_LEVELS)
    parser.add_argument("--max-turns", type=int)
    parser.add_argument("--timeout", type=int, default=1800, help="Seconds (default 1800).")
    parser.add_argument("--add-dir", action="append", default=[])
    parser.add_argument("--resume-job")


def _start(args: argparse.Namespace) -> dict[str, Any]:
    prompt = sys.stdin.read() if args.prompt == "-" else args.prompt
    return jobs.create_job(
        runtime=args.runtime,
        prompt=prompt,
        cwd=str(Path(args.cwd).resolve()),
        account=args.account,
        model=args.model,
        effort=args.effort,
        override=args.override,
        access=args.access,
        max_turns=args.max_turns,
        timeout_seconds=args.timeout,
        add_dirs=[str(Path(item).resolve()) for item in args.add_dir],
        resume_job=args.resume_job,
    )


# Accounts --------------------------------------------------------------------


def cmd_accounts(args: argparse.Namespace) -> int:
    config = load_config()
    runtime_config = config.runtime(args.runtime)
    runtime = get_runtime(args.runtime)
    action = args.action or "list"

    if action == "list":
        _print(tools.account_report(args.runtime))
        return 0

    if action == "reset":
        # The primary account can cool down too, so 'primary' is a valid name here.
        if not args.name or runtime_config.find(args.name) is None:
            print(f"No {args.runtime} account named '{args.name or ''}'. See: csa accounts list")
            return 1
        cleared = accounts.clear_cooldown(args.runtime, args.name)
        print(f"{args.runtime} account '{args.name}': " + ("cooldown cleared" if cleared else "had no cooldown"))
        return 0

    if not args.name or not NAME_RE.match(args.name) or args.name == "primary":
        print("Give the account a short name: letters, digits, dot, dash or underscore ('primary' is reserved).")
        return 2

    if action == "add":
        if runtime_config.find(args.name):
            print(f"{args.runtime} account '{args.name}' already exists.")
            return 1
        home = Path(args.home).expanduser().resolve() if args.home else homes_dir() / args.runtime / args.name
        try:
            status, account = setup.register_account(config, args.runtime, args.name, home)
        except ValueError as exc:
            print(exc)
            return 1
        if status == "exists":
            print(f"That folder already belongs to {args.runtime} account '{account.name}'.")
            return 1
        make_private_dir(home)
        save_config(config)
        print(f"Added {args.runtime} account '{args.name}' with folder {home}")
        if getattr(runtime, "add_note", None):
            print(runtime.add_note)
        report = runtime.prepare(runtime_config, account, _probe_spec(args.runtime))
        _print_setup(report)
        if args.no_login:
            print(f"Log in later with: csa accounts login {args.name} --runtime {args.runtime}")
            return 0
        return _login(runtime_config, runtime, account)

    account = runtime_config.find(args.name)
    if account is None:
        print(f"No {args.runtime} account named '{args.name}'. See: csa accounts list")
        return 1

    if action == "login":
        return _login(runtime_config, runtime, account)
    if action == "sync":
        _print_setup(runtime.prepare(runtime_config, account, _probe_spec(args.runtime, args.cwd)))
        return 0
    if action == "remove":
        runtime_config.accounts = [item for item in runtime_config.accounts if item.name != args.name]
        save_config(config)
        print(f"Removed '{args.name}' from the config. Its folder was left as is: {account.home}")
        return 0
    if action in ("enable", "disable"):
        account.enabled = action == "enable"
        save_config(config)
        print(f"{args.name}: {'enabled' if account.enabled else 'disabled'}")
        return 0
    print(f"Unknown action: {action}")
    return 2


def _probe_spec(runtime: str, cwd: str | None = None) -> JobSpec:
    return JobSpec(job_id="probe", runtime=runtime, prompt="", cwd=str(Path(cwd or os.getcwd()).resolve()))


def _print_setup(report: dict[str, Any]) -> None:
    for key, label in (("linked", "Linked"), ("copied", "Copied"), ("removed", "Removed")):
        if report.get(key):
            print(f"{label}: {', '.join(report[key])}")
    if report.get("mcp_servers"):
        print(f"Shared MCP servers: {', '.join(report['mcp_servers'])}")
    if report.get("plugin_dirs"):
        print(f"Shared plugins: {', '.join(Path(item).parts[-2] for item in report['plugin_dirs'])}")
    for key in ("conflicts", "warnings"):
        for message in report.get(key) or []:
            print(f"Note: {message}")


def _login(runtime_config, runtime, account: Account) -> int:
    argv, env = runtime.login_command(runtime_config, account)
    print(f"Opening the {runtime.name} login for '{account.name}'. Sign in with the account this profile should use.")
    if hasattr(runtime, "login_instructions"):
        print(runtime.login_instructions(account))
    cwd = runtime.login_cwd(account) if hasattr(runtime, "login_cwd") else None
    return subprocess.call(argv, env=env, cwd=cwd)


# Setup -----------------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> int:
    code, report = setup.run_setup(
        load_config(), args.host or ["auto"], args.account, dry_run=args.dry_run, claude_route=args.claude_route,
    )
    print()
    print(setup.format_report(report))
    return code


def cmd_update(args: argparse.Namespace) -> int:
    code, report = setup.run_update(load_config(), args.host or ["auto"], dry_run=args.dry_run, package=not args.no_package)
    print()
    print(setup.format_report(report, "update"))
    return code


# Doctor ----------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config()
    runtime_config = config.runtime(args.runtime)
    runtime = get_runtime(args.runtime)
    report = tools.account_report(args.runtime)
    _print(report)
    problems = 0
    for account in runtime_config.pool_accounts():
        copies = runtime.fallback_copies(account) if hasattr(runtime, "fallback_copies") else sync.fallback_copies(account.home)
        if copies and not account.is_primary:
            print(f"Note: account '{account.name}' has copies instead of links (they don't follow later edits "
                  f"until the next sync): {', '.join(copies)}")
    if args.runtime == "codex" and os.name == "nt":
        from cli_subagents.runtimes.codex import windows_sandbox_summary
        print(windows_sandbox_summary(runtime_config))
    print(f"\nEffective limits ({args.runtime}): when_full={runtime_config.when_full}, usage_ceiling={runtime_config.usage_ceiling}"
          + (f", runtime max_jobs={runtime_config.max_jobs}" if runtime_config.max_jobs else "")
          + (f", global max_jobs={config.max_jobs}" if config.max_jobs else ""))
    if "room_cap" in report.get("limits", {}):
        shown = report["limits"]
        left = shown["remaining_percent"]
        detail = f"{left:g}% left" if left is not None else "no usage data"
        print(f"  max_jobs_by_room: {shown['room_cap']} jobs at a time, {detail}; effective runtime cap {shown['effective_max_jobs']}")
    for entry in report["accounts"]:
        shown = entry.get("limits") or {}
        text = f"  {entry['name']}: up to {shown.get('max_jobs')} jobs ({shown.get('max_jobs_from')})"
        if shown.get("max_effort"):
            text += f", effort up to {shown['max_effort']} ({shown.get('max_effort_from')})"
        if shown.get("models"):
            text += f", models {', '.join(shown['models'])} ({shown.get('models_from')})"
        if shown.get("suggested_plan"):
            text += f"; suggested plan {shown['suggested_plan']}"
        if entry.get("explicit_only"):
            text += "; explicit-only"
        if not entry.get("enabled", True):
            text += "; disabled"
        if entry.get("spend_cap_usd") is not None:
            text += "; " + spend.describe(entry)
        print(text)
        for pool in entry.get("pools", []):
            print(f"    {pool['state']}")
    if not report["binary"]:
        print(f"\nProblem: no {args.runtime} binary found. Install it or set [{args.runtime}] binary in the config.")
        return 1
    for entry in report["accounts"]:
        auth = entry.get("auth") or {}
        if not auth.get("loggedIn") and entry.get("enabled", True):
            problems += 1
            flag = "" if args.runtime == "claude" else f" --runtime {args.runtime}"
            how = f"run: csa accounts login {entry['name']}{flag}"
            if args.runtime == "antigravity" and entry["name"] == "primary":
                how = "run agy in a terminal and sign in"
            print(f"Problem: account '{entry['name']}' is not logged in. To fix it, {how}")
    if not args.live:
        print("\nAdd --live to start one tiny job per account and compare what each one loads.")
        return 1 if problems else 0

    cwd = str(Path(args.cwd).resolve())
    # Claude's cheapest model is a known alias; other runtimes use their default unless asked.
    model = args.model or ("haiku" if args.runtime == "claude" else None)
    started = []
    for account in runtime_config.pool_accounts():
        status = jobs.create_job(
            runtime=args.runtime,
            prompt="Reply with exactly: OK",
            cwd=cwd,
            account=account.name,
            model=model,
            access="read",
            max_turns=1,
            timeout_seconds=180,
        )
        started.append((account.name, status["job_id"]))
    loaded: dict[str, dict[str, Any]] = {}
    for name, job_id in started:
        status = jobs.wait_for(job_id, 200)
        init = status.get("init") or {}
        loaded[name] = init
        print(f"\n[{name}] {status.get('outcome')}: {status.get('message') or ''}")
        if init:
            local = [server["name"] for server in init.get("mcp_servers", []) if server.get("source") != "claudeai"]
            connectors = [server["name"] for server in init.get("mcp_servers", []) if server.get("source") == "claudeai"]
            print(f"  model {init.get('model')}, permission mode {init.get('permission_mode')}")
            print(f"  MCP servers ({len(local)}): {', '.join(sorted(local)) or '-'}")
            print(f"  claude.ai connectors (account-specific): {len(connectors)}")
            print(f"  plugins: {', '.join(sorted(init.get('plugins') or [])) or '-'}")
            print(f"  skills: {len(init.get('skills') or [])}, agents: {len(init.get('agents') or [])}")
        if status.get("outcome") != "success":
            problems += 1
    names = list(loaded)
    if len(names) > 1:
        base = loaded[names[0]]
        for name in names[1:]:
            for key in ("plugins", "skills", "agents"):
                missing = sorted(set(base.get(key) or []) - set(loaded[name].get(key) or []))
                extra = sorted(set(loaded[name].get(key) or []) - set(base.get(key) or []))
                if missing or extra:
                    print(f"Difference in {key} between {names[0]} and {name}: missing {missing}, extra {extra}")
            base_mcp = {s["name"] for s in base.get("mcp_servers", []) if s.get("source") != "claudeai"}
            other_mcp = {s["name"] for s in loaded[name].get("mcp_servers", []) if s.get("source") != "claudeai"}
            if base_mcp != other_mcp:
                print(f"Difference in MCP servers between {names[0]} and {name}: {sorted(base_mcp ^ other_mcp)}")
    return 1 if problems else 0


# Jobs ------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    status = _start(args)
    if args.follow:
        from cli_subagents.follow import follow

        return follow(status["job_id"], None, args.max_wait, window=args.window)
    print(f"Started {status['job_id']}", file=sys.stderr)
    status = jobs.wait_for(status["job_id"], args.timeout + 960)
    result = jobs.read_result(status["job_id"], 1_000_000)
    if result.get("state") == "done":
        jobs.claim_delivery(status["job_id"])
    if result.get("result"):
        print(result["result"])
    print(json.dumps(tools.summarize(result), indent=2, default=str), file=sys.stderr)
    return 0 if result.get("outcome") == "success" else 1


def cmd_follow(args: argparse.Namespace) -> int:
    from cli_subagents.follow import follow

    if args.from_offset is not None and args.from_offset < 0:
        raise ValueError("--from must be 0 or more")
    return follow(args.job_id, args.from_offset, args.max_wait, window=args.window)


def cmd_message(args: argparse.Namespace) -> int:
    if args.from_offset is not None:
        if not args.follow:
            raise ValueError("--from requires --follow")
        if args.from_offset < 0:
            raise ValueError("--from must be 0 or more")
    text = sys.stdin.read() if args.text == "-" else args.text
    result = tools.call("subagent_message", {"job_id": args.job_id, "text": text, "reply_to": args.reply_to})
    if args.follow:
        from cli_subagents.follow import follow

        new_job = result.get("new_job")
        job_id = new_job["job_id"] if new_job else args.job_id
        # A continuation has its own event log; an old job's offset cannot apply.
        start = None if new_job else args.from_offset
        return follow(job_id, start, args.max_wait, window=args.window)
    _print(result)
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    _print(tools.summarize(_start(args)))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _print(tools.summarize(jobs.wait_for(args.job_id, args.wait)))
    return 0


def cmd_result(args: argparse.Namespace) -> int:
    _print(tools.call("subagent_result", {"job_id": args.job_id, "max_chars": args.max_chars}))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    _print(tools.summarize(jobs.cancel(args.job_id)))
    return 0


def cmd_jobs(args: argparse.Namespace) -> int:
    for job in jobs.list_jobs(args.limit):
        created = time.strftime("%m-%d %H:%M", time.localtime(job.get("created_at") or 0))
        override = f"  override={json.dumps(job['override'], ensure_ascii=False)}" if job.get("override") else ""
        print(f"{job['job_id']}  {created}  {job.get('runtime')}  {job.get('state')}/{job.get('outcome') or '-'}  {job.get('account') or '-'}{override}")
    return 0


# Interactive launcher --------------------------------------------------------


def cmd_launch(args: argparse.Namespace, extra: list[str]) -> int:
    """Open an interactive CLI on the account with the most room, with the shared setup."""

    config = load_config()
    runtime_config = config.runtime(args.runtime)
    runtime = get_runtime(args.runtime)
    if args.account:
        account = runtime_config.find(args.account)
        if account is None:
            print(f"No {args.runtime} account named '{args.account}'.")
            return 1
    else:
        ranked, _ = accounts.ranked_accounts(runtime_config)
        if not ranked:
            print(f"Every {args.runtime} account is cooling down after a usage limit.")
            return 1
        account = ranked[0]
    setup = runtime.prepare(runtime_config, account, _probe_spec(args.runtime))
    argv, env = runtime.interactive(runtime_config, account, setup)
    print(f"Starting {args.runtime} on account '{account.name}'", file=sys.stderr)
    return subprocess.call([*argv, *extra], env=env)


# Entry point -----------------------------------------------------------------


def _positive_seconds(value: str) -> int:
    try:
        seconds = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--window must be a positive integer") from None
    if seconds < 1:
        raise argparse.ArgumentTypeError("--window must be a positive integer")
    return seconds


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="csa", description="CLI Subagents: run tasks in other agent CLIs under your own accounts.")
    parser.add_argument("--version", action="version", version=f"cli-subagents {__version__}")
    parser.add_argument("--home", dest="csa_home", help="Use this folder instead of ~/.cli-subagents (sets CLI_SUBAGENTS_HOME).")
    sub = parser.add_subparsers(dest="command", required=True)
    rules.add_parser(sub)

    mcp_parser = sub.add_parser("mcp", help="Run the MCP server on stdio.")
    mcp_parser.add_argument("--worker", help=argparse.SUPPRESS)

    accounts_parser = sub.add_parser("accounts", help="List, add, log in, sync, reset or remove accounts.")
    accounts_parser.add_argument(
        "action", nargs="?", choices=("list", "add", "login", "sync", "remove", "enable", "disable", "reset"),
        help="reset clears an account's usage-limit cooldown.",
    )
    accounts_parser.add_argument("name", nargs="?")
    accounts_parser.add_argument("--runtime", default="claude", choices=RUNTIMES)
    accounts_parser.add_argument("--home", help="Folder for this account (default ~/.cli-subagents/homes/<runtime>/<name>).")
    accounts_parser.add_argument("--no-login", action="store_true")
    accounts_parser.add_argument("--cwd", default=os.getcwd(), help="Project used to resolve local MCP servers for sync.")

    setup_parser = sub.add_parser(
        "setup", help="Install the plugin into Claude Code, Codex and Antigravity and register existing account folders.",
        description="Installs the cli-subagents plugin through each host's own terminal commands, registers "
        "existing logged-in account folders, and lists what is left for you (logins, restarts). "
        "It never runs a login and never reads credential files.",
    )
    setup_parser.add_argument(
        "--host", action="append", choices=("claude", "codex", "antigravity", "auto"),
        help="Host to install into; repeat for several (default auto: every host that is installed).",
    )
    setup_parser.add_argument(
        "--account", action="append", default=[], metavar="[RUNTIME:]NAME=FOLDER",
        help="Register an existing, logged-in account folder and sync it, e.g. max-2=~/.claude-2 or "
        "codex:plus-2=~/.codex-2 (the runtime is guessed from the folder when left out). Repeatable.",
    )
    setup_parser.add_argument("--dry-run", action="store_true", help="Print the plan; change nothing (read-only checks still run).")
    setup_parser.add_argument(
        "--claude-route", choices=("auto", "cli", "settings"), default="auto",
        help="auto: `claude plugin ...`, or settings.json when the claude command is missing or fails. "
        "settings: only declare the plugin in settings.json (Claude Code installs it on its next start).",
    )

    update_parser = sub.add_parser(
        "update", help="Reinstall csa from GitHub and update the plugin in every host that has it.",
        description="Reinstalls the csa package with the tool that installed it (pipx, uv or pip) and updates "
        "the cli-subagents plugin through each host's own terminal commands.",
    )
    update_parser.add_argument("--host", action="append", choices=("claude", "codex", "antigravity", "auto"),
                               help="Host to update; repeat for several (default auto: every host that has the plugin).")
    update_parser.add_argument("--no-package", action="store_true", help="Only update the plugins, not the csa package.")
    update_parser.add_argument("--dry-run", action="store_true", help="Print the plan; change nothing (read-only checks still run).")

    doctor = sub.add_parser("doctor", help="Check binaries, logins and shared setup.")
    doctor.add_argument("--runtime", default="claude", choices=RUNTIMES)
    doctor.add_argument("--live", action="store_true", help="Run one tiny job per account (uses a little quota).")
    doctor.add_argument("--model", help="Model for --live jobs (default: haiku for claude, the CLI's default otherwise).")
    doctor.add_argument("--cwd", default=os.getcwd())

    for name, help_text in (("run", "Run a task and print the result."), ("start", "Start a task in the background.")):
        _job_arguments(sub.add_parser(name, help=help_text))
    run_parser = sub.choices["run"]
    run_parser.add_argument(
        "--follow", action="store_true",
        help="Print the worker's steps as they happen, then the result (see csa follow).",
    )
    run_parser.add_argument("--max-wait", type=float, default=540, help="With --follow: stop watching after this many seconds (default 540).")
    follow_parser = sub.add_parser(
        "follow", help="Print a job's steps as they happen, then its result.",
        description="Prints one line per worker event and ends with one marker: DONE or FAILED (with the result), "
        "STILL RUNNING (with the command to keep watching) or QUESTION (with the commands to answer and continue).",
    )
    follow_parser.add_argument("job_id")
    follow_parser.add_argument("--from", dest="from_offset", type=int, metavar="OFFSET",
                               help="Continue at this byte offset, as printed by an earlier follow (default: from the start).")
    follow_parser.add_argument("--max-wait", type=float, default=540, help="Stop watching after this many seconds (default 540).")

    status = sub.add_parser("status", help="Show a job's state.")
    status.add_argument("job_id")
    status.add_argument("--wait", type=float, default=0)
    result = sub.add_parser("result", help="Show a job's result.")
    result.add_argument("job_id")
    result.add_argument("--max-chars", type=int, default=20000)
    cancel = sub.add_parser("cancel", help="Stop a job.")
    cancel.add_argument("job_id")
    wait = sub.add_parser("wait", help="Wait until a job finishes or sends a message.")
    wait.add_argument("job_ids", nargs="+")
    wait.add_argument("--timeout", type=int, default=1800)
    message = sub.add_parser("message", help="Send a follow-up or an answer to a job.")
    message.add_argument("job_id")
    message.add_argument("text", help="The message. Use '-' to read it from stdin.")
    message.add_argument("--reply-to")
    message.add_argument("--follow", action="store_true",
                         help="Follow the running job or its new continuation, with an authenticated header and status.")
    message.add_argument("--from", dest="from_offset", type=int, metavar="OFFSET",
                         help="With --follow: continue at this event offset if the same job is still running; "
                         "a new continuation always starts from the beginning.")
    message.add_argument("--max-wait", type=float, default=540,
                         help="With --follow: stop watching after this many seconds (default 540).")
    from cli_subagents.follow import DEFAULT_WINDOW

    for watcher in (run_parser, follow_parser, message):
        watcher.add_argument("--window", type=_positive_seconds, nargs="?", const=DEFAULT_WINDOW, metavar="SECONDS",
                             help="Return after this many seconds and repeat the window in continuations "
                             "(bare --window: 60; omitted: keep --max-wait behavior).")
    jobs_parser = sub.add_parser("jobs", help="List recent jobs.")
    jobs_parser.add_argument("--limit", type=int, default=20)

    launcher = sub.add_parser("launch", help="Open an interactive session on the best account with the shared setup.")
    launcher.add_argument("--runtime", default="claude", choices=RUNTIMES)
    launcher.add_argument("--account")

    hook = sub.add_parser("hook", help="Claude Code hook commands (see hooks/hooks.json).")
    hook.add_argument(
        "action", choices=("await", "pending", "inbox"),
        help="await: wake the main session when a job finishes or asks something. "
        "pending: list uncollected results at session start. inbox: a worker's follow-ups.",
    )
    hook.add_argument("job_id", nargs="?", help="The worker's job (inbox only).")

    runner = sub.add_parser("_runner", help=argparse.SUPPRESS)
    runner.add_argument("job_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    # Everything after `csa launch [options] --` goes to the launched CLI.
    extra: list[str] = []
    if argv[:1] == ["launch"] and "--" in argv:
        index = argv.index("--")
        argv, extra = argv[:index], argv[index + 1 :]
    args = build_parser().parse_args(argv)
    if getattr(args, "csa_home", None):
        os.environ["CLI_SUBAGENTS_HOME"] = args.csa_home
    if args.command == "hook":
        from cli_subagents.hooks import main as run_hook

        return run_hook(args.action, args.job_id)
    try:
        if args.command == "mcp":
            from cli_subagents.mcp_server import main as serve

            return serve(args.worker)
        if args.command == "_runner":
            from cli_subagents.runner import main as run_job

            return run_job(args.job_id)
        handlers = {
            "rules": rules.command,
            "accounts": cmd_accounts,
            "setup": cmd_setup,
            "update": cmd_update,
            "doctor": cmd_doctor,
            "run": cmd_run,
            "start": cmd_start,
            "status": cmd_status,
            "result": cmd_result,
            "cancel": cmd_cancel,
            "jobs": cmd_jobs,
            "wait": lambda a: _print(tools.call("subagent_wait", {"job_ids": a.job_ids, "timeout_seconds": a.timeout})) or 0,
            "message": cmd_message,
            "follow": cmd_follow,
        }
        if args.command == "launch":
            return cmd_launch(args, extra)
        return handlers[args.command](args)
    except (ValueError, RuntimeError, tools.ToolError) as exc:
        print(f"csa: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
