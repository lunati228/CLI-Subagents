"""Compare what csa delivered to a relay with what the relay wrote, and total its tokens."""
import json, sys

def blocks(out):
    lines = out.splitlines()
    if "===== MESSAGES =====" not in lines:
        return None
    s, e = lines.index("===== MESSAGES ====="), lines.index("===== END MESSAGES =====")
    return "\n".join(l[2:] for l in lines[s + 1:e])

seen, tot, events = set(), {}, []
for line in open(sys.argv[1], encoding="utf-8"):
    e = json.loads(line)
    if e.get("type") == "assistant":
        m = e["message"]
        for b in m.get("content", []):
            if b.get("type") == "text":
                events.append(("text", b["text"]))
            elif b.get("type") == "thinking":
                events.append(("think", len(b.get("thinking", ""))))
            elif b.get("type") == "tool_use":
                events.append(("tool", b["name"]))
        if m.get("id") not in seen:
            seen.add(m.get("id"))
            for k in ("cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
                tot[k] = tot.get(k, 0) + (m.get("usage", {}).get(k) or 0)
            events.append(("effort", e.get("effort")))
    elif e.get("type") == "user" and isinstance(e["message"].get("content"), list):
        for b in e["message"]["content"]:
            if b.get("type") == "tool_result":
                out = b.get("content")
                out = out if isinstance(out, str) else " ".join(x.get("text", "") for x in out if isinstance(x, dict))
                events.append(("block", blocks(out)))

pending, exact, other, skipped, efforts = None, 0, [], 0, set()
for kind, value in events:
    if kind == "effort":
        efforts.add(value)
    elif kind == "block":
        if pending is not None:
            skipped += 1
        pending = value
    elif kind == "text" and pending is not None:
        if value.strip() == pending.strip():
            exact += 1
        else:
            other.append((pending[:80], value[:160]))
        pending = None
    elif kind == "tool" and pending is not None and value == "Bash":
        skipped += 1
        pending = None
delivered = sum(1 for k, v in events if k == "block" and v is not None)
print(f"effort {efforts} requests {len(seen)} {tot}")
print(f"blocks delivered {delivered}, copied exactly {exact}, skipped {skipped}, written differently {len(other)}")
for block, text in other:
    print("  BLOCK:", repr(block), "\n  WROTE:", repr(text))
