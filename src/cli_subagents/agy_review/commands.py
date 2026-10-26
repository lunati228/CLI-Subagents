"""Shell command rules: a conservative read-only parser and deterministic denials.

The parser accepts a command only when it is plainly read-only: a few known
programs, joined at most by pipes, with no expansion, redirection, chaining,
subshells or script blocks anywhere in the text. Everything it can't prove is
read-only goes to the reviewer; the hard-deny list ends some commands at once.
"""

from __future__ import annotations

import os
import re
from typing import Callable, NamedTuple

# Characters that expand, redirect, chain or start subshells in sh, cmd or PowerShell.
UNSAFE_CHARS = set("`$%<>;&(){}^!@\r\n")

SIMPLE_READERS = {
    "ls", "dir", "gci", "get-childitem", "cat", "type", "gc", "get-content", "head", "tail", "wc", "pwd",
    "get-location", "echo", "write-output", "tree", "which", "where.exe", "file", "stat",
    "du", "select-string", "sls", "findstr", "grep", "egrep", "fgrep", "test-path", "resolve-path",
    "get-item", "gi", "measure-object", "measure", "select-object", "select", "sort-object", "format-table",
    "ft", "format-list", "fl", "out-string", "get-filehash", "nl", "basename", "dirname", "realpath", "date",
    "get-date", "whoami", "hostname", "uname", "rg", "find", "fd", "sort", "diff", "cmp", "md5sum",
    "sha256sum", "certutil",
}
# Version spellings are command-specific: `npm version` writes package files,
# `java version` runs a class, and `make --version` can read a hostile makefile.
VERSION_ONLY = {"python", "python3", "node"}
VERSION_FLAGS = {"--version"}

# Each entry is (flags, options requiring one value). Unknown options go to
# review, including GNU abbreviations. Values are checked as paths as well as
# operands, so -fFILE/--option=FILE cannot hide an unchecked input path.
# Commands with no entry accept operands only. Do not add indirect file-list,
# magic database, config, output, pager, exec or decompression options here.
READ_OPTIONS: dict[str, tuple[str, str]] = {
    "ls": ("-a -A -l -h -R -r -t -S -d -F -1 -n -i --all --almost-all --long --human-readable --recursive", ""),
    "dir": ("/b /s /a /ad /a-d", ""),
    "cat": ("-n -b -s -v -E -T -A --number --number-nonblank --squeeze-blank", ""),
    "head": ("-q -v --quiet --verbose", "-n -c --lines --bytes"),
    "tail": ("-q -v --quiet --verbose", "-n -c --lines --bytes"),
    "wc": ("-l -w -c -m -L --lines --words --bytes --chars --max-line-length", ""),
    "tree": ("-a -d -f /f /a", "-L"),
    "which": ("-a --all", ""),
    "where.exe": ("/q /f /t", "/r"),
    "file": ("-b -i -L -h --brief --mime --mime-type --mime-encoding --dereference --no-dereference", ""),
    "stat": ("-L -f --dereference --file-system", "-c --format --printf"),
    "du": ("-a -s -h -c -b -k -m -L -P --all --summarize --human-readable --total", "-d --max-depth"),
    "grep": ("-i -v -n -H -h -l -L -c -q -s -E -F -w -x -r -R -o -a -I --ignore-case --invert-match --line-number --files-with-matches --files-without-match --count --quiet --recursive --dereference-recursive --fixed-strings --extended-regexp", "-e -A -B -C -m --regexp --after-context --before-context --context --max-count"),
    "findstr": ("/i /l /r /s /n /m /v /x /b /e /p /off /offline", "/c"),
    "rg": ("-i -v -n -N -H -l -c -q -s -S -F -w -x -o -a -U -u -L -0 --hidden --no-ignore --no-ignore-vcs --no-ignore-dot --files --files-with-matches --files-without-match --count --count-matches --line-number --no-line-number --ignore-case --smart-case --case-sensitive --fixed-strings --word-regexp --line-regexp --only-matching --text --multiline --follow --null --no-config", "-e -g -t -T -A -B -C -m -j --regexp --glob --iglob --type --type-not --after-context --before-context --context --max-count --threads --max-depth --max-filesize --color"),
    "find": ("-H -L -P -print -print0 -ls -empty -readable -writable -executable -true -false -a -and -o -or -not", "-name -iname -path -ipath -type -maxdepth -mindepth -size -mtime -mmin -user -group"),
    "fd": ("-H -I -i -s -g -a -L -0 --hidden --no-ignore --ignore-case --case-sensitive --glob --absolute-path --follow --print0", "-e -t -d -E --extension --type --max-depth --exclude"),
    "sort": ("-b -d -f -g -h -i -M -n -r -R -s -u -V -z -c -C --ignore-leading-blanks --dictionary-order --ignore-case --general-numeric-sort --human-numeric-sort --numeric-sort --reverse --stable --unique --version-sort --zero-terminated --check", "-k -t --key --field-separator"),
    "diff": ("-a -b -B -i -q -r -s -u -w -y --text --ignore-space-change --ignore-blank-lines --ignore-case --brief --recursive --report-identical-files", "-U --unified"),
    "cmp": ("-b -l -s --print-bytes --verbose --silent --quiet", "-n -i --bytes --ignore-initial"),
    "md5sum": ("-b -t --binary --text", ""),
    "sha256sum": ("-b -t --binary --text", ""),
    "nl": ("-p --no-renumber", "-b -d -f -h -i -l -n -s -v -w --body-numbering --number-format --number-width --number-separator"),
    "realpath": ("-e -m -s -z --canonicalize-existing --canonicalize-missing --no-symlinks --zero", ""),
    "basename": ("-a -z --multiple --zero", "-s --suffix"),
    "dirname": ("-z --zero", ""),
    "date": ("-u -I -R --utc --universal --iso-8601 --rfc-email", ""),
    "uname": ("-a -s -n -r -v -m -p -i -o --all", ""),
    "whoami": ("/user /groups /priv /all /fo /nh", ""),
    "certutil": ("-hashfile", ""),
}

