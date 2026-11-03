"""Render eval-agent.json (+ optional extra runs) → EVAL.md (UTF-8 safe).
Usage: python tools/publish-eval.py eval-agent.json [eval-rest.json …] [EVAL.md]"""
import json
import sys

files = [a for a in sys.argv[1:] if a.endswith(".json")]
out = sys.argv[-1] if sys.argv[-1].endswith(".md") else "EVAL.md"
reps = [json.load(open(f, encoding="utf-8")) for f in files]

body = []
body.append("# axdroid — agent-in-the-loop eval (measured)")
body.append("")
body.append(f"Driver: **{reps[0]['driver']}** — a real agent CLI operating the phone")
seen = {}
for rep in reps:
    for t in rep["tasks"]:
        seen[t["task"]] = t
body.append("Runs: " + " + ".join(f"{rep['passed']}/{rep['total']}" for rep in reps)
            + f" · {len(seen)} distinct tasks")
body.append("")
body.append("Every task ran a fresh agent session against the phone's **on-device MCP endpoint** "
            "(USB → `http://127.0.0.1:8360/mcp`); outcomes are verified from the device itself "
            "(state read from the tree, action sequences from the phone's journal, or the reply "
            "where labelled) — never from the model's self-report alone.")
body.append("")
rows = list(seen.values())
passed = sum(1 for t in rows if t["ok"])
wall = round(sum(t["wall_s"] for t in rows), 1)
llm = {k: sum(t["llm_tokens"][k] for t in rows) for k in ("input", "output", "reasoning")}
cost = round(sum(t["llm_cost_usd"] for t in rows), 4)
calls = sum(t["tool_calls"] for t in rows)
outtok = sum(t["tool_output_tokens"] for t in rows)
body.append(f"**{passed}/{len(rows)} tasks passed** · {wall} s wall · "
            f"LLM {llm['input'] + llm['output'] + llm['reasoning']} tokens (${cost}) · "
            f"{calls} phone tool calls ({outtok} tokens of tool output)")
body.append("")
body.append("| task | result | wall s | LLM tok | $ | tool calls | tool-out tok | evidence | note |")
body.append("|---|---|---|---|---|---|---|---|---|")
for t in rows:
    res = "PASS" if t["ok"] else "FAIL"
    tk = t["llm_tokens"]
    body.append(f"| {t['task']} | {res} | {t['wall_s']} | "
                f"{tk['input'] + tk['output'] + tk['reasoning']} | {t['llm_cost_usd']} | "
                f"{t['tool_calls']} | {t['tool_output_tokens']} | {t['evidence']} | "
                f"{(t['note'] or '')[:60]} |")
body.append("")

with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(body))
print("wrote", out)
