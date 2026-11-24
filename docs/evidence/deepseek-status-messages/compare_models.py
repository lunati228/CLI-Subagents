"""Compare the visible between-step text (what a relay copies) by worker model across all csa jobs.

Usage: python -X utf8 -I compare_models.py <jobs-dir>
Reads Claude Code stream-json (assistant text blocks) and OpenCode json (part type text).
"""
import json, re, sys, statistics
from collections import defaultdict
from pathlib import Path

MARKERS = re.compile(r"\b(Let me|I'll check|Actually|Wait[,.]|Hmm|I think|I need to|Okay[,.]|Now let me|Let's)\b", re.I)


def texts_of(path: Path):
    model, out = None, []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "assistant":
            msg = ev.get("message") or {}
            model = model or msg.get("model")
            for it in msg.get("content") or []:
                if isinstance(it, dict) and it.get("type") == "text" and it.get("text", "").strip():
                    out.append(it["text"].strip())
        part = ev.get("part")
        if isinstance(part, dict) and part.get("type") == "text" and str(part.get("text") or "").strip():
            out.append(str(part["text"]).strip())
    return model, out


def degenerate(t):
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    if len(lines) < 20:
        return False
    return len(set(lines)) / len(lines) < 0.2


stats = defaultdict(lambda: {"jobs": 0, "blocks": [], "marker": 0, "degenerate": 0, "max": 0, "per_job_chars": []})
for job in Path(sys.argv[1]).iterdir():
    ev = job / "events.jsonl"
    spec = job / "spec.json"
    if not ev.exists():
        continue
    model, texts = texts_of(ev)
    try:
        sp = json.loads(spec.read_text(encoding="utf-8"))
    except Exception:
        sp = {}
    model = model or sp.get("model") or "?"
    runtime = sp.get("runtime", "?")
    if not texts:
        continue
    key = f"{runtime}:{model}"
    s = stats[key]
    s["jobs"] += 1
    s["blocks"] += [len(t) for t in texts]
    s["marker"] += sum(1 for t in texts if MARKERS.search(t[:200]))
    s["degenerate"] += sum(1 for t in texts if degenerate(t))
    s["max"] = max(s["max"], max(len(t) for t in texts))
    s["per_job_chars"].append(sum(len(t) for t in texts))

print(f"{'runtime:model':58} jobs blocks  med  p90   max    think-aloud%  degenerate  chars/job(med)")
for key, s in sorted(stats.items(), key=lambda kv: -kv[1]["jobs"]):
    b = sorted(s["blocks"])
    if s["jobs"] < 2:
        continue
    p90 = b[int(len(b) * 0.9)]
    print(f"{key[:58]:58} {s['jobs']:4} {len(b):6} {int(statistics.median(b)):4} {p90:5} {s['max']:7}   {100*s['marker']/len(b):5.1f}%      {s['degenerate']:4}      {int(statistics.median(s['per_job_chars']))}")
