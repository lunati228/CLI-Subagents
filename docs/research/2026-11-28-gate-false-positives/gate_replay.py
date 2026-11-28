"""Replay every recorded tool call of csa's review gate through the fixed rules of one csa source tree.

Usage: python -I gate_replay.py <src dir> <out.jsonl>
Reads ~/.cli-subagents/jobs/*/{spec.json,events.jsonl,review-context.json}. Only the fixed rules run
(no reviewer calls): the verdict is approve, deny or review (= would go to the reviewer chain).
Paths are never opened beyond what the gate itself does.
"""
import json
import sys
from pathlib import Path

src, out = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(src))
from cli_subagents.agy_review import context as gate_context  # noqa: E402
from cli_subagents.agy_review import rules  # noqa: E402
from cli_subagents import opencode_review  # noqa: E402

jobs = Path.home() / ".cli-subagents" / "jobs"


def agy_calls(events: Path):
    """Last seen parameters per step (the stream repeats a step as it fills in)."""
    steps: dict[int, dict] = {}
    for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        upd = e.get("step_update") or {}
        info = upd.get("tool_info") or {}
        if upd.get("step_type") == "tool" and info.get("name"):
            steps[upd.get("step_index")] = {"name": info["name"], "args": info.get("parameters") or {},
                                            "conversation": upd.get("conversation_id")}
    return sorted(steps.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))


def opencode_calls(events: Path):
    seen = {}
    for line in events.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        part = e.get("part") or {}
        if part.get("type") == "tool" and part.get("tool"):
            seen[part.get("id")] = {"tool": part["tool"], "input": (part.get("state") or {}).get("input") or {}}
    return list(seen.items())


with out.open("w", encoding="utf-8") as fh:
    for job in sorted(jobs.iterdir()):
        ctx_file, events, spec = job / "review-context.json", job / "events.jsonl", job / "spec.json"
        if not (ctx_file.is_file() and events.is_file() and spec.is_file()):
            continue
        runtime = json.loads(spec.read_text(encoding="utf-8")).get("runtime")
        try:
            ctx = gate_context.load_context(ctx_file)
        except Exception as exc:  # noqa: BLE001
            fh.write(json.dumps({"job": job.name, "error": f"context: {exc}"}) + "\n")
            continue
        if runtime == "antigravity":
            for step, call in agy_calls(events):
                conv = call.pop("conversation", None)
                payload = {"toolCall": call, "stepIdx": step}
                if conv and ctx.get("agent_home"):
                    payload["artifactDirectoryPath"] = str(Path(ctx["agent_home"]) / ".gemini" / "antigravity-cli" / "brain" / conv)
                try:
                    v = rules.decide(call, payload, ctx)
                    rec = {"action": v.action, "reason": v.reason}
                except Exception as exc:  # noqa: BLE001
                    rec = {"action": "error", "reason": repr(exc)[:200]}
                fh.write(json.dumps({"job": job.name, "rt": runtime, "step": step, "tool": call["name"],
                                     "summary": rules.summarize(call, 300), **rec}) + "\n")
        elif runtime == "opencode":
            for step, ev in opencode_calls(events):
                try:
                    payloads = opencode_review.translate(ev, ctx)
                    verdicts = [opencode_review.decide(p["toolCall"], p, ctx) for p in payloads]
                    worst = next((v for v in verdicts if v.action == "deny"), None) or \
                        next((v for v in verdicts if v.action == "review"), None) or verdicts[0]
                    rec = {"action": worst.action, "reason": worst.reason}
                except opencode_review.Denied as exc:
                    rec = {"action": "deny", "reason": str(exc)[:200]}
                except Exception as exc:  # noqa: BLE001
                    rec = {"action": "error", "reason": repr(exc)[:200]}
                summary = json.dumps(ev["input"], ensure_ascii=False)[:300]
                fh.write(json.dumps({"job": job.name, "rt": runtime, "step": step, "tool": ev["tool"],
                                     "summary": summary, **rec}) + "\n")
print("written", out)
