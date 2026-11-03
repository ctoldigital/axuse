"""axdroid — element model: the Android accessibility dump as Nodes.

The app returns a flat node list (see app/src/…/TreeDump.java):

    {"id": "w24:0/3/2",       # windowId + child-index path — the act handle
     "d": 3,                  # depth
     "cls": "android.widget.Switch",
     "text": "Wi‑Fi", "desc": null, "rid": "com.android.settings:id/switch",
     "b": [l, t, r, b],       # screen px
     "f": 1088,               # flags bitmask (below)
     "value": "1"|None,       # synthesized for checkables
     "sig": "1a2b3c4d"}       # content identity (role|rid|text|desc|checked)

Everything here works on Node objects built from that list. Roles are derived
host-side (single implementation, testable offline); the device stays a dump.
"""

from __future__ import annotations

# flag bits — must match TreeDump.java
F_CLICK = 1
F_LONG = 2
F_SCROLL = 4
F_EDIT = 8
F_CHECKABLE = 16
F_CHECKED = 32
F_ENABLED = 64
F_SELECTED = 128
F_FOCUSABLE = 256
F_FOCUSED = 512
F_VISIBLE = 1024
F_PASSWORD = 2048

# class-name → role (suffix/contains match, longest first)
_CLASS_ROLES = (
    ("ImageButton", "image-button"),
    ("MaterialButton", "button"),
    ("AppCompatButton", "button"),
    ("Button", "button"),
    ("CheckBox", "checkbox"),
    ("SwitchCompat", "switch"),
    ("Switch", "switch"),
    ("ToggleButton", "switch"),
    ("RadioButton", "radio"),
    ("CheckedTextView", "cell"),
    ("EditText", "textfield"),
    ("TextInputEditText", "textfield"),
    ("SeekBar", "slider"),
    ("ProgressBar", "progress"),
    ("Spinner", "picker"),
    ("WebView", "webview"),
    ("RecyclerView", "list"),
    ("ListView", "list"),
    ("NestedScrollView", "scroll"),
    ("ScrollView", "scroll"),
    ("ViewPager2", "pager"),
    ("ViewPager", "pager"),
    ("Toolbar", "navbar"),
    ("ActionBarContainer", "navbar"),
    ("TabView", "tab"),
    ("TabItem", "tab"),
    ("ImageView", "image"),
    ("TextView", "text"),
)

ACTIONABLE = frozenset({
    "button", "image-button", "checkbox", "switch", "radio", "textfield",
    "cell", "tab", "picker", "webview", "link",
})
CONTAINERS = frozenset({"group", "list", "scroll", "navbar", "tabbar", "alert", "keyboard", "app", "window", "pager"})
TEXT_ROLES = frozenset({"textfield"})
FRAMEWORKS = (
    ("flutter", ("FlutterView", "io.flutter")),
    ("react-native", ("ReactTextView", "com.facebook.react", "ReactViewGroup")),
    ("webview", ("WebView", "chromium.WebView")),
    ("compose", ("ComposeView", "androidx.compose", "ComposeNode")),
    ("unity", ("UnityPlayer",)),
)


def class_base(cls: str | None) -> str:
    return (cls or "").rsplit(".", 1)[-1]


def infer_role(cls: str | None, flags: int) -> str:
    """Class-first role mapping, like axphone — with the clickable bit as tiebreak."""
    base = class_base(cls)
    full = cls or ""
    if base.endswith("EditText"):
        return "textfield"
    if base.endswith("Button") or "Button" in base:
        return "image-button" if "Image" in base else "button"
    for needle, role in _CLASS_ROLES:
        if needle in full:
            return role
    if flags & F_EDIT:
        return "textfield"
    if flags & F_SCROLL:
        return "scroll"
    if flags & F_CLICK:
        # clickable containers without a widget class: menus, cards, custom cells
        return "cell" if (flags & F_VISIBLE) and not (flags & F_LONG) else "group"
    return "group"


def detect_framework(clses: set[str]) -> list[str]:
    out = []
    for name, needles in FRAMEWORKS:
        if any(any(n in c for n in needles) for c in clses):
            out.append(name)
    return out