PS_OPTIONS = {
    "get-childitem": ("-Recurse -Force -File -Directory -Name -Hidden", "-Path -LiteralPath -Filter -Include -Exclude -Depth"),
    "get-content": ("-Raw", "-Path -LiteralPath -TotalCount -Tail -Encoding -ReadCount"),
    "get-location": ("", ""),
    "select-string": ("-CaseSensitive -SimpleMatch -List -Quiet -NotMatch -AllMatches", "-Path -LiteralPath -Pattern -Context -Encoding"),
    "test-path": ("-IsValid", "-Path -LiteralPath -PathType"),
    "resolve-path": ("-Relative", "-Path -LiteralPath"),
    "get-item": ("-Force", "-Path -LiteralPath"),
    "measure-object": ("-Line -Word -Character -Sum -Average -Minimum -Maximum", "-Property"),
    "select-object": ("-Unique", "-Property -Skip -Last -First"),
    "sort-object": ("-Descending -Unique -CaseSensitive", "-Property"),
    "format-table": ("-AutoSize -Wrap -HideTableHeaders", "-Property"),
    "format-list": ("", "-Property"),
    "out-string": ("-Stream", "-Width"),
    "get-filehash": ("", "-Path -LiteralPath -Algorithm"),
    "get-date": ("-AsUTC", "-Format -UFormat"),
}
ALIASES = {
    "gci": "get-childitem", "gc": "get-content", "sls": "select-string", "gi": "get-item",
    "measure": "measure-object", "select": "select-object", "ft": "format-table", "fl": "format-list",
    "egrep": "grep", "fgrep": "grep",
}
# PowerShell drives that aren't files: environment variables, registry, certificates and the like.
PROVIDER_RE = re.compile(r"^(.*\\)?(env|hklm|hkcu|hkcr|hku|hkcc|cert|variable|function|alias|wsman|registry)::?", re.IGNORECASE)

