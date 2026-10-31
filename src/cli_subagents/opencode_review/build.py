"""Ordinary build and test commands, approved without a reviewer call when an OpenCode job runs sandboxed.

This only applies when csa runs commands inside an OS sandbox (`"sandbox": true`
in the review context) and the job has edit access. The sandbox is what makes
these commands lower risk: `npm test` runs whatever the project's test script
says, and only the sandbox confines it. Without the flag, the same commands go
to the reviewer.

The match is deliberately narrow: one known build or test program with a known
subcommand, no chaining, pipes, redirection, variables or wildcards, and every
path-like argument inside the workspace and clear of secrets. git, package
installs, npx and anything that publishes stay with the fixed rules and the
reviewer.

With an open network, explicit transfer tools and publish/upload actions (even
passed as build arguments) also need review. Command text cannot tell whether
the project's test or build scripts send data; MXC's open network permits that.
"""

from __future__ import annotations

import os
import re
from typing import Callable

from cli_subagents.agy_review import commands

# Script names a package manager may run: test, build, lint and type checks, with suffixes (test:unit, build-prod).
SCRIPT_RE = re.compile(r"(test|tests|build|lint|check|typecheck|type-check|compile)([:._-][A-Za-z0-9:._-]*)?", re.IGNORECASE)
ANY = None  # the program itself is a build or test tool; its options are still checked
PACKAGE_MANAGERS = {"npm", "pnpm", "yarn", "bun"}
# Program -> allowed first subcommand (ANY: no subcommand needed). gradle and dotnet
# are left out: a sandboxed run can hand its work to a build daemon or node that an
# earlier, unsandboxed run left behind (Gradle daemon, MSBuild node reuse, VBCSCompiler).
SUBCOMMANDS: dict[str, set[str] | None] = {
    "cargo": {"build", "b", "test", "t", "check", "c", "clippy", "fmt", "bench", "doc"},
    "go": {"build", "test", "vet", "fmt"},
    "mvn": {"compile", "test-compile", "test", "package", "verify"},
    "deno": {"test", "check", "lint", "fmt"},
    "swift": {"build", "test"},
    "ruff": {"check", "format"},
    "pytest": ANY, "mypy": ANY, "pyright": ANY, "flake8": ANY, "pylint": ANY, "black": ANY,
    "tsc": ANY, "eslint": ANY, "prettier": ANY, "vitest": ANY, "jest": ANY, "ctest": ANY,
}
PYTHON = {"python", "python3", "py"}
PYTHON_MODULES = {"pytest", "unittest", "mypy", "ruff", "flake8", "pylint", "black", "compileall"}
MAKE_TARGETS = re.compile(r"all|" + SCRIPT_RE.pattern, re.IGNORECASE)
# Programs that run every goal they are given: each positional must be an allowed one.
EVERY_GOAL = {"mvn"}
# Options that run another program, load code or config, or pick other targets. Such a
# command goes to the reviewer even when its value looks harmless (`go test -exec sh`).
RISKY_OPTIONS = {
    "-exec", "-toolexec", "-vettool", "--config", "--eval", "--script-shell", "--shell", "--require", "-r",
    "--import", "--loader", "--experimental-loader", "--node-options", "-p", "--plugin", "--plugins", "-t",
    "--target", "-target", "--rootdir", "-c", "--rcfile", "--settings", "-s", "--global-settings", "-gs",
    "--file", "-f", "--makefile", "-include-dir", "--include-dir", "-i", "--init", "--init-script",
}
# An option value: plain words, numbers and lists only. Paths go in separate words, where they are checked.
OPTION_VALUE_RE = re.compile(r"[A-Za-z0-9._,+-]*")
DRIVE_RELATIVE_RE = re.compile(r"^[A-Za-z]:(?![\\/])")
# Builds can forward these words to scripts. With network access they need a
# reviewer too; accepting `npm test -- curl ...` would hide a visible transfer.
TRANSFER_PROGRAMS = {"curl", "wget", "scp", "sftp", "ssh", "rsync"}
TRANSFER_ACTION_RE = re.compile(r"(?:^|[:._-])(publish|upload|push|deploy)(?:$|[:._-])", re.IGNORECASE)


