"""axdroid — simulated Android phone (deterministic, no hardware).

Speaks the *identical* /rpc wire protocol as the app (see RpcServer.java +
Core.java) over real HTTP on an ephemeral port, so the entire host stack —
client, engine, refs/diffs, MCP, gates — is exercised against the same
protocol the real phone speaks. It models the semantics that matter:

  * ids are "w<windowId>:<i>/<j>" paths; dialogs recreate the window
    (ids change → the engine's signature-fallback diff still works)
  * a scrollable Settings list with off-screen rows and a search field
    (ACTION_SET_TEXT + IME submit filters the list)
  * switches with checked state; rows wrapping their controls
  * a system dialog that covers the screen: gesture taps under it are
    OCCLUDED, semantic clicks still work only if the node is reachable
  * the guard (same labels as Guard.java), custody stop/resume,
    and a change counter with realistic quiet timing
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

W, H = 1080, 2340
ROW_H = 168
STATUSBAR = 132

SETTINGS_ROWS = ["Network and internet", "Connections", "Apps", "Notifications", "Battery",
                 "Storage", "Sound and vibration", "Display", "Wallpaper and style",
                 "Themes", "Home screen", "Lock screen and AOD", "Security and privacy",
                 "Location", "Safety and emergency", "Accounts and backup", "Google",
                 "Advanced features", "Digital Wellbeing and parental controls",
                 "About phone", "Developer options", "Reset"]
ABOUT_ITEMS = ["Status information", "Legal information", "Device name", "Model number",
               "Software information", "Build number"]

F_CLICK, F_LONG, F_SCROLL, F_EDIT, F_CHECKABLE, F_CHECKED = 1, 2, 4, 8, 16, 32
F_ENABLED, F_SELECTED, F_FOCUSABLE, F_FOCUSED, F_VISIBLE, F_PASSWORD = 64, 128, 256, 512, 1024, 2048

RISK_RE = [
    ("payment", re.compile(r"\b(pay|buy|purchase|checkout|place order|order now|subscribe|confirm (payment|purchase|order)|send money|transfer|donate|tip|book now|rent|renew)\b|google pay|samsung pay", re.I)),
    ("send", re.compile(r"\b(send|post|publish|tweet|reply all|share|submit|call|dial)\b", re.I)),
    ("delete", re.compile(r"\b(delete|remove|erase|discard|clear all|reset|unsubscribe|cancel subscription|deactivate|close account|sign out|log out|uninstall|block|factory reset|erase all)\b", re.I)),
    ("permission", re.compile(r"\b(allow|always allow|trust|grant|install|enable installation)\b", re.I)),
]
SAFE_RE = re.compile(r"^\s*(don'?t|do not|not now|no thanks|no|deny|decline|never|later|skip|close|dismiss|"
                     r"cancel(?! (?:subscription|membership|order|plan|account|trip|booking|reservation)))\b", re.I)


class SimPhone:
    def __init__(self):
        self.lock = threading.Lock()
        self.state_seq = 0
        self.changes = 0
        self.last_event = time.monotonic()
        self.launch_count = {}
        self.journal = []
        self.typed_log = []
        self.policy = {"guard_mode": "ask", "allow_apps": [], "deny_apps": []}
        self.reset_state()

    def reset_state(self):
        """Back to a clean Settings home screen (used by tests between cases)."""
        self.foreground = "com.android.settings"
        self.stack = ["root"]
        self.scroll = 0
        self.search = ""
        self.wifi = True
        self.dialog = None
        self.ime_for = None
        self.stopped = False
        self.bump(1)

    # ── events ────────────────────────────────────────────────────────
    def bump(self, n=1):
        self.changes += n
        self.last_event = time.monotonic()

    def quiet_ms(self):
        return max(0, (time.monotonic() - self.last_event) * 1000)

    # ── windows & trees ───────────────────────────────────────────────
    def windows(self):
        wins = []
        if self.dialog:
            wins.append({"id": 91, "pkg": "com.android.systemui", "active": False,
                         "overlay": True, "bounds": [0, 0, W, H]})
        wins.append({"id": 24, "pkg": self.foreground, "active": True, "bounds": [0, 0, W, H]})
        if self.ime_for is not None:
            wins.append({"id": 77, "pkg": "com.google.android.inputmethod.latin", "ime": True,
                         "active": False, "bounds": [0, H - 520, W, 520]})
        return wins

    def node(self, win, path, cls, *, text=None, desc=None, rid=None, b=None, f=F_VISIBLE | F_ENABLED, value=None):
        return {"id": f"w{win}:{path}", "d": path.count("/") + (0 if path == "" else 1),
                "cls": cls, "text": text, "desc": desc, "rid": rid, "b": b, "f": f, "value": value}

    def sig(self, n):
        s = f"{n.get('cls')}|{n.get('rid')}|{n.get('text')}|{n.get('desc')}|{n.get('f', 0) & (F_CHECKABLE | F_CHECKED)}"
        return format(abs(hash(s)) & 0xFFFFFFFF, "x")

    def tree(self):
        """(windows, nodes) for the current state — parent-before-child, topmost window first."""
        nodes = []
        if self.dialog:
            d = self.dialog
            nodes.append(self.node(91, "", "android.widget.FrameLayout", b=[0, 0, W, H], f=F_VISIBLE | F_ENABLED))
            nodes.append(self.node(91, "0", "android.widget.TextView", text=d["title"], b=[W * 0.1, 900, W * 0.9, 1010]))
            nodes.append(self.node(91, "1", "android.widget.TextView", text=d["message"], b=[W * 0.1, 1020, W * 0.9, 1180]))
            for j, label in enumerate(d["buttons"]):
                nodes.append(self.node(91, f"2/{j}", "android.widget.Button", text=label,
                                       rid=f"android:id/button{j + 1}",
                                       b=[W * 0.1, 1260 + j * 150, W * 0.9, 1390 + j * 150],
                                       f=F_VISIBLE | F_ENABLED | F_CLICK | F_FOCUSABLE))
        # settings
        nodes.append(self.node(24, "", "android.widget.FrameLayout", b=[0, 0, W, H]))
        nodes.append(self.node(24, "0", "android.widget.TextView", text="Settings",
                               b=[56, 210, 700, 320], f=F_VISIBLE | F_ENABLED | F_FOCUSABLE))
        nodes.append(self.node(24, "1", "android.widget.EditText", text=self.search or None, desc="Search settings",
                               rid="com.android.settings:id/search_bar", b=[56, 360, W - 56, 500],
                               f=F_VISIBLE | F_ENABLED | F_CLICK | F_EDIT | F_FOCUSABLE | (F_FOCUSED if self.ime_for == "search" else 0)))
        if self.stack[-1] == "root":
            nodes.append(self.node(24, "2", "androidx.recyclerview.widget.RecyclerView",
                                   rid="com.android.settings:id/recycler", b=[0, 560, W, H],
                                   f=F_VISIBLE | F_ENABLED | F_SCROLL))
            rows = [r for r in SETTINGS_ROWS if not self.search or self.search.lower() in r.lower()]
            y0 = 560 + 60 - self.scroll
            for i, name in enumerate(rows):
                y = y0 + i * ROW_H
                if y + ROW_H < 400 or y > H:
                    continue
                path = f"2/{i}"
                wrap_f = F_VISIBLE | F_ENABLED | F_CLICK | (F_SCROLL if False else 0)
                if name == "Connections":
                    nodes.append(self.node(24, path, "android.widget.RelativeLayout", b=[0, y, W, y + ROW_H], f=F_VISIBLE | F_ENABLED))
                    nodes.append(self.node(24, f"{path}/0", "android.widget.TextView", text="Connections",
                                           b=[56, y + 40, 900, y + 110]))
                    nodes.append(self.node(24, f"{path}/1", "android.widget.Switch", desc="Wi‑Fi",
                                           rid="com.android.settings:id/switch_widget", value="1" if self.wifi else "0",
                                           b=[W - 260, y + 40, W - 56, y + 120],
                                           f=F_VISIBLE | F_ENABLED | F_CLICK | F_CHECKABLE | (F_CHECKED if self.wifi else 0) | F_FOCUSABLE))
                else:
                    nodes.append(self.node(24, path, "android.widget.RelativeLayout", text=None,
                                           b=[0, y, W, y + ROW_H], f=wrap_f))
                    nodes.append(self.node(24, f"{path}/0", "android.widget.TextView", text=name,
                                           b=[56, y + 40, 900, y + 110]))
        elif self.stack[-1] == "About phone":
            nodes.append(self.node(24, "2", "androidx.recyclerview.widget.RecyclerView",
                                   rid="com.android.settings:id/recycler", b=[0, 560, W, H],
                                   f=F_VISIBLE | F_ENABLED | F_SCROLL))
            for i, name in enumerate(ABOUT_ITEMS):
                y = 620 + i * ROW_H
                nodes.append(self.node(24, f"2/{i}", "android.widget.RelativeLayout", b=[0, y, W, y + ROW_H],
                                       f=F_VISIBLE | F_ENABLED | F_CLICK))
                nodes.append(self.node(24, f"2/{i}/0", "android.widget.TextView", text=name, b=[56, y + 40, 900, y + 110]))
        else:
            nodes.append(self.node(24, "2", "android.widget.TextView", text=f"{self.stack[-1]} screen",
                                   b=[56, 620, 900, 700]))
        if self.ime_for == "search":
            nodes.append(self.node(77, "", "android.inputmethodservice.KeyboardView", b=[0, H - 520, W, H]))
            nodes.append(self.node(77, "0", "android.widget.Button", desc="Search", rid="ime:action",
                                   b=[W - 300, H - 480, W - 20, H - 320],
                                   f=F_VISIBLE | F_ENABLED | F_CLICK | F_FOCUSABLE))
        for n in nodes:
            n["sig"] = self.sig(n)
        return self.windows(), nodes

    def visible_ids(self):
        wins, nodes = self.tree()
        return {n["id"] for n in nodes if n["f"] & F_VISIBLE}

    # ── rpc methods ───────────────────────────────────────────────────
    def rpc(self, method, p):
        if method == "status":
            return {"app": "axdroid-sim", "version": "0.1.0",
                    "device": {"model": "Pixel Sim", "os": "Android 16 (sim)", "sdk": 36,
                               "display": {"w": W, "h": H, "density": 3}},
                    "foreground": self.foreground, "policy": self.policy,
                    "custody": "stopped(sim)" if self.stopped else "running",
                    "service": {"connected": True, "lan": False, "port": 0,
                                "changes": self.changes, "quiet_ms": self.quiet_ms()}}
        if self.stopped and method not in ("observe", "changes", "apps", "status", "journal", "resume"):
            return {"ok": False, "error": {"code": "STOPPED_BY_OWNER",
                                           "message": "the owner stopped the bridge (sim)"}}
        if method == "observe":
            wins, nodes = self.tree()
            self.state_seq += 1
            return {"state": self.state_seq, "windows": wins, "nodes": nodes,
                    "count": len(nodes), "truncated": False,
                    "changes": self.changes, "ms": 55}
        if method == "changes":
            return {"count": self.changes, "quiet_ms": self.quiet_ms()}
        if method == "apps":
            return {"apps": [{"pkg": "com.android.settings", "name": "Settings"},
                             {"pkg": "com.android.chrome", "name": "Chrome"},
                             {"pkg": "com.example.canvas", "name": "Canvas Game"}]}
        if method == "act":
            return self.act(p)
        if method == "type":
            return self.type(p)
        if method == "wait":
            return self.wait(p)
        if method == "launch":
            self.launch_count[p["pkg"]] = self.launch_count.get(p["pkg"], 0) + 1
            self.foreground = p["pkg"]
            if p["pkg"] == "com.android.settings":
                self.stack = ["root"]
                self.search = ""
                self.scroll = 0
            self.bump(3)
            return {"ok": True, "pkg": p["pkg"], "ms": 220}
        if method == "open_uri":
            self.foreground = "com.android.chrome"
            self.bump(2)
            return {"ok": True}
        if method == "clipboard":
            return {"ok": True, "text": ""}
        if method == "stop":
            self.stopped = True
            self.journal.append({"type": "owner_stop"})
            return {"ok": True, "custody": "stopped(rpc)"}
        if method == "resume":
            self.stopped = False
            self.journal.append({"type": "owner_resume"})
            return {"ok": True, "custody": "running"}
        if method == "journal":
            return {"events": self.journal[-p.get("n", 50):]}
        if method == "policy_get":
            return self.policy
        if method == "policy_set":
            self.policy.update({k: p[k] for k in ("guard_mode", "allow_apps", "deny_apps") if k in p})
            return self.policy
        if method == "screenshot":
            if not self.policy.get("screenshots", True):
                return {"ok": False, "error": {"code": "SCREENSHOTS_OFF", "message": "the owner turned screenshots off (sim)"}}
            _wins, nodes = self.tree()
            readable = sum(1 for n in nodes if (n.get("text") or n.get("desc") or "").strip())
            if not p.get("force") and readable >= 8:
                return {"ok": False, "error": {"code": "SCREENSHOT_NOT_NEEDED",
                                               "message": f"This screen has {readable} readable elements: use phone_observe"}}
            png = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
            return {"ok": True, "image_b64": png, "mime": "image/png", "w": 1, "h": 1, "bytes": 70}
        return {"ok": False, "error": {"code": "UNKNOWN_METHOD", "message": method}}

    def classify(self, name):
        if not name:
            return None
        name = name.replace("’", "'").replace("‘", "'")
        if SAFE_RE.match(name):
            return None
        for kind, rx in RISK_RE:
            if rx.search(name):
                return kind
        return None

    def guard(self, p, node):
        name = (node or {}).get("text") or (node or {}).get("desc") if node else p.get("_name")
        kind = self.classify(name)
        if kind is None or self.policy["guard_mode"] == "allow":
            return None
        if self.policy["guard_mode"] == "block":
            return {"ok": False, "error": {"code": "BLOCKED_BY_POLICY", "kind": kind, "element": name}}
        if not p.get("confirm"):
            return {"ok": False, "error": {"code": "NEEDS_CONFIRMATION", "kind": kind, "element": name,
                                           "message": f"'{name}' looks like a {kind} action. Ask the user, then repeat with confirm:true."}}
        return None

    def find_node(self, nid):
        _, nodes = self.tree()
        for n in nodes:
            if n["id"] == nid:
                return n
        return None

    def act(self, p):
        op = p.get("op")
        with self.lock:
            if op in ("click", "long_click"):
                node = self.find_node(p.get("id")) if p.get("id") else None
                if p.get("id") and node is None:
                    return {"ok": False, "error": {"code": "STALE_REF", "message": "gone (sim)"}}
                if node is None:  # blind coordinate tap
                    x, y = p.get("x", -1), p.get("y", -1)
                    hit = None
                    if not self.dialog:
                        _, nodes = self.tree()
                        for n in nodes:
                            b = n.get("b")
                            if b and b[0] <= x <= b[2] and b[1] <= y <= b[3] and n["f"] & F_VISIBLE:
                                # only a *named* element counts; an unnamed root/container is a blind tap
                                if n.get("text") or n.get("desc") or n.get("rid"):
                                    hit = n
                    if hit is None:
                        if not p.get("confirm"):
                            return {"ok": False, "error": {"code": "NEEDS_CONFIRMATION", "kind": "blind_tap",
                                                           "message": "no element can be named at the point (sim)"}}
                        self.bump()
                        return {"ok": True, "via": "gesture"}
                    refusal = self.guard(p, hit)
                    if refusal:
                        return refusal
                    return self.click_node(hit)
                refusal = self.guard(p, node)
                if refusal:
                    return refusal
                # occlusion: a dialog window sits above the app
                if self.dialog and not node["id"].startswith("w91:"):
                    coverer = self.find_node("w91:0")
                    return {"ok": False, "error": {"code": "OCCLUDED",
                                                   "message": "the tap point is covered by another element",
                                                   "facts": {"coverer": {"text": coverer and coverer.get("text")}}}}
                return self.click_node(node)
            if op == "scroll_fwd":
                self.scroll += 900
                self.bump(2)
                return {"ok": True, "via": "ax-scroll"}
            if op == "scroll_back":
                self.scroll = max(0, self.scroll - 900)
                self.bump(2)
                return {"ok": True, "via": "ax-scroll"}
            if op == "swipe":
                self.bump(2)
                return {"ok": True, "via": "gesture"}
            if op == "global":
                name = p.get("name")
                if name == "back":
                    if self.dialog:
                        self.dialog = None
                    elif self.ime_for:
                        self.ime_for = None
                    elif self.search:
                        self.search = ""        # back collapses an active search (Settings behaviour)
                    elif len(self.stack) > 1:
                        self.stack.pop()
                    self.bump(2)
                    return {"ok": True, "via": "global"}
                if name in ("home", "recents", "notifications", "quick_settings", "power_dialog", "lock", "take_screenshot"):
                    self.bump(1)
                    return {"ok": True, "via": "global"}
                return {"ok": False, "error": {"code": "BAD_REQUEST", "message": f"unknown global {name}"}}
            if op == "ime_submit":
                if self.ime_for == "search":
                    self.ime_for = None
                    self.bump(2)
                    return {"ok": True, "via": "ax-ime-enter"}
                return {"ok": False, "error": {"code": "SUBMIT_FAILED", "message": "no focused field (sim)"}}
            return {"ok": False, "error": {"code": "BAD_REQUEST", "message": f"unknown op {op}"}}

    def click_node(self, node):
        rid = node.get("rid") or ""
        text = node.get("text") or node.get("desc") or ""
        if rid == "com.android.settings:id/switch_widget":
            self.wifi = not self.wifi
            self.bump(1)
            return {"ok": True, "via": "ax-click"}
        if rid == "com.android.settings:id/search_bar":
            self.ime_for = "search"
            self.bump(1)
            return {"ok": True, "via": "ax-click"}
        if rid.startswith("android:id/button"):
            if self.dialog:
                self.dialog = None
                self.bump(2)
            return {"ok": True, "via": "ax-click"}
        if text in SETTINGS_ROWS:
            self.stack.append(text)
            self.scroll = 0
            self.bump(3)
            return {"ok": True, "via": "ax-click"}
        if text in ABOUT_ITEMS:
            self.bump(1)
            return {"ok": True, "via": "ax-click"}
        self.bump(1)
        return {"ok": True, "via": "ax-click"}

    def type(self, p):
        with self.lock:
            node = self.find_node(p.get("id")) if p.get("id") else None
            if node is None or not node["f"] & F_EDIT:
                return {"ok": False, "error": {"code": "NOT_EDITABLE", "message": "not an editable field (sim)"}}
            expected = p["text"] if p.get("replace", True) else (self.search + p["text"])
            self.search = expected
            self.typed_log.append(expected)
            self.bump(2)
            out = {"ok": True, "via": "ax-set-text", "value": expected, "verified": True}
            if p.get("submit"):
                self.ime_for = None
                self.bump(2)
                out["submit_via"] = "ax-ime-enter"
            return out

    def wait(self, p):
        text, gone, timeout = p.get("text"), bool(p.get("gone")), p.get("timeout", 8)
        stable = p.get("stable", 0)
        deadline = time.monotonic() + min(25, timeout)
        t0 = time.monotonic()
        while time.monotonic() < deadline:
            _, nodes = self.tree()
            hit = text is None or any(
                text.lower() in (n.get("text") or "").lower() or text.lower() in (n.get("desc") or "").lower()
                for n in nodes if n["f"] & F_VISIBLE)
            if hit != gone and (stable <= 0 or self.quiet_ms() >= stable * 1000):
                return {"ok": True, "matched": True, "elapsed_ms": int((time.monotonic() - t0) * 1000),
                        "changes": self.changes}
            time.sleep(0.05)
        return {"ok": False, "timeout": True, "elapsed_ms": int((time.monotonic() - t0) * 1000),
                "changes": self.changes}


class SimHandler(BaseHTTPRequestHandler):
    phone: SimPhone = None  # set by serve_sim

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/healthz":
            body = json.dumps({"ok": True, "app": "axdroid-sim", "version": "0.1.0"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if self.path != "/rpc":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
            data = self.phone.rpc(req.get("method"), req.get("params") or {})
            if "ok" in data and data["ok"] is False:
                out = {"ok": False, "error": data.get("error")}
                status = 200
            else:
                out = {"ok": True, "data": data}
                status = 200
        except Exception as e:
            out = {"ok": False, "error": {"code": "INTERNAL", "message": str(e)}}
            status = 200
        body = json.dumps(out).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve_sim(phone: SimPhone | None = None, host: str = "127.0.0.1", port: int = 0):
    """Start the simulated phone on a background HTTP server → (phone, Phone client, shutdown)."""
    from .client import Phone
    ph = phone or SimPhone()
    SimHandler.phone = ph
    srv = ThreadingHTTPServer((host, port), SimHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    client = Phone(f"http://{host}:{srv.server_address[1]}")
    return ph, client, srv.shutdown