GIT_GLOBAL_OK = {"--no-pager", "-P", "--no-optional-locks", "--literal-pathspecs"}
GIT_OPTIONS = {
    "status": ("-s -b -v --short --branch --porcelain --long --show-stash --no-renames", "--untracked-files --ignored"),
    "log": ("--oneline --stat --name-only --name-status --all --graph --decorate --no-decorate --reverse --first-parent --no-merges --merges -p --patch --no-patch --no-ext-diff --no-textconv --no-color", "-n --max-count --skip --format --pretty --since --until --author --grep"),
    "diff": ("-p -u -w -b --patch --stat --numstat --shortstat --name-only --name-status --check --cached --staged --no-ext-diff --no-textconv --no-color --no-renames --quiet --exit-code", "-U --unified"),
    "show": ("-p --patch --stat --name-only --name-status --no-patch --no-ext-diff --no-textconv --no-color", "--format --pretty"),
    "blame": ("-w -l -s -e --line-porcelain --porcelain --no-textconv", "-L"),
    "branch": ("-a -r -v --list --all --remotes --show-current --verbose --no-color --no-column", "--contains --no-contains --merged --no-merged --points-at --sort"),
    "ls-files": ("-z -c -d -m -o -i -s -u --cached --deleted --modified --others --ignored --stage --unmerged --exclude-standard", "--exclude"),
    "ls-tree": ("-r -t -l -z --name-only --name-status --full-name --full-tree", ""),
}

# Hard denials: (pattern, reason). Matched on the lowercased command with "\" turned into "/".
_DENY_PATTERNS: list[tuple[str, str]] = [
    (r"\bcmdkey\b|\bvaultcmd\b|\bsecret-tool\b|get-storedcredential|credential-manager|\bmimikatz\b"
     r"|\bsecurity\s+(find|dump|export)-|\bkeyring\s+get\b|convertfrom-securestring|\bcredwiz\b",
     "it reads the system credential store"),
    (r"\bgh\s+auth\s+(token|status\b.*(-t\b|--show-token))|\bgit\s+credential\b|\bgcloud\s+auth\s+print-",
     "it prints a login token"),
    (r"\bgit\b.*\bpush\b.*(\s--force\b|\s-f\b|\s--force-with-lease\b|\s--mirror\b|\s--delete\b|\s-d\b|\s\+\S)",
     "it force-pushes or deletes remote branches"),
    (r"\b(npm|pnpm|yarn|bun)\s+(publish|unpublish)\b|\byarn\s+npm\s+publish\b|\btwine\s+upload\b"
     r"|\b(poetry|uv|flit|hatch)\s+publish\b|\bcargo\s+(publish|yank)\b|\bgem\s+(push|yank)\b"
     r"|\b(docker|podman)\s+push\b|\bgh\s+(release\s+(create|delete|upload)|repo\s+(delete|edit|rename|archive)"
     r"|secret\s+set|pr\s+merge)\b|\b(dotnet\s+)?nuget\s+push\b|\bmvn\b.*\bdeploy\b|\bgradlew?\b.*\bpublish\b"
     r"|\b(vsce|ovsx)\s+publish\b|\bfirebase\s+deploy\b|\bterraform\s+(apply|destroy)\b|\bkubectl\s+(apply|delete|replace)\b",
     "it publishes or deploys"),
    (r"\bgit\s+reset\s+--hard\b|\bgit\s+clean\b.*\s-[a-z]*f|\bgit\s+checkout\s+--\s+\.(\s|$)|\bgit\s+restore\s+\.(\s|$)"
     r"|\bgit\s+stash\s+(drop|clear)\b|\bgit\s+(filter-branch|filter-repo)\b|\bgit\s+update-ref\s+-d\b",
     "it throws away uncommitted or committed work"),
    (r"\bformat(\.com)?\s+[a-z]:|\bdiskpart\b|\bbcdedit\b|\bmkfs\b|\bdd\s+.*of=/dev/|\bshutdown\b|\brestart-computer\b"
     r"|\bstop-computer\b|\bvssadmin\b.*\bdelete\b|\bwbadmin\b.*\bdelete\b|\bcipher\s+/w|\bclear-disk\b|\bformat-volume\b",
     "it can destroy disks or stop the machine"),
    (r"\breg(\.exe)?\s+(add|delete|import|load|save|restore)\b|\bset-mppreference\b|\badd-mppreference\b"
     r"|\bnetsh\s+(advfirewall|firewall)\b|\bset-executionpolicy\b|\bsetx\b|\btakeown\b|\bicacls\b.*\b(grant|setowner)\b"
     r"|\bsystemctl\s+(enable|disable|mask)\b|\bufw\s+disable\b|\bsetenforce\s+0\b|\bsudo\b|\brunas\b",
     "it changes system security settings or raises privileges"),
    (r"\bschtasks\b.*\s/create\b|\b(new|register)-scheduledtask\b|\bsc(\.exe)?\s+(create|config|delete)\b|\bnew-service\b"
     r"|\bcrontab\b|\blaunchctl\s+(load|bootstrap|submit)\b",
     "it creates a scheduled task, service or other persistence"),
    (r"\binvoke-expression\b|\biex\b|-encodedcommand\b|frombase64string"
     r"|\b(powershell|pwsh)(\.exe)?\b[^|]*\s-(e|ec|en|enc|enco|encod|encode|encoded)\s"
     r"|\b(curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod)\b[^|]*\|\s*(sh|bash|zsh|python[0-9.]*|node|iex|pwsh|powershell)\b",
     "it runs hidden or downloaded code"),
]
DENY_RES = [(re.compile(pattern), reason) for pattern, reason in _DENY_PATTERNS]
DELETE_COMMANDS = {"rm", "rmdir", "rd", "del", "erase", "remove-item", "ri", "rimraf", "shred", "unlink"}
# Paths in a command that should make even a sandboxed command go to the reviewer.
PROTECTED_MARKERS = (
    ".git/hooks", ".git/config", ".git/info", "core.hookspath", "core.fsmonitor", "core.sshcommand", "core.pager",
    "core.editor", "git config", "hooks.json", "/.agents", ".agents/", "/.gemini", ".gemini/", "/.claude", ".claude/",
    "/.codex", ".codex/", ".husky", ".vscode", "init --template", "settings.json",
    "/.opencode", ".opencode/", "opencode.json",
)


