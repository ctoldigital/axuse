"""MCP layer over the engine, against the sim ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â stdio smoke included."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import unittest

sys.path.insert(0, "..")

from axdroid.engine import Session  # noqa: E402
from axdroid.mcp import McpCore, TOOLS  # noqa: E402
from axdroid.sim import serve_sim  # noqa: E402


class TestMcp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sim, phone, cls.shutdown = serve_sim()
        cls.core = McpCore(Session(phone))

    def setUp(self):
        self.sim.reset_state()
        self.core.s.refs.clear()
        self.core.s.snap = None

    @classmethod
    def tearDownClass(cls):
        cls.shutdown()

    def run_coro(self, coro):
        return asyncio.run(coro)

    def test_initialize_and_tools(self):
        r = self.run_coro(self.core.handle({"id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-03-26"}}))
        self.assertEqual(r["result"]["serverInfo"]["name"], "axdroid")
        r = self.run_coro(self.core.handle({"id": 2, "method": "tools/list"}))
        names = {t["name"] for t in r["result"]["tools"]}
        self.assertIn("phone_observe", names)
        self.assertIn("phone_run", names)
        self.assertEqual(len(names), len(TOOLS))

    def test_observe_call(self):
        r = self.run_coro(self.core.handle({"id": 3, "method": "tools/call",
                                  "params": {"name": "phone_observe", "arguments": {}}}))
        self.assertFalse(r["result"]["isError"])
        self.assertIn("app: com.android.settings", r["result"]["content"][0]["text"])

    def test_tap_and_diff(self):
        r = self.run_coro(self.core.handle({"id": 4, "method": "tools/call",
                                  "params": {"name": "phone_tap", "arguments": {"target": "Battery"}}}))
        self.assertFalse(r["result"]["isError"], r)
        self.assertIn("ax-click", r["result"]["content"][0]["text"])
        self.run_coro(self.core.handle({"id": 6, "method": "tools/call",
                              "params": {"name": "phone_button", "arguments": {"name": "back"}}}))

    def test_guard_needs_confirmation_surfaced(self):
        self.core.s.scroll("down", until="Reset")   # the risky row is off-screen
        r = self.run_coro(self.core.handle({"id": 7, "method": "tools/call",
                                  "params": {"name": "phone_tap", "arguments": {"target": "Reset"}}}))
        self.assertTrue(r["result"]["isError"])
        self.assertEqual(r["result"]["structuredContent"]["code"], "NEEDS_CONFIRMATION")

    def test_stdio_smoke(self):
        import os
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        lines = [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ]
        # stdio against the sim isn't wired in the CLI (adb default) ÃƒÂ¢Ã¢â€šÂ¬Ã¢â‚¬Â run the core directly in-process
        async def script():
            out = []
            class FakeStdin:
                def __init__(self, data): self.data = data
                def readline(self): return self.data.pop(0) if self.data else ""
            class FakeOut:
                def write(self, s): out.append(s)
                def flush(self): pass
            import axdroid.mcp as m
            real_in, real_out = sys.stdin, sys.stdout
            sys.stdin, sys.stdout = FakeStdin([l + "\n" for l in lines]), FakeOut()
            try:
                await m.serve_stdio(self.core)
            finally:
                sys.stdin, sys.stdout = real_in, real_out
            return out
        out = self.run_coro(script())
        self.assertEqual(len(out), 2)
        self.assertIn("axdroid", out[0])
        self.assertIn("phone_observe", out[1])


if __name__ == "__main__":
    unittest.main()
