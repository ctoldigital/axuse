"""Host-side MCP server: `phone_read_text` — OCR escape hatch for opaque
screens (images, canvas, video frames) that carry no accessibility semantics.

The axdroid app stays screenshot-free by design; this is the *host* helper
that exists for exactly the screens axdroid reports as opaque. It captures
over adb (dev/USB affordance) and reads text with Windows OCR (offline).

Run: python -m androidworld.host_mcp        (env AW_ADB, AW_SERIAL)
The benchmark agent gets two servers: axdroid (on-device, a11y) + this one.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from .adb_bridge import LocalAdbEnv
from .ocr import ocr_png

PROTO = "2025-03-26"
PROTOCOLS = ["2025-11-25", "2025-06-18", "2025-03-26"]

TOOLS = [{
    "name": "phone_read_text",
    "description": ("OCR the CURRENT screen and return its text lines (with "
                    "rough pixel positions). Use ONLY when the accessibility "
                    "tree lacks the information (photos, drawings, maps, "
                    "video frames) — phone_observe is always the better tool "
                    "when semantics exist."),
    "inputSchema": {"type": "object", "properties": {},
                    "required": [], "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "destructiveHint": False,
                    "idempotentHint": True, "openWorldHint": False},
}, {
    "name": "phone_read_colors",
    "description": ("Dominant colors (hex + share) of a screen region in px. "
                    "For pixel-only content the a11y tree cannot name: color "
                    "swatches, palettes, drawings. Combine with phone_find "
                    "bounds: sample each element's region to learn its "
                    "color."),
    "inputSchema": {"type": "object",
                    "properties": {"x": {"type": "number"}, "y": {"type": "number"},
                                   "w": {"type": "number"}, "h": {"type": "number"}},
                    "required": [], "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "destructiveHint": False,
                    "idempotentHint": True, "openWorldHint": False},
}, {
    "name": "phone_read_grid",
    "description": ("Read a pixel-drawn GRID (maze, board, tile map, pixel "
                    "puzzle) as a matrix, one symbol per cell: `.` white, `#` "
                    "black, G/R/B/Y/C/P colours (G green), g grey; a trailing "
                    "`*` marks something drawn inside the cell — a player "
                    "marker, a letter. Call it with no arguments: the grid is "
                    "the largest block on screen and its box and size are "
                    "found automatically (pass x,y,w,h only to restrict the "
                    "search, rows/cols only if the size is detected wrongly). "
                    "Use it for boards the accessibility tree cannot "
                    "describe; read it again after each move to see where "
                    "your marker is."),
    "inputSchema": {"type": "object",
                    "properties": {"x": {"type": "number"}, "y": {"type": "number"},
                                   "w": {"type": "number"}, "h": {"type": "number"},
                                   "rows": {"type": "number"}, "cols": {"type": "number"}},
                    "required": [], "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "destructiveHint": False,
                    "idempotentHint": True, "openWorldHint": False},
}, {
    "name": "phone_scan_list",
    "description": ("Open EVERY row of the list on screen one after another, read each "
                    "row's detail screen, come back, and return all the records plus the "
                    "sets of rows whose records are IDENTICAL. Use it when a task needs "
                    "every item compared or counted — exact duplicates, 'how many ... "
                    "with ...', 'delete all that ...' — instead of opening dozens of "
                    "rows yourself. Open the list first (rows must be cells); it "
                    "scrolls the list itself, changes nothing, and takes about 2 "
                    "seconds per row. The result says which rows are identical and how "
                    "to recognise them (their row label and their fields); to act on "
                    "one, open a row with that label until its detail shows those "
                    "fields. If it reports INCOMPLETE, call it again."),
    "inputSchema": {"type": "object",
                    "properties": {"list": {"type": "string", "description":
                                            "part of the list's name, if several lists are on screen"},
                                   "max_rows": {"type": "number"}},
                    "required": [], "additionalProperties": False},
    "annotations": {"readOnlyHint": False, "destructiveHint": False,
                    "idempotentHint": True, "openWorldHint": False},
}, {
    "name": "phone_read_video_text",
    "description": ("While a video PLAYS on the device, screen-record it for "
                    "`duration` seconds (2-40; give the video's full length "
                    "+2s) and return the sequence of distinct text messages "
                    "shown (per-frame noise is averaged out; a reading must "
                    "stay stable for ~1s). Simplest: open the video in the player, "
                    "then call with from_start: true — the tool itself pauses, "
                    "rewinds to 0:00, records, plays and stops when playback ends "
                    "(duration is only a ceiling, default 40). Otherwise start "
                    "playback FIRST and call once; frames after playback ends are "
                    "ignored."),
    "inputSchema": {"type": "object",
                    "properties": {"duration": {"type": "number"},
                                   "from_start": {"type": "boolean", "description":
                                                  "pause, rewind to 0:00, record the whole video, stop when it ends"},
                                   "play": {"type": "boolean", "description":
                                            "press PLAY 1 s after recording starts (pause the video at 0:00 first)"}},
                    "required": [], "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "destructiveHint": False,
                    "idempotentHint": False, "openWorldHint": False},
}]


def _text_result(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": False}


class HostCore:
    def __init__(self, adb_path: str | None = None, serial: str | None = None):
        self.bridge = LocalAdbEnv(
            adb_path or os.environ.get(
                "AW_ADB", __import__("shutil").which("adb") or "adb"),
            serial or os.environ.get("AW_SERIAL", "emulator-5554"))
        self._png = None

    def screenshot_fresh(self) -> bytes:
        """a new capture every time — for state that changes between calls
        (a marker that moved), unlike the 3 s cache below"""
        import time as _t
        data = self.bridge.screenshot_png()
        self._png = (_t.time(), data)
        return data

    def screenshot(self) -> bytes:
        import time as _t
        # 3s cache: several color samples of one screen shouldn't re-capture
        now = _t.time()
        if self._png and now - self._png[0] < 3.0:
            return self._png[1]
        data = self.bridge.screenshot_png()
        self._png = (now, data)
        return data

    def _media_state(self) -> tuple[bool, int | None]:
        """(is a media session playing, its position in ms) from `dumpsys
        media_session` — VLC and most players publish a PlaybackState."""
        import re
        out = self.bridge.shell("dumpsys", "media_session", timeout=20)
        m = re.search(r"PlaybackState \{state=([23]), position=(\d+)", out)
        return "PlaybackState {state=3" in out, (int(m.group(2)) if m else None)

    def record(self, duration: float, play: bool = False, from_start: bool = False) -> str:
        """adb screenrecord → local mp4 path. Real frame timing (screencap
        polling ran at an unknown 0.5–1.5 s cadence).
        play=True presses MEDIA_PLAY ~1 s after the recording started: a
        video started by the agent was already seconds in by the time the
        next tool call began recording (the first messages were lost).
        from_start=True does the whole sequence from the host: PAUSE, REWIND
        until the position is 0:00, record, PLAY, and stop one second after
        playback ends (`duration` is only a ceiling). VLC's on-screen
        controls hide within a second or two, faster than a model turn, so
        an agent cannot reliably tap Pause / drag the slider itself."""
        import subprocess
        import tempfile
        remote = "/sdcard/axdroid_rec.mp4"
        if from_start:
            # VLC treats the media keys as TOGGLES (PAUSE on a paused video
            # starts it), so press one only after checking the state
            def _ensure(want_playing: bool) -> None:
                playing, _ = self._media_state()
                if playing != want_playing:
                    self.bridge.shell("input", "keyevent", "85")     # KEYCODE_MEDIA_PLAY_PAUSE
                    time.sleep(0.8)
            _ensure(False)
            # Rewind WITHOUT trusting the reported position: the media
            # session often keeps a stale "position=0" while the video is
            # well in (a run then started on the second message and the
            # first was never recorded). Each press jumps back ~10 s, and a
            # press at 0:00 does nothing, so press enough for a 40 s video.
            for _ in range(5):
                self.bridge.shell("input", "keyevent", "89")         # KEYCODE_MEDIA_REWIND
                time.sleep(0.5)
            _ensure(False)
        limit = int(round(duration)) + (5 if from_start else 0)
        # own process (not bridge.shell): the bridge holds a per-serial lock
        # for a call's whole duration and the play key must go in meanwhile
        rec = subprocess.Popen([self.bridge.adb_path, "-s", self.bridge.serial, "shell",
                                "screenrecord", "--time-limit", str(limit),
                                "--bit-rate", "8000000", remote],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if play or from_start:
            time.sleep(1.2)
            if from_start:
                _ensure(True)
            else:
                self.bridge.shell("input", "keyevent", "126")   # KEYCODE_MEDIA_PLAY
        if from_start:
            # the player's session keeps reporting "playing, position 0" for
            # the whole video and after it, so it cannot tell when the video
            # ends; the focused window can — the player closes (or shows an
            # end screen in another window) when playback is over
            def _focus() -> str:
                out = self.bridge.shell("dumpsys", "window", "displays", timeout=20)
                return next((l.strip() for l in out.splitlines() if "mCurrentFocus" in l), "")
            focus0, t0, idle = _focus(), time.time(), 0
            while time.time() - t0 < duration:
                time.sleep(0.5)
                idle = idle + 1 if _focus() != focus0 else 0
                if idle >= 2:
                    break
            time.sleep(1.0)
            self.bridge.shell("pkill", "-2", "screenrecord")     # SIGINT: finalises the mp4
        try:
            rec.wait(timeout=duration + 30)
        except subprocess.TimeoutExpired:
            rec.kill()
        time.sleep(0.5)   # mp4 finalised on the device
        fd, local = tempfile.mkstemp(suffix=".mp4")
        os.close(fd)
        r = self.bridge._adb("pull", remote, local, timeout=60)
        self.bridge.shell("rm", "-f", remote)
        if getattr(r, "returncode", 0) != 0:
            raise RuntimeError(f"pull of the recording failed: {getattr(r, 'stderr', b'')!r}")
        return local

    async def handle(self, msg: dict) -> dict | None:
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            return None
        ok = lambda result: {"jsonrpc": "2.0", "id": mid, "result": result}  # noqa: E731
        try:
            if method == "initialize":
                want = params.get("protocolVersion")
                return ok({"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                           "capabilities": {"tools": {"listChanged": False}},
                           "serverInfo": {"name": "axdroid-host", "title": "axdroid host helpers",
                                          "version": "0.1.0"}})
            if method == "ping":
                return ok({})
            if method == "tools/list":
                return ok({"tools": TOOLS})
            if method == "tools/call":
                name = params.get("name")
                if name not in {t["name"] for t in TOOLS}:
                    return ok({"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True})
                args = params.get("arguments") or {}
                loop = asyncio.get_running_loop()
                if name == "phone_read_text":
                    png = await loop.run_in_executor(None, self.screenshot)
                    res = await loop.run_in_executor(None, ocr_png, png)
                    lines = "\n".join(f"y={l['y']:>4} {l['text']}" for l in res["lines"])
                    return ok(_text_result(lines or "(no text found on screen)"))
                if name == "phone_read_colors":
                    from .ocr import colors_png
                    png = await loop.run_in_executor(None, self.screenshot)
                    res = await loop.run_in_executor(
                        None, colors_png, png,
                        args.get("x", 0), args.get("y", 0),
                        args.get("w", 0), args.get("h", 0))
                    lines = [f"{c['hex']} {int(c['share'] * 100)}%" for c in res["colors"]]
                    return ok(_text_result(f"region {res['region']}: " + ", ".join(lines)))
                if name == "phone_scan_list":
                    from .scan import scan_list
                    port = os.environ.get("AW_AXDROID_PORT", "8360")

                    def dev_call(tool, targs):
                        import urllib.request
                        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": tool, "arguments": targs}}).encode()
                        rq = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=body,
                                                    headers={"Content-Type": "application/json"})
                        d = json.load(urllib.request.urlopen(rq, timeout=120))
                        if "result" not in d:
                            return json.dumps(d.get("error", d))[:300]
                        return d["result"]["content"][0]["text"]

                    text = await loop.run_in_executor(
                        None, lambda: scan_list(dev_call, str(args.get("list", "")),
                                                int(args.get("max_rows", 150))))
                    return ok(_text_result(text))
                if name == "phone_read_grid":
                    from .ocr import grid_from_png
                    png = await loop.run_in_executor(None, self.screenshot_fresh)
                    res = await loop.run_in_executor(
                        None, lambda: grid_from_png(
                            png, int(args.get("x", 0)), int(args.get("y", 0)),
                            int(args.get("w", 0)), int(args.get("h", 0)),
                            int(args.get("rows", 0)), int(args.get("cols", 0))))
                    if res.get("error"):
                        return ok(_text_result(
                            res["error"] + (f" (grid box found: {res['box']})" if res.get("box") else "")))
                    lines = [f"grid {res['rows']} rows x {res['cols']} cols, cell about "
                             f"{res['cell_px'][0]}x{res['cell_px'][1]} px, box x,y,w,h = {res['box']}",
                             "(row 0 is the top; '.' white  '#' black  G green  * = something drawn in the cell)"]
                    for i, row in enumerate(res["matrix"]):
                        lines.append(f"r{i}: " + " ".join(s.ljust(2) for s in row))
                    return ok(_text_result("\n".join(lines)))
                if name == "phone_read_video_text":
                    from .ocr import video_text_from_file
                    from_start = bool(args.get("from_start"))
                    duration = max(2.0, min(float(args.get("duration", 40 if from_start else 24)),
                                            90 if from_start else 40))
                    path = await loop.run_in_executor(None, self.record, duration,
                                                      bool(args.get("play")), from_start)
                    try:
                        res = await loop.run_in_executor(None, video_text_from_file, path)
                    finally:
                        # keep the latest recording for diagnosis (a short
                        # "video playing" figure cannot be explained after
                        # the fact otherwise); older ones are replaced
                        try:
                            import shutil
                            import tempfile
                            shutil.move(path, os.path.join(tempfile.gettempdir(), "axdroid_last_video.mp4"))
                        except OSError:
                            pass
                    note = (f" (recorded {duration:.0f}s, video playing "
                            f"{res.get('playing_s', 0)}s)")
                    if res.get("playing_s", 0) < 5.0:
                        # the emulator draws video in software and can deliver
                        # only a few frames: a read this sparse can miss
                        # messages, and two reads may disagree
                        note += (" — SPARSE capture (few video frames): messages may be missing; "
                                 "call again, and when two reads differ the one listing MORE "
                                 "messages (that contains the shorter list) is the complete one")
                    return ok(_text_result(
                        "messages in order: " +
                        ("; ".join(res["messages"]) or "(none captured)") + note))
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}


async def main():
    core = HostCore()
    loop = asyncio.get_running_loop()
    out = sys.stdout
    try:
        out.reconfigure(encoding="utf-8")
    except Exception:
        pass
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        r = await core.handle(msg)
        if r is not None:
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
            out.flush()


# ── Streamable HTTP transport (same shape as the phone's /mcp) ────────────

def serve_http(port: int, adb_path: str, serial: str):
    """Synchronous, thread-hostable HTTP MCP endpoint at POST /mcp.

    Local stdio spawning by MCP clients is fragile on Windows (the child may
    not inherit a bootable environment); an HTTP endpoint behaves exactly
    like the phone's own on-device server, which every client handles well.
    """
    import threading
    import uuid
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    core = HostCore(adb_path, serial)
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            # codex's rmcp EventNotificationTransport opens GET /mcp and
            # treats every non-200 (405/501) as fatal. Serve a real SSE
            # stream with keepalives for the session lifetime.
            if self.path == "/mcp":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    deadline = time.time() + 3300
                    while time.time() < deadline:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        time.sleep(15)
                except Exception:
                    pass
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            if self.path != "/mcp":
                self.send_response(404)
                self.end_headers()
                return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                msg = json.loads(self.rfile.read(n).decode("utf-8"))
            except ValueError:
                self.send_response(400)
                self.end_headers()
                return
            try:
                self._handle_msg(msg)
            except Exception:
                import traceback
                traceback.print_exc()
                try:
                    data = json.dumps({"jsonrpc": "2.0", "id": msg.get("id"),
                                       "error": {"code": -32603, "message": "server error"}}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception:
                    pass

        def _handle_msg(self, msg):
            session = self.headers.get("Mcp-Session-Id")
            print(f"[host-mcp] {self.command} {self.path} {msg.get('method')} id={msg.get('id')}",
                  file=sys.stderr, flush=True)
            if msg.get("id") is None or str(msg.get("method", "")).startswith("notifications/"):
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            future = asyncio.run_coroutine_threadsafe(core.handle(msg), loop)
            body = future.result(timeout=180)
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            print(f"[host-mcp] replying {msg.get('method')} {len(data)}B", file=sys.stderr, flush=True)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            # mint a session id ONLY on initialize; changing it mid-session
            # makes streamable-HTTP clients (codex rmcp) kill the transport
            if not session and msg.get("method") == "initialize":
                self.send_header("Mcp-Session-Id", uuid.uuid4().hex)
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
            print(f"[host-mcp] wrote {msg.get('method')}", file=sys.stderr, flush=True)

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "http":
        serve_http(int(sys.argv[2]), os.environ.get("AW_ADB", ""), os.environ.get("AW_SERIAL", "emulator-5554"))
    else:
        asyncio.run(main())
