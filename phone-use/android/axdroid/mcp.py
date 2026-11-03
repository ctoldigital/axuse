"""axdroid — MCP server (stdio + Streamable HTTP), zero dependencies.

One JSON-RPC core, two transports — the same shape as axphone's, so an agent
that learned one phone knows the other. The engine is a Session over the
on-device RPC server; the phone itself enforces the guard, custody and
journal, so this layer stays thin.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import time

from . import __version__
from .client import PhoneError
from .engine import Session

PROTOCOLS = ["2025-11-25", "2025-06-18", "2025-03-26"]

INSTRUCTIONS = "\n".join([
    "axdroid: drive a real Android phone through its native accessibility tree (text first; a screenshot only as a fallback).",
    "Loop: phone_observe once → act on refs ([e7]) → read the diff each action returns (no need to re-observe).",
    "Targets: a ref ('e7'), visible text ('Wi‑Fi'), or {text, role, rid, nth}. Ambiguity is an error: pick a ref.",
    "Open apps with phone_launch {app:'Settings'} — never navigate the home screen.",
    "Text: phone_type {into:'Search', text:'…', submit:true} — typed with the platform's own text action and verified.",
    "Off-screen rows: phone_scroll {until:'text'} or just tap them (scrollable-ancestor support).",
    "NEEDS_CONFIRMATION means the action moves money, sends, deletes or grants: ask the user, then repeat with confirm:true.",
    "Screens with no accessibility tree (games, canvases) report as opaque; coordinate taps there are blind taps and need confirm:true.",
    "Only when the tree cannot tell you what is on screen (a map, game, canvas, photo): phone_screenshot returns an image for your own vision. It refuses screens that have readable text unless you pass force:true.",
    "phone_run executes a known step sequence without a model round trip per step.",
    "A 'notice:' line (rate limit, bot check) means stop and tell the user — never retry around it.",
])

T = {"type": "string"}
TARGET = {"type": "string", "description": "ref like 'e12' or visible text"}


def _tool(name, desc, props, required=(), *, ro=False, destructive=False):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": props, "required": list(required), "additionalProperties": False},
            "annotations": {"readOnlyHint": ro, "destructiveHint": destructive, "idempotentHint": ro, "openWorldHint": not ro}}


TOOLS = [
    _tool("phone_observe", "Read the current screen as a compact accessibility tree with refs. Use once per task or after phone_launch; actions already return diffs.",
          {"mode": {"type": "string", "enum": ["full", "lite"], "description": "lite = actionable elements only (small models)"},
           "max_lines": {"type": "number"}}, ro=True),
    _tool("phone_find", "Find elements by text / role / resource id on the current screen (including off-screen rows). Returns refs, states and bounds.",
          {"text": T, "role": T, "rid": T}, ro=True),
    _tool("phone_tap", "Tap an element (semantic click first — the platform's own action; gesture fallback is occlusion-checked on the phone), or a point {x,y} in screen px. A point where no element can be named is a blind tap: NEEDS_CONFIRMATION until repeated with confirm:true.",
          {"target": TARGET, "role": T, "rid": T, "nth": {"type": "number"}, "x": {"type": "number"}, "y": {"type": "number"},
           "kind": {"type": "string", "enum": ["tap", "double_tap", "long_press"]},
           "space": {"type": "string", "enum": ["screen", "image", "normalized"],
                     "description": "what x,y mean: screen px (default), the last phone_screenshot image's px, or 0-1000 on both axes"},
           "confirm": {"type": "boolean", "description": "true only after the user approved a consequential (or blind-coordinate) action"}}, destructive=True),
    _tool("phone_type", "Type text into a field (taps and focuses it first) or the focused field. Typed with ACTION_SET_TEXT and read back; submit presses the IME action. On a slider (shown as value=N (min–max)) the text is a number and sets its value exactly.",
          {"text": T, "into": TARGET, "replace": {"type": "boolean"}, "submit": {"type": "boolean"},
           "at": {"type": "string", "enum": ["start", "end"],
                  "description": "keep the field's text and insert at its start (or end) instead of replacing it"}},
          ["text"], destructive=True),
    _tool("phone_scroll", "Scroll to reveal content. direction = where the content is. until = stop when this text is visible.",
          {"direction": {"type": "string", "enum": ["down", "up", "left", "right"]}, "until": T, "in": TARGET, "max_swipes": {"type": "number"}}),
    _tool("phone_swipe", "Raw swipe from (x1,y1) by (x2,y2), in screen px — carousels, games, notifications.",
          {"x1": {"type": "number"}, "y1": {"type": "number"}, "x2": {"type": "number"}, "y2": {"type": "number"},
           "duration": {"type": "number"}}, ["x1", "y1", "x2", "y2"]),
    _tool("phone_back", "Go back (the system navigation action).", {}),
    _tool("phone_button", "Press a device-level action.",
          {"name": {"type": "string", "enum": ["home", "back", "recents", "notifications", "quick_settings", "power_dialog", "lock", "take_screenshot"]}}, ["name"]),
    _tool("phone_launch", "Open an app by name ('Settings') or package. Faster and more reliable than tapping icons.",
          {"app": T, "fresh": {"type": "boolean", "description": "best-effort restart first: clean state"}}, ["app"]),
    _tool("phone_apps", "List installed launchable apps (name + package).", {}, ro=True),
    _tool("phone_wait", "Wait until text appears (or disappears with gone:true) — evaluated on the phone, event-driven. stable:N waits until the screen stopped changing for N seconds (streaming answers).",
          {"text": T, "gone": {"type": "boolean"}, "timeout": {"type": "number"}, "stable": {"type": "number"}}, [], ro=True),
    _tool("phone_run", "Run a known sequence of steps without a model round trip per step. Stops at the first failure and reports it. "
                       "Steps: {tap:target}, {long_press:target}, {type:text, into:target, replace, submit}, {scroll:'down', until:text}, {launch:app}, "
                       "{button:name}, {wait:text, gone, timeout, stable}, {expect:text}, {back:true}. Add confirm:true to a step only after user approval.",
          {"steps": {"type": "array", "items": {"type": "object"}}}, ["steps"], destructive=True),
    _tool("phone_status", "Device, bridge, guard policy, custody and measured latencies.", {}, ro=True),
    _tool("phone_screenshot", "Take a screenshot and return it as an image. Fallback ONLY for screens the accessibility tree cannot describe (maps, games, canvases, photos). Refuses a screen with readable text unless force:true; the phone owner can turn it off.",
          {"force": {"type": "boolean", "description": "true only when phone_observe really lacks what you need"},
           "max_px": {"type": "number", "description": "longest side in px (default 1280)"},
           "grid": {"type": "boolean", "description": "overlay a labelled coordinate grid in image pixels (default true)"},
           "region": {"type": "array", "items": {"type": "number"}, "description": "[left, top, right, bottom]: zoom into this part of the screen at native detail (screen px unless space says otherwise)"},
           "space": {"type": "string", "enum": ["screen", "image", "normalized"], "description": "what region coordinates mean"},
           "marks": {"type": "boolean", "description": "box interactive elements and label them with their phone_observe refs (on-device server)"}}, ro=True),
]
READ_ONLY = {t["name"] for t in TOOLS if t["annotations"]["readOnlyHint"]}


class McpCore:
    def __init__(self, session: Session):
        self.s = session
        self.started = False

    def _server_info(self, proto):
        return {"protocolVersion": proto, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "axdroid", "title": "axuse phone-use (Android)", "version": __version__},
                "instructions": INSTRUCTIONS}

    async def handle(self, msg: dict, *, scopes: frozenset = frozenset({"phone:read", "phone:act"}),
                     actor: str = "local") -> dict | None:
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:
            return None
        try:
            if method == "initialize":
                want = params.get("protocolVersion")
                return self._ok(mid, self._server_info(want if want in PROTOCOLS else PROTOCOLS[0]))
            if method == "ping":
                return self._ok(mid, {})
            if method == "tools/list":
                visible = TOOLS if "phone:act" in scopes else [t for t in TOOLS if t["name"] in READ_ONLY]
                return self._ok(mid, {"tools": visible})
            if method == "tools/call":
                name, args = params.get("name"), params.get("arguments") or {}
                if name not in {t["name"] for t in TOOLS}:
                    return self._ok(mid, _err_result("UNKNOWN_TOOL", f"unknown tool {name}"))
                if name not in READ_ONLY and "phone:act" not in scopes:
                    return self._ok(mid, _err_result("INSUFFICIENT_SCOPE", "this token is read-only (phone:read)"))
                return self._ok(mid, await self.call(name, args))
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        except Exception as e:  # protocol layer never dies on a tool bug
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}}

    @staticmethod
    def _ok(mid, result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    async def call(self, name: str, a: dict) -> dict:
        try:
            if not self.started:
                await self.s.start()
                self.started = True
            s = self.s
            if name == "phone_screenshot":
                r = await _run(s.screenshot, force=bool(a.get("force")), max_px=int(a.get("max_px", 1280)),
                               grid=bool(a.get("grid", True)), region=a.get("region"))
                self.last_shot = (r.get("w"), r.get("h"), r.get("ox", 0), r.get("oy", 0), r.get("cw"), r.get("ch"))
                r["text"] += (" with a labelled coordinate grid in image pixels" if a.get("grid", True) else "") + (
                    ". Marks are available on the on-device server only." if a.get("marks") else "") + (
                    ". For a blind tap use phone_tap {x, y, space:\"image\" or \"normalized\", confirm:true}.")
                out = {"content": [{"type": "text", "text": r["text"]},
                                   {"type": "image", "data": r["image_b64"], "mimeType": r.get("mime", "image/jpeg")}],
                       "isError": False}
                return out
            if name == "phone_observe":
                r = s.observe(mode=a.get("mode", "full"), max_lines=int(a.get("max_lines", 220)))
                return _text(r["text"], {"app": r.get("app"), "observe_ms": r.get("ms")})
            if name == "phone_find":
                from .model import find as find_nodes
                snap = s.current()
                hits = find_nodes(snap, a.get("text"), a.get("role"), a.get("rid"))
                if not hits:
                    snap = s.observe()["snap"]
                    hits = find_nodes(snap, a.get("text"), a.get("role"), a.get("rid"))
                lines = [f"{n.ref or '-'} {n.role} \"{(n.name or '')[:60]}\" visible={n.visible}"
                         + (f" value='{(n.value or '')[:40]}'" if n.value else "")
                         + (f" rid={n.rid}" if n.rid else "")
                         + (f" bounds={n.bounds}" if n.bounds else "") for n in hits[:25]]
                return _text("\n".join(lines) if lines else "no match", {"count": len(hits)})
            if name == "phone_tap":
                target = a.get("target")
                if target is not None and (a.get("role") or a.get("rid") or a.get("nth")):
                    target = {"ref": target if str(target).startswith("e") and str(target)[1:].isdigit() else None,
                              "text": None if str(target).startswith("e") and str(target)[1:].isdigit() else target,
                              "role": a.get("role"), "rid": a.get("rid"), "nth": a.get("nth")}
                elif target is None and a.get("rid"):
                    target = {"rid": a["rid"], "role": a.get("role")}
                x, y = a.get("x"), a.get("y")
                if a.get("space") in ("image", "normalized") and x is not None and y is not None:
                    disp = (s.info or {}).get("device", {}).get("display", {})
                    x, y = convert_xy(a["space"], x, y, (disp.get("w"), disp.get("h")), getattr(self, "last_shot", None))
                r = await _run(s.tap, target, x=x, y=y,
                               kind="long_press" if a.get("kind") == "long_press" else "tap",
                               confirm=bool(a.get("confirm")))
            elif name == "phone_type":
                r = await _run(s.type, a["text"], a.get("into"), replace=bool(a.get("replace", True)),
                               submit=bool(a.get("submit")), at=a.get("at"))
            elif name == "phone_scroll":
                r = await _run(s.scroll, a.get("direction", "down"), until=a.get("until"),
                               max_swipes=int(a.get("max_swipes", 8)))
            elif name == "phone_swipe":
                r = await _run(s.swipe, a["x1"], a["y1"], a["x2"], a["y2"], float(a.get("duration", 0.3)))
            elif name == "phone_back":
                r = await _run(s.back)
            elif name == "phone_button":
                r = await _run(s.button, a["name"])
            elif name == "phone_launch":
                r = await _run(s.launch, a["app"], fresh=bool(a.get("fresh")))
            elif name == "phone_apps":
                apps = await _run(s.apps)
                return _text("\n".join(f"{x['name']} — {x['pkg']}" for x in apps) or "(none)")
            elif name == "phone_wait":
                r = await _run(s.wait_for, a.get("text"), gone=bool(a.get("gone")),
                               timeout=float(a.get("timeout", 8)), stable=float(a.get("stable", 0)))
                if not r["action"]["matched"]:
                    return _err_result("WAIT_TIMEOUT", r["text"])
            elif name == "phone_run":
                r = await _run(s.run, a["steps"])
                body = "\n".join(r["log"]) + "\n---\n" + r["text"]
                if not r["ok"]:
                    return {"content": [{"type": "text", "text": body}], "isError": True,
                            "structuredContent": {"ok": False, "failed_step": r["failed_step"]}}
                return _text(body, {"ok": True, "ms": r["ms"]})
            elif name == "phone_status":
                return _text(json.dumps(s.status(), indent=1))
            else:
                return _err_result("UNKNOWN_TOOL", name)
            act = r.get("action") or {}
            head = f"{act.get('op', name)} {act.get('target') or ''}".strip()
            if act.get("ms") is not None:
                head += f" · {act['ms']:.0f} ms"
            if act.get("via"):
                head += f" · via {act['via']}"
            return _text(head + "\n" + r["text"], {"action": act, "diff": r.get("diff")})
        except PhoneError as e:
            return _err_result(e.code, str(e), **e.facts)


async def _run(fn, *args, **kwargs):
    loop = asyncio.get_running_loop()
    import functools
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


def convert_xy(space, x, y, screen, shot):
    """x,y given in `space` -> screen pixels. space: screen (unchanged), image (the last phone_screenshot's
    pixels, `shot`=(w,h)) or normalized (0-1000 on both axes). `screen`=(w,h) of the phone."""
    if space in (None, "", "screen") or x is None or y is None:
        return x, y
    sw, sh = screen
    if space == "normalized":
        px, py = x * sw / 1000, y * sh / 1000
    elif space == "image":
        if not shot or shot[0] <= 0 or shot[1] <= 0:
            raise ValueError('space:"image" needs a phone_screenshot first (it defines the image pixels)')
        ox, oy, cw, ch = (shot[2], shot[3], shot[4], shot[5]) if len(shot) >= 6 else (0, 0, sw, sh)
        px, py = ox + x * cw / shot[0], oy + y * ch / shot[1]
    else:
        raise ValueError("space must be screen, image or normalized")
    if px < 0 or py < 0 or px > sw or py > sh:
        raise ValueError(f"the point ({round(px)},{round(py)}) is outside the screen ({sw}x{sh}): check the coordinate space")
    return round(px), round(py)


def _text(text, structured=None):
    out = {"content": [{"type": "text", "text": text}], "isError": False}
    if structured is not None:
        out["structuredContent"] = structured
    return out


def _err_result(code, message, **facts):
    payload = {"code": code, "message": message, **facts}
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "isError": True,
            "structuredContent": payload}


# ── stdio ────────────────────────────────────────────────────────────────────
async def serve_stdio(core: McpCore):
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
            out.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
            out.flush()
            continue
        batch = msg if isinstance(msg, list) else [msg]
        replies = [r for r in [await core.handle(m) for m in batch if isinstance(m, dict)] if r is not None]
        if replies:
            out.write(json.dumps(replies if isinstance(msg, list) else replies[0], ensure_ascii=False) + "\n")
            out.flush()


# ── Streamable HTTP (optional; for LAN-shared hosts) ─────────────────────────
async def serve_http(core: McpCore, host: str, port: int, *, token: str | None = None):
    import hashlib
    import hmac as hmac_mod

    expected = hashlib.sha256(token.encode()).hexdigest() if token else None

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                req = await reader.readline()
                if not req:
                    return
                method, path, _ = req.decode("latin-1").split(" ", 2)
                headers = {}
                while True:
                    h = await reader.readline()
                    if h in (b"\r\n", b"\n", b""):
                        break
                    k, _, v = h.decode("latin-1").partition(":")
                    headers[k.strip().lower()] = v.strip()
                body = await reader.readexactly(int(headers.get("content-length", "0") or 0)) if headers.get("content-length") else b""
                auth = headers.get("authorization", "")
                ok_auth = expected is None or (
                    auth.lower().startswith("bearer ") and
                    hmac_mod.compare_digest(hashlib.sha256(auth[7:].strip().encode()).hexdigest(), expected))
                if path.split("?")[0] != "/mcp" or method != "POST":
                    writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
                    continue
                if not ok_auth:
                    writer.write(b'HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\nContent-Length: 48\r\n\r\n'
                                 b'{"error":"unauthorized","code":"UNAUTHORIZED"}')
                    await writer.drain()
                    continue
                try:
                    msg = json.loads(body or b"null")
                except json.JSONDecodeError:
                    writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
                    continue
                batch = msg if isinstance(msg, list) else [msg]
                replies = [r for r in [await core.handle(m) for m in batch if isinstance(m, dict)] if r is not None]
                if not replies:
                    writer.write(b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
                    continue
                rb = json.dumps(replies if isinstance(msg, list) else replies[0], ensure_ascii=False).encode()
                extra = b"Mcp-Session-Id: " + secrets.token_hex(16).encode() + b"\r\n" \
                    if any(isinstance(m, dict) and m.get("method") == "initialize" for m in batch) else b""
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" + extra +
                             b"Content-Length: " + str(len(rb)).encode() + b"\r\n\r\n" + rb)
                await writer.drain()
                if headers.get("connection", "").lower() == "close":
                    return
        except (asyncio.IncompleteReadError, ConnectionError, ValueError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, host, port)
    async with server:
        await server.serve_forever()