def _subcommand_ok(name: str, rest: list[str]) -> bool:
    """Whether the program and its first non-option arguments name a build or test step."""

    positional = [word for word in rest if not word.startswith("-")]
    if name in PACKAGE_MANAGERS:
        if not positional:
            return False
        first = positional[0]
        if first == "test" or (first == "t" and name in ("npm", "pnpm")):
            return True
        if first in ("run", "run-script"):
            return len(positional) > 1 and bool(SCRIPT_RE.fullmatch(positional[1]))
        # yarn, pnpm and bun run package scripts by name; bun's own `bun test` is its test runner.
        return name in ("yarn", "pnpm", "bun") and bool(SCRIPT_RE.fullmatch(first))
    if name in PYTHON:
        return len(rest) >= 2 and rest[0] == "-m" and rest[1] in PYTHON_MODULES
    if name == "node":
        return rest[:1] == ["--test"]
    if name == "make":
        return all(MAKE_TARGETS.fullmatch(word) for word in positional)
    if name == "cmake":
        return rest[:1] == ["--build"]
    if name not in SUBCOMMANDS:
        return False
    allowed = SUBCOMMANDS[name]
    if allowed is ANY:
        return True
    if name in EVERY_GOAL:
        return bool(positional) and all(word in allowed for word in positional)
    return bool(positional) and positional[0] in allowed


def build_or_test(line: str, *, cwd: str, path_ok: Callable[[str], bool], sandbox_network: bool = True) -> bool:
    """True for one ordinary build or test command whose arguments all pass `path_ok`.

    `path_ok` gets every operand and option value as written (relative to `cwd`), since
    any of them may name a file, and must say whether it is inside the workspace and
    clear of secrets.

    `sandbox_network` is true unless the caller knows the sandbox blocks the
    network. Installs and direct transfer/publish commands never match either way.
    """

    if not line.strip() or any(char in commands.UNSAFE_CHARS for char in line):
        return False
    path_ok = commands.each_part(path_ok)  # PowerShell passes `a,b` as two arguments
    # The same caution as the read-only parser: quoting that sh, cmd and PowerShell read differently.
    if "'" in line or '\\"' in line or (os.name != "nt" and "\\" in line):
        return False
    if commands.denial(line) or commands.suspicious(line) or commands.touches_protected(line):
        return False
    segments = commands.tokenize(line)
    if not segments or len(segments) != 1 or not segments[0]:
        return False
    words = segments[0]
    if sandbox_network and any(commands.program_name(word).lower() in TRANSFER_PROGRAMS
                               or TRANSFER_ACTION_RE.search(word) for word in words):
        return False
    program = words[0]
    if "/" in program or "\\" in program or commands._shadowed(program, cwd):
        return False
    name = commands.program_name(program)
    rest = words[1:]
    if not _subcommand_ok(name, rest):
        return False
    for word in rest:
        if commands.GLOB_CHARS & set(word) or "://" in word:
            return False
        if word.startswith("-"):
            option = re.split(r"[=:]", word, maxsplit=1)[0]
            if option.lower() in RISKY_OPTIONS or option in RISKY_OPTIONS:
                return False
            if not OPTION_VALUE_RE.fullmatch(word[len(option) + 1:] if len(word) > len(option) else ""):
                return False  # a path, a nested NAME=value or spaces inside an option value
            if any(mark in option for mark in ("/", "\\", "..", "~")):
                return False  # an attached value that can't be split off reliably (-o../out)
            continue
        if DRIVE_RELATIVE_RE.match(word) or commands.PROVIDER_RE.match(word) or "=" in word:
            return False
        # Any operand may name a file.
        if not path_ok(word):
            return False
    return True
