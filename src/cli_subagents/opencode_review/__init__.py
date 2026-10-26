"""OpenCode v2's tool hook backed by the existing review gate's fixed rules.

No reviewer model is started here. Actions the fixed rules cannot approve are
denied, including arbitrary shell writes and untrusted MCP calls.
"""

from __future__ import annotations

from fnmatch import fnmatchcase
import json
import os
from pathlib import Path
import re
from typing import Any

from cli_subagents.agy_review import hook, paths
from cli_subagents.agy_review.context import load_context
from cli_subagents.agy_review import store


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"toolCall": {"name": name, "args": args}}


class Denied(Exception):
    """A tool call the OpenCode translation itself refuses, with the reason shown to the model."""


# csa's own package. With an editable install this is the gate's source, so
# edits there are refused; reading it stays allowed.
PACKAGE_DIR = Path(__file__).resolve().parent.parent
# OpenCode's grep runs `rg --hidden` over a folder and returns matching lines
# without checking read permission per file (v2.1.4 core/src/ripgrep.ts,
# tool/plugin/grep.ts). Folder searches are walked first and refused when a
# searched file could hold secrets.
GREP_SKIP_DIRS = {".git", "node_modules"}
GREP_FILE_LIMIT = 5000
COMPLEX_GLOB = re.compile(r"[\[\]{}!\\]")


def _include_matches(name: str, include: str | None) -> bool:
    """Whether rg's --glob could select this file name. Unsure means yes."""

    if not include:
        return True
    if COMPLEX_GLOB.search(include):
        return True
    last = include.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or "*"
    return fnmatchcase(name.casefold(), last.casefold())


def _check_folder_grep(folder: Path, include: str | None, context: dict[str, Any]) -> None:
    """Raise Denied when a folder grep could return lines of a secrets file."""

    homes = [str(context.get("user_home") or "")]
    seen = 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [item for item in dirs if item not in GREP_SKIP_DIRS]
        for name in files:
            if not _include_matches(name, include):
                continue
            seen += 1
            if seen > GREP_FILE_LIMIT:
                raise Denied(f"CLI Subagents review: this search covers more than {GREP_FILE_LIMIT} files, too many to "
                             "check for secrets. Narrow it with `path` or an `include` glob such as \"*.py\".")
            full = os.path.join(root, name)
            reason = paths.secret_reason(full, homes, trusted=str(folder))
            if reason:
                relative = os.path.relpath(full, folder)
                raise Denied(f"CLI Subagents review: this search would also read {relative} ({reason}). Narrow it with "
                             "an `include` glob that leaves that file out, such as \"*.py\", or a narrower `path`.")


# String.prototype.trim's set (WhiteSpace and LineTerminator). Python's strip()
# also removes \x1c-\x1f and \x85, which JavaScript keeps.
JS_SPACE = "\t\n\x0b\x0c\r \xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
# Line breaks for str.splitlines() but not for OpenCode's patch parser.
ODD_BREAKS = frozenset("\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")
PATCH_HEADERS = ("*** Add File: ", "*** Delete File: ", "*** Update File: ")
MOVE_HEADER = "*** Move to: "


def patch_targets(patch: str) -> list[str] | None:
    """Every path a v2 patch can write, read the way OpenCode reads it, or None to deny.

    OpenCode's parser (packages/util/src/patch.ts in v2.1.4) splits on "\\n" only,
    drops one trailing "\\r", trims headers with JS trim() and reads "*** Move to:"
    after trimEnd(). Header-like lines anywhere count, so the list can only be
    longer than OpenCode's. A patch with other line breaks is denied: the two
    parsers could disagree about where a path ends.
    """

    if ODD_BREAKS & set(patch):
        return None
    targets = []
    for line in patch.split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        header = line.strip(JS_SPACE)
        for prefix in PATCH_HEADERS:
            if header.startswith(prefix):
                targets.append(header[len(prefix):].strip(JS_SPACE))
        moved = line.rstrip(JS_SPACE)
        if moved.startswith(MOVE_HEADER) or header.startswith(MOVE_HEADER):
            source = moved if moved.startswith(MOVE_HEADER) else header
            targets.append(source[len(MOVE_HEADER):].strip(JS_SPACE))
    return targets


