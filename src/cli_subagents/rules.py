"""Render and install the optional delegation rules."""

from __future__ import annotations

import argparse
from html import escape
from importlib.resources import files
import os
from pathlib import Path
import re

from cli_subagents.fsutil import write_private

START = "<!-- cli-subagents:rules:start -->"
END = "<!-- cli-subagents:rules:end -->"
TIERS = ("low", "medium", "high", "xhigh", "max")
VENDORS = {"claude": "Claude", "codex": "OpenAI", "antigravity": "Google"}
DEFAULTS = ("cheapest model", "default model", "strongest model", "strongest model", "strongest model")
USES = (
    "Lookups, file searches, mechanical edits and checks.",
    "Normal implementation, tests and contained fixes.",
    "Hard debugging, code review and ambiguous problems.",
    "Deep planning, high-impact work, or high was not enough.",
    "Large features and the hardest work. Skip when overkill.",
)


def _text(value: str) -> str:
    if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise argparse.ArgumentTypeError("Use non-empty text on one line.")
    value = value.strip()
    if START in value or END in value:
        raise argparse.ArgumentTypeError("Values cannot contain the rules markers.")
    return "none" if value.lower() == "none" else value


def _limit(value: str, minimum: int) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use a whole number.") from None
    if number < minimum:
        raise argparse.ArgumentTypeError(f"Use a number of at least {minimum}.")
    return number


def add_parser(subparsers) -> None:
    parser = subparsers.add_parser("rules", help="Show, install or remove optional delegation rules.")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--show", action="store_true", help="Print the rules (the default).")
    actions.add_argument("--install", action="store_true", help="Install or replace the managed block.")
    actions.add_argument("--remove", action="store_true", help="Remove only the managed block.")
    parser.add_argument("--target", type=_text, default="claude", metavar="HOST|PATH",
                        help="claude, codex, antigravity, or an instruction file (default: claude).")
    parser.add_argument("--dry-run", action="store_true", help="Print what would change; write nothing.")
    for tier in TIERS:
        parser.add_argument(f"--home-{tier}", type=_text, metavar="MODEL",
                            help=f"Home vendor model for {tier}, or none.")
        parser.add_argument(f"--try-{tier}", type=_text, default="none", metavar="MODEL",
                            help=f"Other CLI model to try for {tier} (default: none).")
    parser.add_argument("--work-folder", type=_text, action="append", default=[], metavar="PATH",
                        help="Work folder and its children; repeat for several.")
    parser.add_argument("--main-limit", type=lambda value: _limit(value, 1), default=4, metavar="N",
                        help="Home vendor subagents started by the main session (default: 4).")
    parser.add_argument("--helper-limit", type=lambda value: _limit(value, 0), default=3, metavar="M",
                        help="Helpers per depth-1 subagent (default: 3; 0 disables helpers).")


def target_path(target: str) -> tuple[Path, str]:
    """Resolve a host or file, without reading any host settings."""
    if target == "claude":
        folder = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        return folder.expanduser() / "CLAUDE.md", target
    if target == "codex":
        folder = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        return folder.expanduser() / "AGENTS.md", target
    if target == "antigravity":
        return Path.home() / ".gemini" / "GEMINI.md", target
    path = Path(target).expanduser()
    vendor = {"agents.md": "codex", "gemini.md": "antigravity"}.get(path.name.lower(), "claude")
    return path, vendor


def _markdown(value: str) -> str:
    return escape(value, quote=False).replace("|", "&#124;").replace("`", "&#96;")


def render(args: argparse.Namespace) -> str:
    _, host = target_path(args.target)
    vendor = VENDORS[host]
    rows = []
    for tier, default, use in zip(TIERS, DEFAULTS, USES):
        home = getattr(args, f"home_{tier}") or f"{vendor} {default}"
        other = getattr(args, f"try_{tier}")
        rows.append(f"| {tier} | {use} | {_markdown(home)} | {_markdown(other)} |")
    folders = "\n".join(f"- {_markdown(folder)}" for folder in args.work_folder)
    if not folders:
        folders = "- No work folders listed. Work code and data still stay with the home vendor."
    template = files("cli_subagents").joinpath("templates", "delegation_rules.md").read_text(encoding="utf-8")
    body = template.format(home_vendor=vendor, tier_rows="\n".join(rows), work_folders=folders,
                           main_limit=args.main_limit, helper_limit=args.helper_limit).strip()
    return f"{START}\n{body}\n{END}"


def _span(data: bytes) -> tuple[int, int] | None:
    start, end = START.encode(), END.encode()
    if start not in data and end not in data:
        return None
    if data.count(start) != 1 or data.count(end) != 1 or data.index(end) < data.index(start):
        raise ValueError("Rules markers are incomplete or repeated. Fix them before changing the block.")
    return data.index(start), data.index(end) + len(end)


def update_file(path: Path, block: str | None, dry_run: bool = False) -> str:
    """Change only marker bytes. Preserve all existing bytes outside them."""
    # A symlinked instruction file (a dotfiles repository, say) is written through, not replaced by a copy.
    target = path.resolve() if path.is_symlink() else path
    existed = target.exists()
    original = target.read_bytes() if existed else b""
    if original.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValueError("The instruction file is UTF-16. Save it as UTF-8 first.")
    span = _span(original)
    if block is None:
        if span is None:
            return f"No delegation rules in {path}; nothing changed."
        updated = original[:span[0]] + original[span[1]:]
        action = "Removed"
    else:
        first_newline = re.search(rb"\r\n|\n|\r", original)
        newline = first_newline.group() if first_newline else os.linesep.encode()
        rendered = block.replace("\n", newline.decode()).encode("utf-8")
        if span is None:
            # The separator is appended; no existing byte is rewritten.
            separator = b"" if not original else (newline if original.endswith((b"\n", b"\r")) else newline * 2)
            updated = original + separator + rendered
            action = "Installed"
        else:
            updated = original[:span[0]] + rendered + original[span[1]:]
            action = "Replaced"
    if updated == original:
        return f"Delegation rules in {path} are unchanged."
    if dry_run:
        verb = {"Installed": "install", "Replaced": "replace", "Removed": "remove"}[action]
        return f"Would {verb} delegation rules in {path}; no files written."
    backup = target.with_name(f"{target.name}.csa-rules-backup")
    backup_created = False
    if existed:
        try:
            with backup.open("xb") as handle:
                handle.write(original)
            backup_created = True
        except FileExistsError:
            pass
    write_private(target, updated)
    note = f" Backup: {backup}." if backup_created else ""
    return f"{action} delegation rules in {path}.{note}"


def command(args: argparse.Namespace) -> int:
    if not args.install and not args.remove:
        print(render(args))
        return 0
    path, _ = target_path(args.target)
    try:
        print(update_file(path, None if args.remove else render(args), args.dry_run))
    except OSError as exc:
        raise RuntimeError(f"Cannot change delegation rules in {path}: {exc}") from exc
    return 0
