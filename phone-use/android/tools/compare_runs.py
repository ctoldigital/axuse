"""Compare AndroidWorld runs: success, input tokens and wall time.

usage: compare_runs.py <label=run_dir> [<label=run_dir> ...] [--tasks a,b] [--md out.md]
Each run's results.json is read as a SINGLE run (no merging across retries).
"""
import json
import os
import statistics
import sys


def q(vals, p):
    v = sorted(vals)
    return v[int(round(p * (len(v) - 1)))] if v else 0


def load(d):
    r = json.load(open(os.path.join(d, "results.json"), encoding="utf-8"))
    return r["tasks"], r.get("agent", {})


def main(argv):
    runs, tasks, md = [], None, None
    i = 0
    while i < len(argv):
        if argv[i] == "--tasks":
            tasks = set(argv[i + 1].split(","))
            i += 2
            continue
        if argv[i] == "--md":
            md = argv[i + 1]
            i += 2
            continue
        label, d = argv[i].split("=", 1)
        runs.append((label, *load(d)))
        i += 1
    common = set.intersection(*(set(t) for _, t, _ in runs))
    if tasks:
        common &= tasks
    out = ["| run | agent | tasks | passed | success | input tok median | p90 | mean | wall s median | p90 | total h |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    for label, t, ag in runs:
        rows = [t[n] for n in common]
        ok = sum(1 for r in rows if r.get("ok"))
        tok = [r.get("tokens", {}).get("input", 0) for r in rows]
        wall = [r.get("wall_s", 0) or 0 for r in rows]
        agent = f"{ag.get('model', '?')}/{ag.get('effort', '?')}{' lean' if ag.get('lean') else ''}"
        out.append(f"| {label} | {agent} | {len(rows)} | {ok} | {100 * ok / max(1, len(rows)):.1f}% "
                   f"| {int(statistics.median(tok)):,} | {q(tok, .9):,} | {int(statistics.mean(tok)):,} "
                   f"| {statistics.median(wall):.0f} | {q(wall, .9):.0f} | {sum(wall) / 3600:.2f} |")
    out += ["", "| task | " + " | ".join(f"{l} ok / tok / s" for l, _, _ in runs) + " |",
            "|---|" + "---|" * len(runs)]
    for n in sorted(common):
        cells = []
        for _, t, _ in runs:
            r = t[n]
            cells.append(f"{'✓' if r.get('ok') else '✗'} / {r.get('tokens', {}).get('input', 0) // 1000}k "
                         f"/ {r.get('wall_s', 0) or 0:.0f}")
        out.append(f"| {n} | " + " | ".join(cells) + " |")
    text = "\n".join(out)
    print(text)
    if md:
        open(md, "w", encoding="utf-8").write(text + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
