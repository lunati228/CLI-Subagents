"""Summarise paid DeepSeek csa jobs (Claude Code stream-json): between-step text a relay would copy.

Usage: python -X utf8 -I paid_analyze.py <label> <job_id> [<job_id> ...] [--show]
"""
import json, re, sys, statistics
from pathlib import Path

MARKERS = re.compile(r"\b(Let me|Let's|I'll check|Actually|Wait\b|Hmm|I think|I need to|Okay\b|OK,|Now let me|So,? )", re.I)
INSIGHT = "★ Insight"
JOBS = Path.home() / ".cli-subagents" / "jobs"

label, ids = sys.argv[1], [a for a in sys.argv[2:] if a != "--show"]
show = "--show" in sys.argv
rows = []
for job in ids:
    texts, thinking, tools, model, outcome = [], 0, 0, None, None
    for raw in (JOBS / job / "events.jsonl").read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if ev.get("type") == "result":
            outcome = ev.get("subtype")
        if ev.get("type") != "assistant":
            continue
        msg = ev.get("message") or {}
        model = model or msg.get("model")
        for it in msg.get("content") or []:
            if it.get("type") == "text" and it.get("text", "").strip():
                texts.append(it["text"].strip())
            elif it.get("type") == "thinking":
                thinking += 1
            elif it.get("type") == "tool_use":
                tools += 1
    between = texts[:-1]
    lines = [l.strip() for t in between for l in t.splitlines() if l.strip()]
    rows.append((job, model, outcome, tools, len(between), sum(1 for t in between if MARKERS.search(t[:200])),
                 sum(len(MARKERS.findall(t)) for t in between), sum(map(len, between)),
                 len(lines) - len(set(lines)), sum(t.count(INSIGHT) for t in texts), thinking))
    if show:
        print(f"--- {job}")
        for t in between:
            print("  >>", t[:220].replace("\n", " / "))
print(f"== {label}")
print("   job                     outcome  tools msgs opens-TA markers chars  repeats insight thinking")
for r in rows:
    print(f"   {r[0]:23} {str(r[2])[:8]:8} {r[3]:5} {r[4]:4} {r[5]:8} {r[6]:7} {r[7]:6} {r[8]:7} {r[9]:7} {r[10]:7}")
msgs = sum(r[4] for r in rows)
print(f"   TOTAL msgs={msgs} opening-think-aloud={sum(r[5] for r in rows)} ({100*sum(r[5] for r in rows)/max(msgs,1):.0f}%) "
      f"markers={sum(r[6] for r in rows)} insight-boxes={sum(r[9] for r in rows)} median-chars={int(statistics.median(r[7] for r in rows))}")
