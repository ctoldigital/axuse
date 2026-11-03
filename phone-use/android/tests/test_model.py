"""Offline suite: model (roles/render/find), refs+diff, and the whole stack
against the simulated phone over real HTTP — the same wire protocol the app
speaks. Run: python -m unittest discover -s tests"""

from __future__ import annotations

import sys
import unittest

sys.path.insert(0, "..")

from axdroid.model import (F_CHECKABLE, F_CHECKED, F_CLICK, F_EDIT, F_VISIBLE,  # noqa: E402
                           Snapshot, find, infer_role, render)


def node(id, cls, *, text=None, desc=None, rid=None, b=None, f=F_VISIBLE | 64, value=None, d=None):
    return {"id": id, "d": d if d is not None else (0 if "/" not in id.split(":", 1)[1] else 1),
            "cls": cls, "text": text, "desc": desc, "rid": rid, "b": b, "f": f, "value": value,
            "sig": f"s{id}"}


class TestModel(unittest.TestCase):
    def test_roles(self):
        self.assertEqual(infer_role("android.widget.Button", F_VISIBLE), "button")
        self.assertEqual(infer_role("android.widget.ImageButton", F_VISIBLE), "image-button")
        self.assertEqual(infer_role("android.widget.Switch", F_VISIBLE | F_CHECKABLE), "switch")
        self.assertEqual(infer_role("android.widget.EditText", F_VISIBLE | F_EDIT), "textfield")
        self.assertEqual(infer_role("androidx.recyclerview.widget.RecyclerView", F_VISIBLE | 4), "list")
        self.assertEqual(infer_role("android.view.View", F_VISIBLE | F_CLICK), "cell")
        self.assertEqual(infer_role("android.view.View", F_VISIBLE), "group")

    def test_snapshot_links_parents_and_finds(self):
        snap = Snapshot({"state": 1, "changes": 5, "windows": [
            {"id": 24, "pkg": "com.android.settings", "active": True, "bounds": [0, 0, 1080, 2340]}],
            "nodes": [
                node("w24:", "android.widget.FrameLayout", b=[0, 0, 1080, 2340], d=0),
                node("w24:0", "android.widget.TextView", text="Settings", b=[56, 210, 700, 320], d=1),
                node("w24:1", "android.widget.EditText", text="bat", rid="s:id/search", d=1,
                     f=F_VISIBLE | F_EDIT | F_CLICK),
                node("w24:1/0", "android.widget.Switch", desc="Wi‑Fi", rid="s:id/sw", d=2,
                     f=F_VISIBLE | F_CLICK | F_CHECKABLE | F_CHECKED, value="1"),
            ]})
        self.assertEqual(snap.app, "com.android.settings")
        n = snap.nodes["w24:1/0"]
        self.assertEqual(n.parent, "w24:1")
        self.assertEqual(n.checked, True)
        self.assertEqual(n.name, "Wi‑Fi")
        hits = find(snap, text="settings")
        self.assertEqual(len(hits), 1)
        hits = find(snap, text="wi‑fi", role="switch")
        self.assertEqual(hits[0].id, "w24:1/0")

    def test_render_marked_refs_and_state(self):
        snap = Snapshot({"state": 1, "windows": [{"id": 24, "pkg": "p", "active": True, "bounds": [0, 0, 1080, 2340]}],
                         "nodes": [
                             node("w24:", "android.widget.FrameLayout", d=0),
                             node("w24:0", "android.widget.Switch", text="Wi‑Fi", d=1,
                                  f=F_VISIBLE | F_CLICK | F_CHECKABLE | F_CHECKED | 64),
                             node("w24:1", "android.widget.EditText", text="query", rid="s:id/q", d=1,
                                  f=F_VISIBLE | F_EDIT | 64),
                         ]})
        snap.nodes["w24:0"].ref = "e1"
        snap.nodes["w24:1"].ref = "e2"
        text = render(snap, screen=(1080, 2340))
        self.assertIn('switch "Wi‑Fi" on [ref=e1]', text)
        self.assertIn("textfield", text)


if __name__ == "__main__":
    unittest.main()
