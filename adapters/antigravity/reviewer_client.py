"""One-shot, tool-less Gemini reviewer for Antigravity tool proposals."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from agy_pty import clean_terminal_output, run_pty
from hook_command import hook_command, python_command_prefix

REVIEWER_MODEL = "gemini-4-hydrogen-high"
REVIEWER_EFFORT = "high"
MAX_OBJECTIVE_CHARS = 4_000
MAX_ACTION_CHARS = 12_000
POLICY_PATH = Path(__file__).with_name("review_policy.md")


def parse_reviewer_output(output: str) -> dict[str, str]:
    text = output.strip()
    if text.startswith("[pty unavailable:"):
        _diagnostic, _separator, text = text.partition("\n")
        text = text.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("reviewer did not return exact JSON") from exc
    expected = {"decision", "risk_level", "user_authorization", "reason"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("reviewer JSON has the wrong fields")
    decision = value.get("decision")
    risk_level = value.get("risk_level")
    authorization = value.get("user_authorization")
    reason = value.get("reason")
    if decision not in {"allow", "deny"} or not isinstance(reason, str) or not reason.strip():
        raise ValueError("invalid reviewer decision schema")
    if risk_level not in {"low", "medium", "high", "critical"}:
        raise ValueError("invalid risk level")
    if authorization not in {"low", "medium", "high", "unknown"}:
        raise ValueError("invalid authorization level")
    if decision == "allow" and risk_level == "critical":
        raise ValueError("critical actions cannot be allowed")
    if decision == "allow" and risk_level == "high" and authorization not in {"medium", "high"}:
        raise ValueError("high-risk action lacks sufficient authorization")
    return {
        "decision": decision,
        "risk_level": risk_level,
        "user_authorization": authorization,
        "reason": reason.strip()[:500],
    }


def _review_prompt(objective: str, tool_call: dict[str, Any], project_root: Path) -> str:
    action = json.dumps(tool_call, ensure_ascii=False, sort_keys=True)
    policy = POLICY_PATH.read_text(encoding="utf-8")
    return f"""You are a security approval reviewer. You cannot execute tools.
Evaluate one proposed tool call from another coding agent.

Return exactly one minified JSON object with only these fields:
{{"decision":"allow|deny","risk_level":"low|medium|high|critical","user_authorization":"low|medium|high|unknown","reason":"brief concrete reason"}}
Keep `reason` category-level. Do not quote the command, paths, credentials, or
other proposed-action contents.

<policy>
{policy}
</policy>

Active project: {project_root}
<untrusted_objective>{objective[:MAX_OBJECTIVE_CHARS]}</untrusted_objective>
<untrusted_proposed_action>{action[:MAX_ACTION_CHARS]}</untrusted_proposed_action>
"""


def _write_deny_hook(workspace: Path, gate_script: Path) -> None:
    hooks_dir = workspace / ".agents"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    command = hook_command(
        [*python_command_prefix(), str(gate_script), "deny-reviewer-tools"]
    )
    data = {
        "codex-reviewer-no-tools": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": command, "timeout": 5}],
                }
            ]
        }
    }
    (hooks_dir / "hooks.json").write_text(json.dumps(data), encoding="utf-8")


def review_action(
    *, agy_cmd: str, objective: str, tool_call: dict[str, Any], project_root: Path
) -> dict[str, str]:
    gate_script = Path(__file__).with_name("review_gate.py").resolve()
    # Keep the tool-less reviewer under the already trusted project tree so
    # Antigravity loads its deny-all workspace hook in headless mode.
    with tempfile.TemporaryDirectory(
        prefix=".codex-agy-reviewer-", dir=str(project_root.resolve())
    ) as temp:
        workspace = Path(temp).resolve()
        _write_deny_hook(workspace, gate_script)
        prompt = _review_prompt(objective, tool_call, project_root)
        argv = [
            agy_cmd,
            "--mode=plan",
            "--model",
            REVIEWER_MODEL,
            "--effort",
            REVIEWER_EFFORT,
            f"--print={prompt}",
            "--print-timeout=60s",
        ]
        exit_status, raw = run_pty(argv, str(workspace), 90)
        if exit_status not in (0, None):
            raise RuntimeError(f"reviewer exited with status {exit_status}")
        return parse_reviewer_output(clean_terminal_output(raw))