class Node:
    __slots__ = ("id", "depth", "cls", "text", "desc", "rid", "bounds", "flags",
                 "value", "hint", "sig", "role", "parent", "children", "ref", "win")

    def __init__(self, raw: dict, win: dict | None):
        self.id = raw["id"]
        self.depth = int(raw.get("d", 0))
        self.cls = raw.get("cls")
        self.text = raw.get("text")
        self.desc = raw.get("desc")
        self.rid = raw.get("rid")
        self.bounds = tuple(raw["b"]) if raw.get("b") else None
        self.flags = int(raw.get("f", 0))
        self.value = raw.get("value")
        self.hint = raw.get("hint")
        self.sig = raw.get("sig")
        self.role = infer_role(self.cls, self.flags)
        self.win = win or {}
        self.parent = None
        self.children: list[str] = []
        self.ref: str | None = None

    # parent id from the path (w12:0/3 → w12:0); window roots have no parent
    @staticmethod
    def parent_of(nid: str) -> str | None:
        w, _, path = nid.partition(":")
        if "/" not in path:
            return None
        return w + ":" + path.rsplit("/", 1)[0]

    @property
    def name(self) -> str:
        return (self.text or self.desc or "").strip()

    @property
    def visible(self) -> bool:
        return bool(self.flags & F_VISIBLE)

    @property
    def enabled(self) -> bool:
        return bool(self.flags & F_ENABLED)

    @property
    def clickable(self) -> bool:
        return bool(self.flags & F_CLICK)

    @property
    def checked(self) -> bool | None:
        if self.flags & F_CHECKABLE:
            return bool(self.flags & F_CHECKED)
        return None

    @property
    def editable(self) -> bool:
        return bool(self.flags & F_EDIT)

    @property
    def selected(self) -> bool:
        return bool(self.flags & F_SELECTED)

    @property
    def actionable(self) -> bool:
        return self.role in ACTIONABLE or (self.clickable and self.role not in CONTAINERS)

    @property
    def centre(self) -> tuple[int, int] | None:
        if not self.bounds:
            return None
        l, t, r, b = self.bounds
        return (l + r) // 2, (t + b) // 2

    def identity(self) -> tuple:
        """Signature identity for diff fallback (recycled containers)."""
        return (self.role, self.rid or "", self.name[:60])


class Snapshot:
    """One observe result: windows, nodes in parent-before-child order."""

    def __init__(self, data: dict):
        self.state = data.get("state", 0)
        self.changes = data.get("changes", -1)
        self.truncated = bool(data.get("truncated"))
        self.dump_ms = data.get("ms")
        self.windows = data.get("windows", [])
        actives = [w for w in self.windows if w.get("active")]
        self.app = (actives[0].get("pkg") if actives else
                    (self.windows[0].get("pkg") if self.windows else None))
        self.nodes: dict[str, Node] = {}
        self.order: list[str] = []
        wmap = {w.get("id"): w for w in self.windows}
        for raw in data.get("nodes", []):
            n = Node(raw, wmap.get(int(raw["id"].split(":", 1)[0][1:])) if ":" in raw["id"] else None)
            self.nodes[n.id] = n
            self.order.append(n.id)
        for n in self.nodes.values():
            p = Node.parent_of(n.id)
            n.parent = p if p in self.nodes else None
            if n.parent:
                self.nodes[n.parent].children.append(n.id)

    @property
    def screen(self) -> tuple[int, int] | None:
        if not self.windows:
            return None
        maxb = None
        for w in self.windows:
            b = w.get("bounds")
            if not b:
                continue
            area = (b[2] - b[0]) * (b[3] - b[1])
            if maxb is None or area > maxb[0]:
                maxb = (area, (b[2], b[3]))
        return maxb[1] if maxb else None

    def overlay_windows(self) -> list[dict]:
        """Alerts/dialogs/IME: non-active windows above the active one."""
        seen_active = False
        out = []
        for w in self.windows:  # topmost first from the dump
            if w.get("active"):
                seen_active = True
            elif not seen_active and (w.get("overlay") or w.get("ime")):
                out.append(w)
        return out


# ── rendering ────────────────────────────────────────────────────────────────

def _clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _state_mark(n: Node) -> str:
    c = n.checked
    parts = []
    if c is not None:
        parts.append("on" if c else "off")
    if n.value and n.role in TEXT_ROLES:
        parts.append(f"«{_clip(n.value, 40)}»")
    if not n.text and n.hint and n.role in TEXT_ROLES:
        parts.append(f'empty hint="{_clip(n.hint, 32)}"')
    if n.selected and c is None:
        parts.append("selected")
    if not n.enabled:
        parts.append("disabled")
    return (" " + " ".join(parts)) if parts else ""


# windows whose content is phone chrome: hidden by default (the status bar's
# clock/battery/icons and the nav bar cost ~50 nodes on every One UI observe;
# nav actions are global anyway). phone_find and refs still see them.
CHROME_PKGS = ("com.android.systemui",)