def tokenize(text: str) -> list[list[str]] | None:
    """Split a command at top-level pipes into word lists. None if quoting is unbalanced."""

    segments: list[list[str]] = [[]]
    word: list[str] = []
    quote = ""
    started = False

    def end_word() -> None:
        nonlocal word, started
        if started:
            segments[-1].append("".join(word))
        word, started = [], False

    for char in text:
        if quote:
            if char == quote:
                quote = ""
            else:
                word.append(char)
            continue
        if char in "'\"":
            quote, started = char, True
        elif char == "|":
            end_word()
            segments.append([])
        elif char.isspace():
            end_word()
        else:
            word.append(char)
            started = True
    if quote:
        return None
    end_word()
    return segments


def program_name(word: str) -> str:
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix) and name != "where.exe":
            return name[: -len(suffix)]
    return name


def unquoted(command: str) -> str:
    """The command with the contents of quoted strings removed (commit messages and the like)."""

    out: list[str] = []
    quote = ""
    for char in command:
        if quote:
            if char == quote:
                quote = ""
                out.append(char)
            continue
        if char in "'\"":
            quote = char
        out.append(char)
    return "".join(out)


def _match(text: str) -> str | None:
    text = " " + text.lower().replace("\\", "/") + " "
    for pattern, reason in DENY_RES:
        if pattern.search(text):
            return reason
    return None


def denial(command: str) -> str | None:
    """A reason this command must never run, or None. Quoted text doesn't count (see `suspicious`)."""

    return _match(unquoted(command))


