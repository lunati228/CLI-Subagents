"""Summarise OpenCode json runs: visible text parts (what a relay copies) vs reasoning parts.

Usage: python -X utf8 -I ds_analyze.py <runs-dir> [label-prefix ...] [--show]
"""
import json, re, sys, statistics
from pathlib import Path

MARKERS = re.compile(r"\b(Let me|Let's|I'll check|Actually|Wait\b|Hmm|I think|I need to|Okay\b|OK,|Now let me|So,? )", re.I)
ANYWHERE = MARKERS


def parts(path):
    texts, reasoning, tools = {}, {}, 0
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        part = ev.get("part") if isinstance(ev, dict) else None
        if not isinstance(part, dict):
            continue
        pid = part.get("id") or id(part)
        kind = part.get("type")
        if kind == "text":
            texts[pid] = str(part.get("text") or "")
        elif kind == "reasoning":
            reasoning[pid] = str(part.get("text") or "")
        elif kind in ("tool", "tool_use"):
            tools += 1
    return [t.strip() for t in texts.values() if t.strip()], [r for r in reasoning.values() if r.strip()], tools


args = [a for a in sys.argv[2:] if a != "--show"]
show = "--show" in sys.argv
root = Path(sys.argv[1])
for prefix in args or sorted({p.stem.rsplit("-", 1)[0] for p in root.glob("*.jsonl")}):
    rows = []
    for f in sorted(root.glob(f"{prefix}-*.jsonl")):
        texts, reasoning, tools = parts(f)
        if not texts and not tools:
            continue
        between = texts[:-1]  # the last text is the final answer, which the relay never copies as a message
        marked = sum(1 for t in between if MARKERS.search(t[:200]))
        anywhere = sum(len(ANYWHERE.findall(t)) for t in between)
        lines = [l.strip() for t in between for l in t.splitlines() if l.strip()]
        repeats = len(lines) - len(set(lines))
        rows.append((f.name, tools, len(between), marked, anywhere, sum(map(len, between)), repeats, len(reasoning)))
        if show:
            print(f"--- {f.name}")
            for t in between:
                print("  >>", t[:220].replace("\n", " / "))
    if not rows:
        continue
    print(f"== {prefix}: {len(rows)} runs")
    print("   run                tools msgs opens-think-aloud markers-anywhere chars repeated-lines reasoning-parts")
    for r in rows:
        print(f"   {r[0]:18} {r[1]:5} {r[2]:4} {r[3]:6} {r[4]:8} {r[5]:7} {r[6]:5} {r[7]:6}")
    msgs = sum(r[2] for r in rows)
    print(f"   TOTAL msgs={msgs} opening-think-aloud={sum(r[3] for r in rows)} ({100*sum(r[3] for r in rows)/max(msgs,1):.0f}%) "
          f"markers-anywhere={sum(r[4] for r in rows)} median-chars/run={int(statistics.median(r[5] for r in rows))}")