def translate(event: dict[str, Any], context: dict[str, Any]) -> list[dict[str, Any]]:
    """Translate v2 tool names/inputs into the gate's existing tool vocabulary.

    Unknown tools deliberately stay unknown and therefore fail closed. Patches
    are checked once for every affected path, including a rename destination.
    """

    name = str(event.get("tool") or "")
    args = event.get("input")
    if not isinstance(args, dict):
        raise ValueError("tool input must be an object")
    base = str((context.get("workspaces") or [os.getcwd()])[0])
    path = args.get("path") or args.get("filePath")
    if name in ("shell", "bash"):
        if context.get("access") == "read":
            return [_call("opencode_read_job_shell_denied", args)]
        return [_call("run_command", {"CommandLine": args.get("command"), "Cwd": args.get("workdir") or args.get("cwd") or base})]
    if name == "read":
        if not isinstance(path, str) or not path.strip():
            raise ValueError("read requires a path")
        target = Path(str(path or ""))
        if not target.is_absolute():
            target = Path(base) / target
        tool = "list_dir" if target.is_dir() else "view_file"
        return [_call(tool, {"AbsolutePath": str(target)})]
    if name == "glob":
        if not isinstance(args.get("pattern"), str) or not args["pattern"].strip():
            raise ValueError("glob requires a pattern")
        return [_call("find_by_name", {**args, "SearchDirectory": str(path or base)})]
    if name == "grep":
        if not isinstance(args.get("pattern"), str) or not args["pattern"].strip():
            raise ValueError("grep requires a pattern")
        target = Path(str(path or base))
        if not target.is_absolute():
            target = Path(base) / target
        # A folder search is allowed inside the workspace when no searched file can hold secrets.
        if target.is_dir():
            include = args.get("include")
            _check_folder_grep(target, include if isinstance(include, str) and include.strip() else None, context)
            return [_call("find_by_name", {**args, "SearchDirectory": str(target)})]
        return [_call("grep_search", {**args, "SearchPath": str(target)})]
    if name in ("edit", "write"):
        return [_call("write_to_file", {"TargetFile": str(path or "")})]
    if name in ("patch", "apply_patch"):
        targets = patch_targets(str(args.get("patchText") or args.get("patch") or ""))
        if targets is None:
            return [_call("opencode_patch_with_unusual_line_breaks", {})]
        return [_call("write_to_file", {"TargetFile": value}) for value in targets] or [_call("opencode_unparsed_patch", args)]
    if name in ("todo", "todoread", "todowrite", "skill", "question"):
        return [_call("manage_task" if name != "question" else "ask_question", args)]
    # v2 sanitizes the server namespace, then joins it to the tool with '_'.
    prefix = "csa-parent_"
    if name.startswith(prefix) and name[len(prefix):] in ("message_parent", "ask_parent", "check_parent_messages"):
        return [_call("call_mcp_tool", {"ServerName": "csa-parent", "ToolName": name[len(prefix):], "Arguments": args})]
    return [_call("opencode_" + name, args)]


def run(raw: bytes, context_path: Path) -> dict[str, Any]:
    """Evaluate a v2 hook input; malformed input/context always denies the call."""

    try:
        if not raw or len(raw) > hook.MAX_INPUT:
            raise ValueError("hook input is empty or too large")
        event = json.loads(raw.decode("utf-8"))
        if not isinstance(event, dict):
            raise ValueError("hook input must be an object")
        context = load_context(context_path)
        # The first version never starts a reviewer, regardless of file contents.
        context["reviewer"] = {"kind": "off"}
        answer: dict[str, Any] = {"decision": "auto_approve"}
        try:
            payloads = translate(event, context)
        except Denied as exc:
            return {"decision": "deny", "reason": str(exc)}
        base = Path(str((context.get("workspaces") or [os.getcwd()])[0]))
        for payload in payloads:
            call = payload["toolCall"]
            if call["name"] == "write_to_file":
                target = Path(str(call["args"].get("TargetFile") or ""))
                target = (target if target.is_absolute() else base / target).resolve()
                if target == PACKAGE_DIR or PACKAGE_DIR in target.parents:
                    return {"decision": "deny", "reason": "CLI Subagents review: edits to csa's own package are not allowed."}
        for payload in payloads:
            answer, record = hook.evaluate(payload, context)
            if context.get("log"):
                store.log_append(Path(context["log"]), record)
            if answer.get("decision") != "auto_approve":
                return answer
        return answer
    except Exception as exc:
        return {"decision": "deny", "reason": f"CLI Subagents review failed closed ({type(exc).__name__})"}