INTERPRETER_RE = re.compile(
    r"\b(bash|sh|zsh|dash|cmd|powershell|pwsh|python[0-9.]*|py|node|deno|perl|ruby|php|ssh|wsl|xargs|env)(\.exe)?\b"
    r"|\b(invoke-expression|start-process|docker\s+exec|kubectl\s+exec)\b",
    re.IGNORECASE,
)


def suspicious(command: str) -> str | None:
    """A deny reason that matches only inside quoted text that an interpreter may run (`bash -c "..."`).

    Quoted text given to other programs (a commit message) is data and doesn't count.
    """

    if denial(command) or not INTERPRETER_RE.search(unquoted(command)):
        return None
    return _match(command)


def _flag(word: str) -> bool:
    return word.startswith("-") or (os.name == "nt" and bool(re.fullmatch(r"/[a-zA-Z?]{1,2}", word)))


def destructive_delete(command: str, *, cwd: str, inside_workspace: Callable[[str], bool]) -> str | None:
    """A reason when a delete targets something outside the workspace, or a target that can't be resolved."""

    segments = tokenize(command) or [command.split()]
    for words in segments:
        if not words or program_name(words[0]) not in DELETE_COMMANDS:
            continue
        targets = [word for word in words[1:] if not _flag(word)]
        if not targets:
            return "it deletes without a clear target"
        for target in targets:
            if any(char in target for char in "$%`") or target.startswith("~"):
                return "it deletes with a target that isn't resolved yet (a variable or the home folder)"
            stem = re.split(r"[*?\[]", target, maxsplit=1)[0].strip()
            if stem and re.fullmatch(r"([a-zA-Z]:)?[\\/]*", stem):
                return "it deletes at a drive or file-system root"
            if not inside_workspace(os.path.normpath(os.path.join(cwd, stem or "."))):
                return "it deletes outside the workspace"
    return None


def touches_protected(command: str) -> bool:
    text = command.lower().replace("\\", "/")
    return any(marker in text for marker in PROTECTED_MARKERS)


def _option_value(word: str) -> str:
    """The value part of `--opt=value` or PowerShell's `-Param:value`; the word itself otherwise."""

    if word.startswith("-"):
        for separator in ("=", ":"):
            if separator in word:
                return word.split(separator, 1)[1]
        return ""
    return word


GLOB_CHARS = set("*?[")
MAX_GLOB_MATCHES = 200
# These list names and have no file-writing or program-executing options for
# a shell glob to inject. find, fd and tree do have such options.
GLOB_SAFE_PROGRAMS = {"ls", "dir", "gci", "get-childitem"}


def path_words(command: str, cwd: str | None = None) -> list[str]:
    """Every word of a command that may name a file (for the secret check), quoted or not.

    `rev:path` (git show HEAD:.env) adds the path part; with `cwd`, wildcards add what they match there.
    """

    segments = tokenize(command) or [command.split()]
    found = []
    for words in segments:
        for word in words:
            for piece in re.split(r"[;,|&<>()]", word):
                value = _option_value(piece) if piece.startswith("-") else piece
                value = value.strip("'\"")
                if not value:
                    continue
                candidates = [value]
                if ":" in value and not re.match(r"^[a-zA-Z]:[\\/]", value):
                    candidates.append(value.split(":", 1)[1])
                for candidate in candidates:
                    if candidate:
                        found.append(candidate)
                    if cwd and candidate and GLOB_CHARS & set(candidate):
                        found += expand(candidate, cwd)
    return found


def expand(pattern: str, cwd: str) -> list[str]:
    """What a wildcard matches, relative to `cwd` (capped), hidden files included."""

    import glob
    import itertools

    try:
        matches = glob.iglob(pattern, root_dir=cwd, recursive="**" in pattern, include_hidden=True)
        return list(itertools.islice(matches, MAX_GLOB_MATCHES))
    except (OSError, ValueError, TypeError):
        return []


class _Arguments(NamedTuple):
    values: list[str]  # operands and option values, all checked for paths
    positional: list[str]
    options: set[str]


