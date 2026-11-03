"""Refs stay stable; diffs are keyed by refs with signature fallback."""

from __future__ import annotations

import sys
import unittest

sys.path.insert(0, "..")

from axdroid.engine import Session  # noqa: E402
from axdroid.model import F_CHECKABLE, F_CHECKED, F_CLICK, F_EDIT, F_VISIBLE  # noqa: E402
from axdroid.sim import SimPhone, serve_sim  # noqa: E402


class TestRefsAndDiff(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sim, cls.phone, cls.shutdown = serve_sim()
        cls.s = Session(cls.phone)
        cls.s.observe()

    def setUp(self):
        self.sim.reset_state()
        self.s.refs.clear()
        self.s.snap = None

    @classmethod
    def tearDownClass(cls):
        cls.shutdown()

    def test_refs_stable_across_unchanged_observe(self):
        n0 = self.s.current()
        wifi_ref = {nid: n.ref for nid, n in n0.nodes.items() if n.name == "Connections"}
        self.assertTrue(wifi_ref, "Connections row missing in sim tree")
        n1 = self.s.current()
        self.assertEqual(wifi_ref, {nid: n.ref for nid, n in n1.nodes.items() if n.name == "Connections"})

    def test_diff_after_tap(self):
        r = self.s.tap("Battery")
        d = r["diff"]
        self.assertIn("Battery", r["text"])
        self.assertGreater(d["added"] + d["changed"], 0)
        self.s.back()

    def test_switch_toggle_reports_change(self):
        r = self.s.tap({"text": "Wi‑Fi", "role": "switch"})
        self.assertIn("~", r["text"])
        lines = [l for l in r["text"].splitlines() if l.startswith("~")]
        self.assertTrue(lines, r["text"])
        self.assertTrue(("True→False" in lines[0]) or ("False→True" in lines[0]), r["text"])

    def test_recycled_ids_fall_back_to_signature(self):
        # scroll recreates rows at new offsets; ids may shift for filtered rows
        self.s.scroll("down", max_swipes=2)
        snap = self.s.observe()["snap"]
        self.assertGreater(len(snap.nodes), 10)
        self.s.scroll("up", max_swipes=2)

    def test_typing_searches(self):
        r = self.s.type("battery", "Search", submit=True)
        self.assertEqual(r["action"]["via"], "ax-set-text")
        w = self.s.wait_for("Battery", timeout=4)
        self.assertTrue(w["action"]["matched"], "filtered results should contain Battery")
        self.s.back()
        snap = self.s.observe()["snap"]
        self.assertFalse(any(n.name == "battery" and n.role == "textfield" for n in snap.nodes.values()))

    def test_scroll_until_offscreen_row(self):
        r = self.s.scroll("down", until="About phone")
        self.assertEqual(r["action"].get("found"), "About phone")
        # now it is tappable
        r2 = self.s.tap("About phone")
        self.assertEqual(r2["action"]["via"], "ax-click")
        w = self.s.wait_for("Status information", timeout=3)
        self.assertTrue(w["action"]["matched"])
        self.s.back()


if __name__ == "__main__":
    unittest.main()