def plugin_source(python: str, script: Path, context: Path | None, *, steps: int | None = None,
                  model: str | None = None, variant: str | None = None,
                  fallback: str = "opencode/deepseek-v4.1-flash-free", restricted: tuple[str, ...] = (),
                  permissions: list[dict[str, str]] | None = None, plugin_id: str = "cli-subagents.worker",
                  guarded: bool = True) -> str:
    """A native v2 Promise plugin (OpenCode v2.1.4 `packages/plugin/src/promise`).

    v2 configured local plugins must be directories; the caller writes this as
    server.js beside a package.json with type=module. No npm dependency is needed.

    Fail-closed order: the review hook (read/edit) and the model guard are
    registered before the prompt hook that moves the session off csa's
    unavailable gate model. A failed registration fails setup, OpenCode disposes
    the plugin's hooks, and the session stays on the gate model.

    Model guard: guarded (read/edit) jobs only reach the selected model. Free
    models with data-use terms run only when csa picked them (job model or
    [opencode] default_model), never from OpenCode or project settings.
    """

    settings = json.dumps({"python": python, "script": str(script), "context": str(context) if context else None,
                           "steps": steps, "model": model, "variant": variant, "fallback": fallback,
                           "restricted": list(restricted), "permissions": permissions or [],
                           "id": plugin_id, "guarded": guarded}, ensure_ascii=False)
    return '''// Generated per job by CLI Subagents for OpenCode v2.
import { execFileSync } from "node:child_process";
const settings = SETTINGS;
const parse = value => {
  const match = /^([^/#]+)\\/([^#]+)$/.exec(value);
  if (!match) throw new Error("CLI Subagents: OpenCode models are written provider/model.");
  return { providerID: match[1], id: match[2] };
};
const same = (left, right) => !!left && !!right && left.providerID === right.providerID && left.id === right.id;
const restricted = model => model.providerID === "opencode" &&
  settings.restricted.some(item => item.endsWith("-") ? model.id.startsWith(item) : model.id === item);
export default {
  id: settings.id,
  async setup(ctx) {
    // A thrown Error rejects only this tool call (Tool.Error).
    if (settings.context) await ctx.tool.hook("execute.before", async event => {
      let answer;
      try {
        const raw = execFileSync(settings.python, [settings.script, settings.context], {
          input: JSON.stringify({ tool: event.tool, input: event.input }),
          encoding: "utf8", timeout: 10000, maxBuffer: 65536, windowsHide: true
        });
        answer = JSON.parse(raw);
      } catch (error) {
        throw new Error("CLI Subagents review failed closed; this tool call was denied.");
      }
      if (answer.decision !== "auto_approve")
        throw new Error(answer.reason || "CLI Subagents review denied this tool call.");
    });
    let selected;
    let main;
    // csa picked the model only when the job or csa's config named it.
    let compaction;
    const allowed = model => settings.guarded
      ? (same(model, selected) && (!restricted(model) || !!settings.model)) ||
        (same(model, compaction) && !restricted(model))
      : !restricted(model) || (!!settings.model && same(model, selected));
    await ctx.session.hook("model.request", async event => {
      if (allowed(event.model)) return;
      const name = `${event.model.providerID}/${event.model.id}`;
      throw new Error(restricted(event.model)
        ? `CLI Subagents blocked a request to ${name}: free models with data-use terms only run when the csa job names them (model=${name}).`
        : `CLI Subagents blocked a request to ${name}: read and edit jobs only use the job's model and the compaction agent's model.`);
    });
    // Agent and provider config are post-plugins; a prompt hook runs after their
    // activation, so native settings and denials keep their final precedence.
    await ctx.session.hook("prompt", async event => {
      if (!selected) {
        let copied = false;
        let buildModel;
        await ctx.agent.transform(editor => {
          // OpenCode's compaction agent may summarize long sessions on its own model.
          const summarizer = editor.get("compaction");
          if (summarizer && summarizer.model) compaction = { providerID: summarizer.model.providerID, id: summarizer.model.id };
          const build = editor.get("build");
          if (!build) return;
          buildModel = build.model;
          editor.update("csa-worker", item => {
            const id = item.id, name = item.name;
            Object.assign(item, structuredClone(build), { id, name, hidden: true, mode: "primary" });
            item.permissions = [...structuredClone(build.permissions), ...settings.permissions];
            if (settings.steps) item.steps = settings.steps;
            copied = true;
          });
        });
        if (!copied) throw new Error("CLI Subagents could not initialize the guarded worker agent.");
        let nativeDefault;
        await ctx.model.transform(editor => { nativeDefault = editor.default.get(); });
        // The user's model wins; csa's default only fills in when none is set.
        const base = settings.model ? parse(settings.model)
          : buildModel ? { providerID: buildModel.providerID, id: buildModel.id }
          : nativeDefault ? { providerID: nativeDefault.providerID, id: nativeDefault.modelID }
          : parse(settings.fallback);
        const variant = settings.variant || (!settings.model && buildModel && buildModel.variant) || undefined;
        selected = variant ? { ...base, variant } : base;
        main = event.sessionID;
      }
      // Full access leaves subagent sessions on whatever model OpenCode picks for them.
      if (!settings.guarded && event.sessionID !== main) return;
      // An explicit session model fails with "Model unavailable" or "Variant
      // unavailable" instead of OpenCode's silent fallback to its first free model.
      await ctx.session.switchModel({ sessionID: event.sessionID, model: selected });
    });
  }
};
'''.replace("SETTINGS", settings)