def _arguments(
    words: list[str], spec: tuple[str, str], *, powershell: bool = False, numeric: bool = False,
) -> _Arguments | None:
    """Parse only known options, returning every operand and option value.

    Short clusters are parsed left to right; a value option consumes the rest
    of its word or the next word. Unknown spellings fail closed. PowerShell
    parameter abbreviations are deliberately not accepted.
    """

    fold = str.casefold if powershell else str
    flags, values = ({fold(item) for item in group.split()} for group in spec)
    result = _Arguments([], [], set())
    index = 0
    ended = False
    while index < len(words):
        word = words[index]
        index += 1
        if ended:
            result.values.append(word)
            result.positional.append(word)
            continue
        if word == "--":
            ended = True
            continue
        separator = ":" if powershell or word.startswith("/") else "="
        key, sep, value = word.partition(separator)
        key = fold(key)
        if key in values:
            result.options.add(key)
            if not sep:
                if index >= len(words):
                    return None
                value = words[index]
                index += 1
            result.values.append(value)
            continue
        if key in flags and not sep:
            result.options.add(key)
            continue
        if numeric and re.fullmatch(r"-\d+", word):
            continue
        if word.startswith("-") and word != "-":
            if powershell or word.startswith("--"):
                return None
            # GNU short options, including -oFILE and -xo FILE. A one-dash
            # long option (find -exec, for example) also fails here.
            for pos, char in enumerate(word[1:], 1):
                option = "-" + char
                result.options.add(option)
                if option in values:
                    value = word[pos + 1 :]
                    if not value:
                        if index >= len(words):
                            return None
                        value = words[index]
                        index += 1
                    result.values.append(value)
                    break
                if option not in flags:
                    return None
            continue
        if os.name == "nt" and re.match(r"^/[A-Za-z]", word):
            return None  # unknown cmd.exe switch; do not treat it as a path
        result.values.append(word)
        result.positional.append(word)
    return result


def _git_ok(words: list[str], path_ok: Callable[[str], bool]) -> bool:
    if len(words) == 2 and words[1] in ("--version", "version"):
        return True
    index = 1
    while index < len(words) and words[index].startswith("-"):
        option = words[index]
        if option == "-C" and index + 1 < len(words):
            if not path_ok(words[index + 1]):
                return False
            index += 2
            continue
        if option not in GIT_GLOBAL_OK:
            return False
        index += 1
    if index >= len(words):
        return False
    sub, rest = words[index], words[index + 1 :]
    if sub not in GIT_OPTIONS:
        return False
    # `git branch NAME` creates a branch, even without a mutating flag. Keep
    # branch queries to flags alone; --list patterns can use the reviewer.
    if sub == "branch" and any(not word.startswith("-") for word in rest):
        return False
    # After --, every operand is a literal path, including HEAD~1. Before it,
    # Git's HEAD ancestry and rev:path syntax denote revisions/object paths.
    # Do not mistake HEAD~1 for a Windows DOS short filename.
    if "--" in rest:
        separator = rest.index("--")
        if not all(path_ok(path) for path in rest[separator + 1 :]):
            return False
        rest = rest[:separator]
    operands = _arguments(rest, GIT_OPTIONS[sub], numeric=sub == "log")
    if operands is None:
        return False
    for value in operands.values:
        if sub in ("log", "diff", "show", "blame"):
            if re.fullmatch(r"HEAD(?:~\d+)+", value):
                continue
            if ":" in value and not re.match(r"^[a-zA-Z]:[\\/]", value):
                value = value.split(":", 1)[1]
        if not path_ok(value):
            return False
    return True


