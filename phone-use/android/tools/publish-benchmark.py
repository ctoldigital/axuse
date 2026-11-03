"""Render BENCHMARK.md from tasks + tokens reports (UTF-8 safe)."""
import json
import statistics
import sys

sys.path.insert(0, ".")
from axdroid.tasks import publish_tasks

tasks_rep = json.load(open(sys.argv[1], encoding="utf-8"))
tok = json.load(open(sys.argv[2], encoding="utf-8")) if len(sys.argv) > 2 else None

body = publish_tasks([tasks_rep])
if tok:
    med_c = statistics.median([r["compact_tokens"] for r in tok["screens"] if "compact_tokens" in r])
    med_v = statistics.median([r["verbose_tokens"] for r in tok["screens"] if "verbose_tokens" in r])
    body += "\n## Observe token economy (same screens, compact vs verbose render)\n\n"
    body += "| screen | nodes | compact | verbose | observe ms |\n|---|---|---|---|---|\n"
    for r in tok["screens"]:
        if "compact_tokens" in r:
            body += (f"| {r['screen']} | {r['nodes']} | {r['compact_tokens']}t | "
                     f"{r['verbose_tokens']}t | {r['observe_ms']:.0f} |\n")
    body += (f"\nMedian observe: **{med_c:.0f} tokens** compact vs {med_v:.0f} verbose — "
             f"a {100*(1-med_c/med_v):.0f}% cut. Method: {tok['method']}.\n")
    body += "\n### Action outputs vs full re-dump (same navigation)\n\n| action | output tokens | ms |\n|---|---|---|\n"
    for a in tok["action_diffs"]:
        if "tokens" in a:
            body += f"| {a['action']} | {a['tokens']}t | {a.get('ms', 0):.0f} |\n"
with open(sys.argv[-1], "w", encoding="utf-8") as f:
    f.write(body)
print("wrote", sys.argv[-1])
