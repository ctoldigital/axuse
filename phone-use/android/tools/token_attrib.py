"""Token attribution for codex-backend AndroidWorld logs.

usage: token_attrib.py <run_dir> [<run_dir> ...]
Per task: tool calls by name, mean output chars by tool, input tokens,
and the share explained by tool output re-sent on every later request.
"""
import collections
import glob
import json
import os
import statistics
import sys


def load(path):
    calls = []
    usage = None
    msgs = 0
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "item.completed":
            it = ev["item"]
            if it.get("type") == "mcp_tool_call":
                r = it.get("result") or {}
                txt = "".join(c.get("text", "") for c in (r.get("content") or []) if isinstance(c, dict))
                if not txt and it.get("error"):
                    txt = json.dumps(it.get("error"))
                calls.append((it.get("tool"), len(txt), len(json.dumps(it.get("arguments") or {}))))
            elif it.get("type") in ("agent_message", "reasoning"):
                msgs += 1
        elif ev.get("type") == "turn.completed":
            usage = ev.get("usage")
    return calls, usage, msgs


def main(dirs):
    rows = []
    by_tool = collections.defaultdict(list)
    for d in dirs:
        res = {}
        rp = os.path.join(d, "results.json")
        if os.path.exists(rp):
            res = json.load(open(rp, encoding="utf-8"))["tasks"]
        for p in glob.glob(os.path.join(d, "logs", "*.jsonl")):
            name = os.path.basename(p)[:-6]
            calls, usage, msgs = load(p)
            if not usage:
                continue
            n = len(calls)
            for t, c, _ in calls:
                by_tool[t].append(c)
            # each tool output is re-read by every later request
            resent = sum(c * (n - i) for i, (_, c, _) in enumerate(calls)) / 4
            rows.append(dict(task=name, ok=res.get(name, {}).get("ok"), calls=n,
                             inp=usage["input_tokens"], resent=int(resent),
                             base=int((usage["input_tokens"] - resent) / max(1, n + 1)),
                             wall=res.get(name, {}).get("wall_s")))
    rows.sort(key=lambda r: -r["inp"])
    print(f"{'task':45} ok  calls   input    resent%  base/req  wall")
    for r in rows[:25]:
        print(f"{r['task'][:45]:45} {str(r['ok'])[:1]} {r['calls']:5} {r['inp']:9} "
              f"{100 * r['resent'] / max(1, r['inp']):6.0f}%  {r['base']:7}  {r['wall']}")
    print("\nmedian calls", statistics.median(r["calls"] for r in rows),
          "median input", statistics.median(r["inp"] for r in rows),
          "median base/req", statistics.median(r["base"] for r in rows))
    print("\ntool        n   mean_chars  p90_chars")
    for t, v in sorted(by_tool.items(), key=lambda kv: -sum(kv[1])):
        v = sorted(v)
        print(f"{t:14} {len(v):6} {int(statistics.mean(v)):8} {v[int(.9 * (len(v) - 1))]:8}")


if __name__ == "__main__":
    main(sys.argv[1:])
