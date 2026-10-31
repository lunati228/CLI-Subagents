"""Fixed rules: decide what they can, hand the rest to the reviewer.

`decide(tool_call, payload, context)` returns a Verdict:

    approve  the gate answers auto_approve (no model call)
    deny     the gate answers deny; the reason goes back to the agent
    review   the reviewer decides (or, with reviewer = "off", the gate denies)

Tool names and argument names are the ones agy 1.3.6 sends to PreToolUse hooks
(checked live): view_file{AbsolutePath}, write_to_file{TargetFile},
run_command{CommandLine, Cwd, BypassSandbox}, call_mcp_tool{ServerName, ToolName,
Arguments}, list_dir{DirectoryPath}, grep_search{SearchPath}, find_by_name{SearchDirectory}.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any, Iterable

from . import commands, paths

READ_TOOLS = {"view_file", "list_dir", "find_by_name", "grep_search", "read_file", "read_many_files", "view_file_outline",
              "view_code_item", "codebase_search", "search_files"}
WRITE_TOOLS = {"write_to_file", "replace_file_content", "multi_replace_file_content", "sed_file", "notebook_edit",
               "write_file", "edit_file", "create_file", "delete_file", "mkdir"}
COMMAND_TOOLS = {"run_command"}
# Tools that only touch agy's own UI state, wait, ask, search the web or look at an already open page.
SAFE_TOOLS = {
    "finish", "wait", "wait_5_seconds", "ask_question", "list_permissions", "manage_task", "command_status",
    "search_web", "generate_image", "list_resources", "search_marketplace", "list_plugin_accounts",
    "define_subagent", "invoke_subagent", "manage_subagents", "list_browser_pages", "read_browser_page",
    "browser_get_dom", "capture_browser_screenshot", "capture_browser_console_logs", "browser_list_network_requests",
    "browser_get_network_request",
}
# The agent asking agy for a permission: in a headless run that would end the run, so the gate answers instead.
PERMISSION_TOOLS = {"ask_permission", "ask_custom_permission"}
PATH_KEYS = {"absolutepath", "targetfile", "directorypath", "searchdirectory", "searchpath", "path", "paths", "file",
             "files", "filepath", "directory", "notebookpath", "targetpath", "sourcepath", "destinationpath"}


@dataclass
class Verdict:
    action: str  # approve | deny | review
    reason: str
    quiet: bool = False  # routine approval: logged, not shown in the live view
    why: str = ""  # for the reviewer: why the rules couldn't decide


def approve(reason: str, quiet: bool = False) -> Verdict:
    return Verdict("approve", reason, quiet)


def deny(reason: str) -> Verdict:
    return Verdict("deny", reason)


def review(why: str) -> Verdict:
    return Verdict("review", "", why=why)


class Scope:
    """Where this job may read and write, from the context and the hook payload."""

    def __init__(self, context: dict[str, Any], payload: dict[str, Any]) -> None:
        listed = [str(item) for item in context.get("workspaces") or [] if item]
        self.workspaces = [os.path.normpath(paths.resolve(item)) for item in listed] + [os.path.normpath(item) for item in listed]
        self.artifacts = [os.path.normpath(str(payload["artifactDirectoryPath"]))] if payload.get("artifactDirectoryPath") else []
        self.read_paths = [os.path.normpath(str(item)) for item in context.get("read_paths") or [] if item]
        self.read_paths += [os.path.normpath(paths.resolve(str(item))) for item in context.get("read_paths") or [] if item]
        self.agent_home = str(context.get("agent_home") or "")
        self.user_home = str(context.get("user_home") or "")
        self.homes = [home for home in (self.agent_home, self.user_home) if home]
        self.protected = [os.path.normpath(str(item)) for item in context.get("protected_paths") or [] if item]
        self.secrets = [os.path.normpath(paths.resolve(str(item))) for item in context.get("secret_paths") or [] if item]
        self.access = str(context.get("access") or "read")
        self.sandbox = bool(context.get("sandbox"))
        self.trusted_mcp = {str(item) for item in context.get("trusted_mcp_servers") or []}
        self.base = self.workspaces[0] if self.workspaces else os.getcwd()

    def check(self, value: str, base: str | None = None) -> tuple[str, str | None]:
        """(resolved path, secret reason or None)."""

        normal, resolved, reason = paths.check(value, base=base or self.base, homes=self.homes,
                                               trusted=[*self.workspaces, *self.artifacts, *self.read_paths])
        if reason is None and any(paths.inside_any(form, self.secrets) for form in (normal, resolved)):
            reason = "it is an agent login or service credential"
        if reason is None:
            for form in (normal, resolved):
                # The worker's own home may lie inside a protected folder (CLI Subagents keeps it there);
                # inside it the normal rules apply: skills and plans are fine, logins and keys are not.
                if paths.inside_any(form, self.protected) and not (self.agent_home and paths.inside(form, self.agent_home)):
                    reason = "it is CLI Subagents' own state (jobs, other accounts, config)"
                    break
        return resolved, reason

    def readable(self, resolved: str) -> bool:
        return paths.inside_any(resolved, [*self.workspaces, *self.artifacts, *self.read_paths])

    def writable(self, resolved: str) -> bool:
        return paths.inside_any(resolved, self.workspaces)

    def in_artifacts(self, resolved: str) -> bool:
        return paths.inside_any(resolved, self.artifacts)


def path_args(args: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for key, value in args.items():
        if key.lower() not in PATH_KEYS:
            continue
        if isinstance(value, str) and value.strip():
            found.append(value)
        elif isinstance(value, list):
            found += [item for item in value if isinstance(item, str) and item.strip()]
    return found


def _truthy(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().strip('"').lower() == "true")


def decide(tool_call: dict[str, Any], payload: dict[str, Any], context: dict[str, Any]) -> Verdict:
    name = str(tool_call.get("name") or "")
    args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
    scope = Scope(context, payload)

    if name in PERMISSION_TOOLS:
        return deny("permissions are decided by the review gate; try the action itself instead of asking")
    if name in SAFE_TOOLS:
        return approve("safe tool", quiet=True)

    # Any path argument that names a secret ends the call, whatever the tool (MCP arguments included).
    for value in [*path_args(args), *embedded_paths(args)]:
        _resolved, reason = scope.check(value)
        if reason:
            return deny(f"this path is off limits: {reason}")

    if name in READ_TOOLS:
        return _read(name, args, scope)
    if name in WRITE_TOOLS:
        return _write(name, args, scope)
    if name in COMMAND_TOOLS:
        return _command(args, scope)
    # A `why` reaches the reviewer among the gate's trusted facts, so it never quotes a name the
    # agent chose (an MCP server name or tool name could carry instructions); the action shows them.
    if name == "send_command_input":
        return review("it sends input to a running command")
    if name == "call_mcp_tool":
        server = str(args.get("ServerName") or "")
        if server in scope.trusted_mcp:
            return approve(f"{server} is CLI Subagents' own channel to the main session")
        return review("it calls a tool of an MCP server that isn't CLI Subagents' own (server and tool named in the action)")
    if name == "read_resource":
        server = str(args.get("ServerName") or args.get("Server") or "")
        if server in scope.trusted_mcp:
            return approve("own MCP server", quiet=True)
        return review("it reads a resource from an MCP server")
    if name == "read_url_content":
        return review("it fetches a web page")
    if name.startswith("browser_") or name in ("open_browser_url", "click_browser_pixel", "execute_browser_javascript",
                                               "browser_subagent"):
        if scope.access == "read" and name in ("execute_browser_javascript",):
            return deny("read-only job: no browser scripts")
        return review("it drives a browser")
    return review("the gate has no fixed rule for this tool (named in the action)")


def _read(name: str, args: dict[str, Any], scope: Scope) -> Verdict:
    targets = path_args(args)
    if not targets:
        return review(f"{name} without a recognised path")
    if scope.access == "read" and name not in ("list_dir", "find_by_name"):
        for value in targets:
            resolved, _reason = scope.check(value)
            if commands.GLOB_CHARS & set(value) or not os.path.isfile(resolved):
                return review("a broad content read can reach unchecked key or secrets files; name individual files")
    outside = []
    for value in targets:
        resolved, _reason = scope.check(value)
        if not scope.readable(resolved):
            outside.append(resolved)
    if not outside:
        return approve("read inside the workspace or the shared setup", quiet=True)
    return review("it reads outside the workspace")


def _write(name: str, args: dict[str, Any], scope: Scope) -> Verdict:
    targets = path_args(args)
    if not targets:
        return review(f"{name} without a recognised path")
    needs_review = ""
    for value in targets:
        resolved, _reason = scope.check(value)
        if scope.in_artifacts(resolved):
            continue  # agy's own plan and task files for this conversation
        protection = paths.write_protection(resolved) or paths.write_protection(paths.normalize(value, scope.base))
        if protection:
            return deny(f"edits here are not allowed: {protection}")
        if scope.access != "edit":
            return deny("this is a read-only job; it may not change files")
        if not scope.writable(resolved):
            needs_review = "it writes outside the workspace"
            continue
        note = paths.needs_review_for_write(resolved)
        if note and not needs_review:
            needs_review = note
    if needs_review:
        return review(needs_review)
    return approve("edit inside the workspace", quiet=True)


def _command(args: dict[str, Any], scope: Scope) -> Verdict:
    line = str(args.get("CommandLine") or args.get("Command") or "").strip()
    if not line:
        return deny("empty command")
    cwd_value = str(args.get("Cwd") or scope.base)
    cwd, cwd_reason = scope.check(cwd_value)
    if cwd_reason:
        return deny(f"the working folder is off limits: {cwd_reason}")
    reason = commands.denial(line)
    if reason:
        return deny(f"this command is not allowed: {reason}")
    named = commands.path_words(line)
    for word in named:
        _resolved, secret = scope.check(word, base=cwd)
        if secret:
            return deny(f"the command names a path that is off limits: {secret}")
    # A wildcard that matches a secret file (cat .en*) is not plainly safe: the reviewer decides.
    wildcard_secret = any(scope.check(match, base=cwd)[1] for match in commands.path_words(line, cwd) if match not in named)
    reason = commands.destructive_delete(line, cwd=cwd, inside_workspace=scope.writable)
    if reason:
        return deny(f"this command is not allowed: {reason}")
    suspicious = commands.suspicious(line) or ("a wildcard in it matches a key or secrets file" if wildcard_secret else None)

    def path_ok(value: str) -> bool:
        resolved, secret = scope.check(value, base=cwd)
        return secret is None and scope.readable(resolved)

    if not scope.readable(cwd):
        return review("the command runs outside the workspace")
    if scope.access == "read":
        broad_read = commands.content_read_review(line, cwd)
        if broad_read:
            return review(broad_read)
    if not suspicious and commands.read_only(line, cwd=cwd, path_ok=path_ok):
        return approve("read-only command")
    # A sandbox may allow writes to the workspace, including .git/hooks and
    # agent settings that execute later outside it. Never use sandboxing as a
    # reason to auto-approve arbitrary code in a read job, even when it builds
    # a protected path dynamically so touches_protected cannot see it.
    if scope.access == "read":
        return review("the command isn't plainly read-only and this is a read-only job")
    sandboxed = scope.sandbox and not _bypass(args)
    if sandboxed and scope.access == "edit" and not suspicious:
        if not scope.writable(cwd):
            return review("the command runs outside the workspace")
        if commands.touches_protected(line):
            return review("the command names git internals, hooks or agent settings, which the sandbox doesn't protect")
        return approve("runs in agy's sandbox (no network, writes only to the workspace and temp folders)")
    if _bypass(args) and scope.sandbox:
        return review("the agent asks to run this command outside the sandbox")
    if suspicious:
        return review(f"the command looks risky: {suspicious}")
    return review("the command isn't plainly read-only" + ("" if scope.access == "edit" else " and this is a read-only job"))


def _bypass(args: dict[str, Any]) -> bool:
    return _truthy(args.get("BypassSandbox"))


def summarize(tool_call: dict[str, Any], limit: int = 160) -> str:
    """A short one-line description of the call for the log and the live view."""

    args = tool_call.get("args") if isinstance(tool_call.get("args"), dict) else {}
    for key in ("CommandLine", "AbsolutePath", "TargetFile", "DirectoryPath", "SearchPath", "SearchDirectory", "Url", "URL"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return text if len(text) <= limit else text[: limit - 3] + "..."
    if tool_call.get("name") == "call_mcp_tool":
        return f"{args.get('ServerName')}/{args.get('ToolName')}"
    return ""


CONTENT_KEYS = {"commandline", "command", "codecontent", "replacementcontent", "content", "instruction", "description",
                "toolaction", "toolsummary", "targetcontent", "query", "prompt"}
ABSOLUTE_RE = re.compile(r"^([a-zA-Z]:[\\/]|/|~[\\/]|file:///)")


def embedded_paths(value: Any, depth: int = 0) -> Iterable[str]:
    """Strings anywhere in the arguments that look like one absolute path (file contents and commands skipped)."""

    if depth > 6:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() not in CONTENT_KEYS:
                yield from embedded_paths(item, depth + 1)
    elif isinstance(value, list):
        for item in value[:100]:
            yield from embedded_paths(item, depth + 1)
    elif isinstance(value, str) and len(value) < 1000 and "\n" not in value and ABSOLUTE_RE.match(value.strip()):
        yield value.strip()