def _short_rid(rid: str | None) -> str:
    """com.android.settings:id/search_bar → search_bar (package noise gone)."""
    if not rid:
        return ""
    return rid.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def render(snap: Snapshot, *, mode: str = "full", max_lines: int = 220,
           screen: tuple[int, int] | None = None, prefix_app: str | None = None,
           chrome: bool = False, compact_lists: bool = True) -> str:
    """Compact tree text: `- switch "Wi-Fi" on [ref=e12]`.

    Token economy (measured on One UI; see tools/measure-tokens.py):
      * chrome windows are hidden unless chrome=True (one note line instead)
      * resource ids lose their package prefix
      * runs of ≥4 same-role leaf siblings render 2 + "×N more" — homogeneous
        list rows are the biggest per-observe cost, and phone_find/scroll
        address them better than raw repetition
    """
    lines = []
    if prefix_app:
        lines.append(prefix_app)
    hidden_chrome = 0
    compacted = 0

    def keep(n: Node) -> bool:
        if mode == "lite":
            return n.actionable and (n.name or n.role in ("textfield", "switch", "checkbox")) or n.role == "navbar"
        return bool(n.name or n.actionable or n.role in ("heading", "navbar", "alert", "keyboard") or n.rid)

    kept = [nid for nid in snap.order
            if snap.nodes[nid].visible and keep(snap.nodes[nid])]

    i = 0
    while i < len(kept):
        nid = kept[i]
        n = snap.nodes[nid]
        if not chrome and n.win.get("pkg") in CHROME_PKGS:
            hidden_chrome += 1
            i += 1
            continue
        # homogeneous run: ≥4 consecutive same-parent, same-role leaf rows
        if compact_lists and n.role not in CONTAINERS:
            j = i
            run = []
            while j < len(kept):
                m = snap.nodes[kept[j]]
                if m.parent != n.parent or m.role != n.role or m.win.get("pkg") in CHROME_PKGS and not chrome:
                    break
                run.append(kept[j])
                j += 1
            if len(run) >= 4:
                for k in run[:2]:
                    lines.append(_line(snap.nodes[k], screen, mode))
                rest = len(run) - 2
                compacted += rest
                lines.append(f"{'  ' * min(snap.nodes[run[0]].depth, 24)}… ×{rest} more {n.role} rows "
                             f"(phone_find finds them; phone_scroll reveals)")
                i = j
                if len(lines) >= max_lines:
                    break
                continue
        lines.append(_line(n, screen, mode))
        if len(lines) >= max_lines:
            lines.append(f"… ({len(snap.order)} nodes total, truncated view)")
            break
        i += 1

    if hidden_chrome and not chrome:
        lines.append(f"(chrome hidden: {hidden_chrome} status/nav-bar nodes — observe with chrome:true to include)")
    if compacted:
        lines.append(f"({compacted} homogeneous rows compacted)")
    return "\n".join(lines)


def _line(n: Node, screen, mode) -> str:
    label = _clip(n.name or "", 48) or None
    head = f'{n.role} "{label}"' if label else n.role
    st = _state_mark(n)
    rid = f" @{_short_rid(n.rid)}" if n.rid and mode == "full" else ""
    ref = f" [ref={n.ref}]" if n.ref else ""
    mark = ""
    if screen and n.bounds:
        h = screen[1]
        if n.bounds[3] <= int(h * 0.03):
            mark = " ·above"
        elif n.bounds[1] >= int(h * 0.97):
            mark = " ·below"
    return f"{'  ' * min(n.depth, 24)}- {head}{st}{rid}{ref}{mark}"


def _norm(s: str) -> str:
    """Match text the way a human means it: Unicode hyphens/quotes/spaces
    fold to ASCII ('Wi‑Fi' == 'Wi-Fi', 'Don’t' == \"Don't\")."""
    s = (s or "").replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-") \
        .replace("\u2019", "'").replace("\u2018", "'").replace("\u00a0", " ")
    return " ".join(s.split()).lower()


def _match_tier(n: "Node", low: str) -> int:
    """3 = exact name, 2 = name starts with it, 1 = substring anywhere.
    'Display' must not resolve to 'Always On Display' (measured on One UI)."""
    name = _norm(n.name)
    if name == low:
        return 3
    if name.startswith(low):
        return 2
    if low in _norm(n.text or "") or low in _norm(n.desc or ""):
        return 1
    return 0


def find(snap: Snapshot, text: str | None = None, role: str | None = None,
         rid: str | None = None, *, visible_only: bool = True, pkg: str | None = None) -> list[Node]:
    """pkg scopes the search to one app's windows — the status bar's battery
    icon must not answer for Settings' 'Battery' row (measured on One UI)."""
    out = []
    low = _norm(text) if text else None
    for nid in snap.order:
        n = snap.nodes[nid]
        if visible_only and not n.visible:
            continue
        if role and n.role != role:
            continue
        if rid and n.rid != rid:
            continue
        if pkg and (not n.win or n.win.get("pkg") != pkg):
            continue
        if low:
            name = _norm(n.name)
            if low not in name and low not in _norm(n.text or "") and low not in _norm(n.desc or ""):
                continue
        out.append(n)
    return out


def best_hits(snap: Snapshot, text: str, *, visible_only: bool = True) -> list[Node]:
    """find() + tier ranking: exact-name matches outrank prefix, prefix
    outranks substring. Returns the best tier only."""
    low = _norm(text)
    hits = find(snap, text, visible_only=visible_only)
    if not hits:
        return []
    tiers = [_match_tier(n, low) for n in hits]
    best = max(tiers)
    if best < 1:
        return []
    return [n for n, t in zip(hits, tiers) if t == best]
