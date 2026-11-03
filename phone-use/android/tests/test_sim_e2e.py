"""End-to-end against the simulated phone: guard refusals, blind taps,
custody stop/resume, wait, plans (phone_run semantics).

Off-screen rows are NOT in an Android tree (RecyclerView) — these tests
scroll first, exactly like an agent must."""

from __future__ import annotations

import sys
import unittest

sys.path.insert(0, "..")

sys.path.insert(0, "..")

from axdroid.client import Phone, PhoneError  # noqa: E402
from axdroid.engine import Session  # noqa: E402
from axdroid.sim import serve_sim  # noqa: E402


class TestSimE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sim, cls.phone, cls.shutdown = serve_sim()
        cls.s = Session(cls.phone)

    def setUp(self):
        self.sim.reset_state()
        self.s.refs.clear()
        self.s.snap = None

    @classmethod
    def tearDownClass(cls):
        cls.shutdown()

    def test_status(self):
        st = self.s.status()
        self.assertEqual(st["device"]["os"], "Android 16 (sim)")
        self.assertTrue(st["service"]["connected"])

    def test_guard_needs_confirmation(self):
        self.s.scroll("down", until="Reset")          # 'Reset' is off-screen
        with self.assertRaises(PhoneError) as cm:
            self.s.tap("Reset")
        self.assertEqual(cm.exception.code, "NEEDS_CONFIRMATION")
        # and the sim never navigated
        snap = self.s.current()
        self.assertFalse(any(n.name == "Reset screen" for n in snap.nodes.values()))

    def test_blind_tap_unnamed_point_needs_confirmation(self):
        with self.assertRaises(PhoneError) as cm:
            self.s.tap(x=5, y=5)
        self.assertEqual(cm.exception.code, "NEEDS_CONFIRMATION")

    def test_blind_tap_on_named_element_proceeds(self):
        r = self.s.tap(x=540, y=700)                  # a visible row
        self.assertTrue(r["action"].get("ok", True))
        self.s.back()

    def test_dialog_occludes_semantic_clicks_and_back_dismisses(self):
        self.sim.dialog = {"title": "Allow axdroid to access photos?", "message": "choose",
                           "buttons": ["Allow", "Don’t allow"]}
        self.sim.bump(2)
        with self.assertRaises(PhoneError) as cm:
            self.s.tap("Battery")
        self.assertEqual(cm.exception.code, "OCCLUDED")
        # the dialog's own buttons are reachable, and declines are never guarded
        r = self.s.tap("Don’t allow")
        self.assertEqual(r["action"]["via"], "ax-click")
        self.sim.dialog = None

    def test_custody_stop_resume(self):
        self.phone.call("stop", reason="test")
        with self.assertRaises(PhoneError) as cm:
            self.s.tap("Battery")
        self.assertEqual(cm.exception.code, "STOPPED_BY_OWNER")
        obs = self.s.observe()          # reads still work
        self.assertTrue(obs["snap"].nodes)
        self.phone.call("resume")
        r = self.s.tap("Battery")       # acts again
        self.assertEqual(r["action"]["via"], "ax-click")
        self.s.back()

    def test_wait_and_run_plan(self):
        r = self.s.run([
            {"launch": "Settings"},
            {"tap": "Battery"},
            {"back": True},
            {"type": "battery", "into": "Search", "submit": True},
            {"wait": "Battery", "timeout": 4},
        ])
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(r["log"]), 5)

    def test_run_stops_at_first_failure(self):
        r = self.s.run([
            {"tap": "NoSuchThingAnywhere"},
            {"tap": "Battery"},
        ])
        self.assertFalse(r["ok"])
        self.assertEqual(r["failed_step"]["step"], 1)
        self.assertEqual(r["failed_step"]["error"]["code"], "NOT_FOUND")

    def test_launch_and_apps(self):
        apps = self.s.apps()
        self.assertIn("com.android.settings", {a["pkg"] for a in apps})
        r = self.s.launch("Settings", fresh=True)
        self.assertEqual(r["app"], "com.android.settings")


class TestAuth(unittest.TestCase):
    def test_lan_requires_token(self):
        with self.assertRaises(PhoneError):
            Phone.via_lan("127.0.0.1", "")


if __name__ == "__main__":
    unittest.main()
