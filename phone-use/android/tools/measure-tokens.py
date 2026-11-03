"""Measure token economy on the real phone: observe output size across screens,
compact (default) vs verbose (chrome + no compaction), plus action-diff size.

Tokens are estimated as chars/4 (GPT-family heuristic; documented in the
report). Run against adb: python tools/measure-tokens.py [--screens N]
"""

from __future__ import annotations

import json
import statistics
import sys

sys.path.insert(0, ".")

from axdroid import model
from axdroid.client import Phone, PhoneError
from axdroid.engine import Session

SCREENS = [
    ("launcher", None),
    ("settings root", "Settings"),
    ("settings subpage (About phone)", None),   # navigated to below
    ("messages list", "Messages"),
    ("camera", "Camera"),
]


def toks(s: str) -> int:
    return (len(s) + 3) // 4


def main():
    s = Session(Phone.via_adb())
    rows = []
    action_diffs = []

    def sample(name):
        obs = s.observe()
        snap = obs["snap"]
        compact = model.render(snap, screen=snap.screen, prefix_app=f"app: {snap.app}")
        verbose = model.render(snap, screen=snap.screen, prefix_app=f"app: {snap.app}",
                               chrome=True, compact_lists=False)
        rows.append({
            "screen": name, "app": snap.app, "nodes": len(snap.nodes),
            "compact_chars": len(compact), "compact_tokens": toks(compact),
            "verbose_chars": len(verbose), "verbose_tokens": toks(verbose),
            "observe_ms": obs["ms"],
        })
        return snap

    s.button("home")
    sample("launcher")
    s.launch("Settings", fresh=True)
    sample("settings root")
    try:
        s.scroll("down", until="About phone", max_swipes=8)
        s.tap("About phone")
        sample("settings subpage (About phone)")
        s.button("back")
    except PhoneError as e:
        rows.append({"screen": "settings subpage", "error": e.code})
    for app in ("Messages", "Camera"):
        try:
            s.launch(app)
            sample(f"{app.lower()} list")
        except PhoneError as e:
            rows.append({"screen": app, "error": e.code})
    s.button("home")

    # action diff sizes: navigate Settings -> Connections -> back
    try:
        s.launch("Settings", fresh=True)
        r1 = s.tap("Connections")
        action_diffs.append({"action": "tap Connections", "tokens": toks(r1["text"]),
                             "ms": r1["ms"], "via": r1["action"].get("via")})
        r2 = s.button("back")
        action_diffs.append({"action": "back", "tokens": toks(r2["text"]), "ms": r2["ms"],
                             "via": "global"})
        obs = s.observe()
        action_diffs.append({"action": "observe (after-action alternative: full re-dump)",
                             "tokens": toks(obs["text"]), "ms": obs["ms"], "via": "compact-render"})
    except PhoneError as e:
        action_diffs.append({"error": e.code})

    ok_rows = [r for r in rows if "error" not in r]
    rep = {
        "device": s.info.get("device") if s.info else None,
        "method": "tokens = ceil(chars/4)",
        "screens": rows,
        "screen_tokens_compact": [r.get("compact_tokens") for r in ok_rows],
        "screen_tokens_verbose": [r.get("verbose_tokens") for r in ok_rows],
        "median_compact": statistics.median([r["compact_tokens"] for r in ok_rows]),
        "median_verbose": statistics.median([r["verbose_tokens"] for r in ok_rows]),
        "reduction_pct": round(100 * (1 - statistics.median([r["compact_tokens"] for r in ok_rows])
                                      / statistics.median([r["verbose_tokens"] for r in ok_rows])), 1),
        "action_diffs": action_diffs,
        "median_observe_ms": statistics.median([r["observe_ms"] for r in ok_rows]),
    }
    out = sys.argv[1] if len(sys.argv) > 1 else "tokens-report.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rep, f, indent=1, ensure_ascii=False)
    print(json.dumps({k: v for k, v in rep.items() if k not in ("screens",)}, indent=1)[:1600])
    for r in rows:
        if "error" in r:
            print(r)
        else:
            print(f"{r['screen']:38} nodes={r['nodes']:4}  compact={r['compact_tokens']:5}t  "
                  f"verbose={r['verbose_tokens']:5}t  observe={r['observe_ms']:.0f}ms")
    for a in action_diffs:
        print("action:", a)
    print("->", out)


if __name__ == "__main__":
    main()
