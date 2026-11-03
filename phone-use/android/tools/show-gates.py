import json
import sys

r = json.load(open(sys.argv[1], encoding="utf-8"))
print(r["summary"], "ok=", r["ok"])
for g in r["gates"]:
    res = "PASS" if g["ok"] else ("skip" if g["ok"] is None else "FAIL")
    note = str(g.get("note", ""))[:90]
    err = g.get("error", {}).get("code", "") if g.get("error") else ""
    extra = {k: g[k] for k in ("nodes", "clickable", "via", "tapped", "changes_after_home", "target", "refused_as") if k in g}
    print(f"{g['name']}: {res} {g.get('ms', '')}ms {note} {err} {extra}")
    for a in g.get("apps", [])[:8]:
        print(f"    {a.get('quality','?'):7} {a['name'][:28]:28} {a['app'][:38]:38} actionable={a.get('actionable','?')} fw={','.join(a.get('framework', [])) or '—'} {a.get('error','')}")