def read_only(command: str, *, cwd: str, path_ok: Callable[[str], bool]) -> bool:
    """True when the command is plainly read-only and every path it names passes `path_ok`.

    `path_ok` gets each path-like word as written (relative paths are relative to `cwd`).
    """

    if not command.strip() or any(char in UNSAFE_CHARS for char in command):
        return False
    # This grammar is shared by sh, cmd and PowerShell. Single quotes do not
    # group arguments in cmd, and backslash escapes in sh can turn a supposed
    # operand into an option (sort \\-oFILE). Do not prove safety using a
    # tokenization that can differ from the shell that will run it.
    if "'" in command or '\\"' in command or (os.name != "nt" and "\\" in command):
        return False
    segments = tokenize(command)
    if not segments or any(not words for words in segments):
        return False
    for words in segments:
        name = program_name(words[0])
        if "/" in words[0] or "\\" in words[0] or _shadowed(words[0], cwd):
            return False
        if any(PROVIDER_RE.match(_option_value(word) if word.startswith("-") else word) for word in words[1:]):
            return False  # PowerShell drives such as env:, hklm:, cert: (environment variables, registry, keys)
        rest = words[1:]
        if name not in GLOB_SAFE_PROGRAMS and any(GLOB_CHARS & set(word) for word in rest):
            return False
        if name == "git":
            if not _git_ok(words, path_ok):
                return False
            continue
        if name in VERSION_ONLY:
            if len(rest) != 1 or rest[0] not in VERSION_FLAGS:
                return False
            continue
        if "/" in words[0] or "\\" in words[0]:
            return False
        if name not in SIMPLE_READERS:
            return False
        name = ALIASES.get(name, name)
        powershell = name in PS_OPTIONS
        spec = PS_OPTIONS[name] if powershell else READ_OPTIONS.get(name, ("", ""))
        operands = _arguments(rest, spec, powershell=powershell, numeric=name in ("head", "tail"))
        if operands is None:
            return False
        if name == "certutil" and (len(rest) not in (2, 3) or rest[0].lower() != "-hashfile"):
            return False
        if name == "hostname" and rest:
            return False  # a positional name changes the machine's hostname
        if name == "date" and any(not value.startswith("+") for value in operands.positional):
            return False  # a bare timestamp sets the clock, even without -s
        if not all(path_ok(path) for path in operands.values):
            return False
    return True


def content_read_review(command: str, cwd: str) -> str | None:
    """Why a read command can reach files not checked as literal arguments.

    Do not enumerate globs or directory trees to prove safety: expansions can
    exceed a cap, follow links, or differ between shells (including hidden
    files). A reviewer must assess these reads. Listing filenames is fine.
    """

    segments = tokenize(command)
    if not segments:
        return None
    if len(segments) > 1:
        return "a pipeline can supply unchecked filenames to a content reader"
    words = segments[0]
    if not words:
        return None
    name = ALIASES.get(program_name(words[0]), program_name(words[0]))
    rest = words[1:]
    if name not in GLOB_SAFE_PROGRAMS and any(GLOB_CHARS & set(word) for word in rest):
        return "wildcards can include unchecked key or secrets files"
    if name in ("grep", "diff"):
        if any(word in ("--recursive", "--dereference-recursive") or
               (word.startswith("-") and not word.startswith("--") and any(c in word[1:] for c in "rR"))
               for word in rest):
            return "a recursive content reader can reach unchecked key or secrets files"
    if name == "findstr" and any(word.lower() == "/s" for word in rest):
        return "a recursive content reader can reach unchecked key or secrets files"
    if name == "rg" and "--files" not in rest:
        operands = _arguments(rest, READ_OPTIONS["rg"])
        # Ripgrep recurses by default. Only the simple pattern + explicitly
        # named regular files form has a bounded set of content inputs.
        inputs = [] if operands is None else (
            operands.positional if operands.options & {"-e", "--regexp"} else operands.positional[1:]
        )
        if not inputs or not all(os.path.isfile(os.path.join(cwd, value)) for value in inputs):
            return "ripgrep can search unchecked files recursively; name individual files"
    return None


def _shadowed(word: str, cwd: str) -> bool:
    """True if a file in the working folder could run instead of the program (cmd.exe looks there first)."""

    base = word.replace("\\", "/").rsplit("/", 1)[-1]
    try:
        names = {entry.lower() for entry in os.listdir(cwd)}
    except OSError:
        return False
    stem = base.lower()
    return any(candidate in names for candidate in (stem, *(stem + ext for ext in (".exe", ".cmd", ".bat", ".com", ".ps1"))))
