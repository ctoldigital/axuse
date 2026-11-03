"""Probe the current phone screen: windows, first rows, and what a tap hits."""
import json
import sys

sys.path.insert(0, ".")
from axdroid.client import Phone, PhoneError
from axdroid.engine import Session

s = Session(Phone.via_adb())
obs = s.observe()
snap = obs["snap"]
print("app:", snap.app)
for w in snap.windows:
    print("win", w)
rows = [n for n in snap.nodes.values() if n.visible and n.actionable and n.name][:10]
for n in rows:
    print(f"{n.ref:5} {n.role:12} {n.name[:40]!r:44} clickable={n.clickable} bounds={n.bounds} rid={n.rid}")
# editable?
eds = [n for n in snap.nodes.values() if n.editable]
print("editable:", [(n.ref, n.rid, n.cls, n.bounds) for n in eds][:5])
fields = [n for n in snap.nodes.values() if n.visible and (n.role == "textfield")]
print("textfield-role:", [(n.ref, n.rid, n.cls) for n in fields][:5])
# try a tap on the first row and print the raw error facts
if rows:
    t = rows[0]
    try:
        r = s.tap(t.ref)
        print("tap ok via", r["action"].get("via"), "diff:", (r["text"] or "")[:200])
        s.back()
    except PhoneError as e:
        print("tap failed:", e.code, json.dumps(e.facts, ensure_ascii=False)[:400])
