"""axdroid — the session: observe → resolve → act (guarded, verified on the
device) → settle on the device's own change counter → diff keyed by refs.

Same action contract as axphone, different division of labour: the *phone*
holds the guard, the custody latch, the journal and the semantic executor
(performAction with gesture fallback + occlusion check), because the phone is
the hub — every transport gets those guarantees for free. This host side adds
what benefits from being central: stable refs, ref-keyed diffs, find, plans
(phone_run), and the MCP surface.
"""

from __future__ import annotations

import time

from . import __version__
from .client import Phone, PhoneError
from .model import Snapshot, find, render

# common names → packages (AOSP + Samsung/One UI + Pixel variants)
KNOWN_APPS = {
    "settings": "com.android.settings",
    "phone": "com.android.dialer",
    "dialer": "com.android.dialer",
    "messages": "com.google.android.apps.messaging",
    "sms": "com.google.android.apps.messaging",
    "contacts": "com.android.contacts",
    "camera": "com.android.camera2",
    "gallery": "com.google.android.apps.photos",
    "photos": "com.google.android.apps.photos",
    "browser": "com.android.chrome",
    "chrome": "com.android.chrome",
    "calculator": "com.google.android.calculator",
    "clock": "com.google.android.deskclock",
    "calendar": "com.google.android.calendar",
    "mail": "com.google.android.gm",
    "gmail": "com.google.android.gm",
    "maps": "com.google.android.apps.maps",
    "youtube": "com.google.android.youtube",
    "play store": "com.android.vending",
    "files": "com.android.documentsui",
    "documents": "com.android.documentsui",
    "files by google": "com.google.android.apps.nbu.files",
    "notes": "com.google.android.keep",
    "keep": "com.google.android.keep",
    "whatsapp": "com.whatsapp",
    "telegram": "org.telegram.messenger",
    # android_world benchmark apps (google-research/android_world)
    "markor": "net.gsantner.markor",
    "osmand": "net.osmand.plus",
    "tasks": "org.tasks",
    "tasks app": "org.tasks",
    "opentracks": "de.dennisguse.opentracks",
    "open tracks": "de.dennisguse.opentracks",
    "sports tracker": "de.dennisguse.opentracks",
    "activity tracker": "de.dennisguse.opentracks",
    "joplin": "net.cozic.joplin",
    "vlc": "org.videolan.vlc",
    "vlc player": "org.videolan.vlc",
    "retro music": "code.name.monkey.retromusic",
    "retro player": "code.name.monkey.retromusic",
    "pro expense": "com.arduia.expense",
    "expense": "com.arduia.expense",
    "broccoli": "com.flauschcode.broccoli",
    "recipe app": "com.flauschcode.broccoli",
    "audio recorder": "com.dimowner.audiorecorder",
    "simple calendar": "com.simplemobiletools.calendar.pro",
    "simple calendar pro": "com.simplemobiletools.calendar.pro",
    "simple gallery": "com.simplemobiletools.gallery.pro",
    "simple gallery pro": "com.simplemobiletools.gallery.pro",
    "simple draw": "com.simplemobiletools.draw.pro",
    "simple draw pro": "com.simplemobiletools.draw.pro",
    "simple sms": "com.simplemobiletools.smsmessenger",
    "simple sms messenger": "com.simplemobiletools.smsmessenger",
    "sms messenger": "com.simplemobiletools.smsmessenger",
}

BLOCK_NOTICES = (
    ("too many requests", "rate-limited by the service"),
    ("unusual traffic", "bot check (unusual traffic)"),
    ("verify you're human", "bot check"),
    ("are you a robot", "bot check"),
    ("请求过于频繁", "rate-limited by the service"),
    ("人机验证", "bot check"),
)


class Session:
    def __init__(self, phone: Phone, *, settle_quiet: float = 0.12, settle_max: float = 2.5):
        self.p = phone
        self.snap: Snapshot | None = None
        self._snap_changes = -1
        self._next_ref = 1
        self.refs: dict[str, str] = {}       # id → ref
        self.ref_meta: dict[str, tuple] = {} # ref → content identity (self-heal)
        self.latency = __import__("axdroid.client", fromlist=["Latency"]).Latency()
        self.settle_quiet = settle_quiet
        self.settle_max = settle_max
        self.info: dict = {}
        self.started = False

    async def start(self):
        if not self.started:
            self.info = self.p.call("status")
            self.started = True
        return self.info

    # ── observe & refs ────────────────────────────────────────────────

    def observe(self, mode: str = "full", max_lines: int = 220, *, fresh: bool = False,
                chrome: bool = False) -> dict:
        t0 = time.perf_counter()
        data = self.p.call("observe")
        snap = Snapshot(data)
        self._assign_refs(snap)
        self.snap = snap
        self._snap_changes = snap.changes
        ms = (time.perf_counter() - t0) * 1000
        self.latency.record("observe", ms)
        screen = snap.screen or (0, 0)
        app_line = f"app: {snap.app} · {screen[0]}×{screen[1]}px · changes {snap.changes}"
        if snap.truncated:
            app_line += " · truncated"
        overlays = [w.get("pkg") for w in snap.overlay_windows()]
        if overlays:
            app_line += f" · overlay: {', '.join(str(p) for p in overlays)}"
        text = render(snap, mode=mode, max_lines=max_lines, screen=screen, prefix_app=app_line, chrome=chrome)
        for needle, why in BLOCK_NOTICES:
            if any(needle in (n.text or "").lower() or needle in (n.desc or "").lower()
                   for n in snap.nodes.values() if n.visible):
                text += f"\nnotice: {why} — stop and tell the user; never retry around it."
                break
        return {"text": text, "snap": snap, "ms": round(ms, 1), "app": snap.app}

    def current(self) -> Snapshot:
        """Reuse the cached tree if nothing changed since the last observe."""
        if self.snap is None:
            self.observe()
            assert self.snap
            return self.snap
        ch = self.p.call("changes")
        if ch.get("count") == self._snap_changes and ch.get("quiet_ms", 999) >= 150:
            return self.snap
        self.observe()
        assert self.snap
        return self.snap

    def _assign_refs(self, snap: Snapshot):
        """Refs stay stable while the element id survives; a *recycled* element
        (RecyclerView re-bound the row: new path id, same content identity)
        inherits its predecessor's ref — axphone's registry idea, adapted."""
        live = set(snap.nodes)
        # identity of the outgoing ids, so recycled rows can reclaim their refs
        old_identity = {}
        if self.snap is not None:
            for nid, n in self.snap.nodes.items():
                if nid not in live:
                    old_identity.setdefault(n.identity(), nid)
        for gone in [i for i in self.refs if i not in live]:
            del self.refs[gone]
        for nid in snap.order:
            if nid not in self.refs:
                prev = old_identity.get(snap.nodes[nid].identity())
                if prev is not None and prev in self.refs:
                    self.refs[nid] = self.refs.pop(prev)
                else:
                    self.refs[nid] = f"e{self._next_ref}"
                    self._next_ref += 1
            snap.nodes[nid].ref = self.refs[nid]
            self.ref_meta[self.refs[nid]] = snap.nodes[nid].identity()

    # ── resolve ───────────────────────────────────────────────────────

    def resolve(self, target, *, snap: Snapshot | None = None) -> tuple[str, str]:
        """target → (node id, human name). Ambiguity is an error, never a guess."""
        snap = snap or self.current()
        if isinstance(target, str):
            if target.startswith("e") and target[1:].isdigit():
                for nid, n in snap.nodes.items():
                    if self.refs.get(nid) == target:
                        return nid, n.name or n.rid or target
                # self-heal: the row was re-bound (new path id, same content)
                ident = self.ref_meta.get(target)
                if ident is not None:
                    hits = [n for n in snap.nodes.values() if n.identity() == ident and n.visible]
                    if hits:
                        return hits[0].id, hits[0].name or target
                raise PhoneError("STALE_REF", f"ref {target} is not in the current tree — observe again", retry="observe")
            from .model import best_hits
            hits = best_hits(snap, target)
            hits = [h for h in hits if h.actionable] or hits
            if len(hits) > 1 and snap.app:
                # matches in the foreground app win over the launcher behind it
                # or system chrome (measured: One UI's background windows answer
                # for app rows and create false AMBIGUOUS)
                fg = [h for h in hits if h.win.get("pkg") == snap.app]
                if fg:
                    hits = fg
            if not hits:
                raise PhoneError("NOT_FOUND", f"no element matching '{target}' on screen")
            if len(hits) > 1:
                # one clickable switch/button inside its own labelled row is not ambiguity
                pick = self._innermost(hits)
                if len(pick) == 1:
                    n = pick[0]
                    return n.id, n.name or target
                cands = ", ".join(f"{n.ref or '-'} {n.role} \"{(n.name or '')[:40]}\"" for n in pick[:6])
                raise PhoneError("AMBIGUOUS", f"'{target}' matches {len(hits)} elements: {cands}")
            return hits[0].id, hits[0].name or target
        if isinstance(target, dict):
            hits = find(snap, target.get("text"), target.get("role"), target.get("rid"))
            if target.get("ref"):
                hits = [n for n in hits if self.refs.get(n.id) == target["ref"]] or \
                       [n for n in snap.nodes.values() if self.refs.get(n.id) == target["ref"]]
            if target.get("nth") is not None:
                if not 0 <= target["nth"] < len(hits):
                    raise PhoneError("NOT_FOUND", f"nth={target['nth']} out of {len(hits)} matches")
                hits = [hits[target["nth"]]]
            if not hits:
                raise PhoneError("NOT_FOUND", f"no element matching {target}")
            if len(hits) > 1:
                pick = self._innermost(hits)
                if len(pick) > 1:
                    cands = ", ".join(f"{n.ref or '-'} {n.role} \"{(n.name or '')[:40]}\"" for n in pick[:6])
                    raise PhoneError("AMBIGUOUS", f"{len(hits)} matches: {cands}")
                hits = pick
            return hits[0].id, hits[0].name or str(target)
        raise PhoneError("BAD_TARGET", f"cannot resolve target {target!r}")

    @staticmethod
    def _innermost(hits):
        """Deepest match wins: a row and its own switch sharing a label."""
        from .model import Node
        ids = {h.id for h in hits}
        out = []
        for h in hits:
            p = Node.parent_of(h.id)
            nested = False
            while p:
                if p in ids:
                    nested = True
                    break
                p = Node.parent_of(p)
            if not nested:
                out.append(h)
        return out if out else hits

    # ── settle & diff ─────────────────────────────────────────────────

    def settle(self, quiet: float | None = None, cap: float | None = None) -> dict:
        """Wait for the device's change counter to go still (event-driven)."""
        quiet = self.settle_quiet if quiet is None else quiet
        cap = self.settle_max if cap is None else cap
        t0 = time.perf_counter()
        last = None
        still_since = None
        while (time.perf_counter() - t0) < cap:
            ch = self.p.call("changes")
            c = ch.get("count")
            if c == last and (ch.get("quiet_ms") or 0) >= quiet * 1000:
                if still_since is None:
                    still_since = time.perf_counter()
                if time.perf_counter() - still_since >= quiet:
                    return {"ok": True, "quiet": True, "ms": round((time.perf_counter() - t0) * 1000, 1), "changes": c}
            else:
                still_since = None
            last = c
            time.sleep(0.05)
        return {"ok": False, "timeout": True, "ms": round((time.perf_counter() - t0) * 1000, 1), "changes": last}

    def settle_action(self, min_wait: float = 0.18) -> dict:
        """Post-action settle, tuned for latency (measured on One UI): the event
        burst after a click is short — return as soon as the counter is quiet
        for ~120 ms, within a 1 s early window. Late transitions are caught by
        the empty-diff re-check, not by waiting here."""
        time.sleep(min_wait)
        t0 = time.perf_counter()
        last = None
        while (time.perf_counter() - t0) < 1.0:
            ch = self.p.call("changes")
            if ch.get("count") == last and (ch.get("quiet_ms") or 0) >= 120:
                return {"ok": True, "quiet": True, "ms": round((time.perf_counter() - t0) * 1000, 1), "changes": ch.get("count")}
            last = ch.get("count")
            time.sleep(0.04)
        return {"ok": False, "timeout": True, "ms": round((time.perf_counter() - t0) * 1000, 1), "changes": last}

    def diff(self, before: Snapshot, after: Snapshot, max_lines: int = 40) -> dict:
        """Ref-keyed diff: identity by id, signature fallback for recycled rows."""
        old, new = before.nodes, after.nodes
        added, removed, changed = [], [], []
        for nid in after.order:
            n = new[nid]
            if nid not in old:
                added.append(n)
                continue
            o = old[nid]
            if o.sig != n.sig or o.role != n.role:
                changed.append((o, n))
        sig_index = {}
        for nid in before.order:
            sig_index.setdefault(old[nid].identity(), []).append(old[nid])
        truly_new = []
        for n in added:
            twins = sig_index.get(n.identity()) or []
            fresh_twin = [t for t in twins if t.id not in new]
            if fresh_twin:  # recycled container: treat as changed
                changed.append((fresh_twin[0], n))
            else:
                truly_new.append(n)
        removed_ids = [nid for nid in before.order if nid not in new]
        removed_nodes = [old[nid] for nid in removed_ids
                         if not any(old[nid].identity() == new[m].identity() for m in new)]
        lines = []
        for n in truly_new[:max_lines]:
            lines.append(f"+ {n.role} \"{(n.name or n.rid or '')[:48]}\" [ref={n.ref}]")
        for n in removed_nodes[:max_lines]:
            lines.append(f"- {n.role} \"{(n.name or n.rid or '')[:48]}\"")
        for o, n in changed[:max_lines]:
            lines.append(f"~ {n.role} \"{(n.name or o.name or '')[:48]}\" [ref={n.ref}]"
                         + (f" {o.checked}→{n.checked}" if (o.checked is not None and o.checked != n.checked) else ""))
        refs_kept = sum(1 for nid in new if nid in old)
        return {"text": "\n".join(lines) if lines else "(no visible change)",
                "added": len(truly_new), "removed": len(removed_nodes), "changed": len(changed),
                "refs_kept": refs_kept, "refs_total": len(new)}

    # ── actions ───────────────────────────────────────────────────────

    def _reveal(self, node, snap: Snapshot) -> Snapshot:
        """Bring an on-screen-but-below-the-fold node into the visible band by
        scrolling (a few swipes, identity-checked after each)."""
        ident = node.identity()
        screen_h = (snap.screen or (1080, 2340))[1]
        for _ in range(4):
            direction = "down" if (node.bounds and node.bounds[1] >= screen_h * 0.5) else "up"
            self.scroll(direction, max_swipes=1)
            snap = self.observe()["snap"]
            for n in snap.nodes.values():
                if n.identity() == ident and n.visible and n.bounds \
                        and n.bounds[1] < screen_h * 0.94 and n.bounds[3] > screen_h * 0.06:
                    return snap
        return snap

    def _act_then_diff(self, call_desc: str, *, op: str, **params) -> dict:
        t0 = time.perf_counter()
        before = self.current()
        r = self.p.call("act", op=op, **params)
        self.settle_action()
        after = self.observe()
        d = self.diff(before, after["snap"])
        ms = (time.perf_counter() - t0) * 1000
        self.latency.record(op, ms)
        return {"action": {**r, "target": params.get("_name") or params.get("id") or ""},
                "text": d["text"], "diff": d, "mode": "diff", "ms": round(ms, 1)}

    def tap(self, target=None, *, x=None, y=None, kind="tap", confirm=False, retry=True,
            auto_scroll: bool = True) -> dict:
        op = "long_click" if kind == "long_press" else "click"
        if target is not None:
            last = None
            for attempt in range(3):
                snap = self.current()
                try:
                    nid, name = self.resolve(target, snap=snap)
                except PhoneError as e:
                    if e.code in ("NOT_FOUND", "STALE_REF") and attempt < 2:
                        # off-screen rows are simply not in an Android tree:
                        # scroll looking for the text — down first, then up if
                        # a fling overshot past it (measured on One UI)
                        if auto_scroll and e.code == "NOT_FOUND" and isinstance(target, str) \
                                and not (target.startswith("e") and target[1:].isdigit()):
                            direction = "up" if attempt == 1 else "down"
                            try:
                                self.scroll(direction, until=str(target), max_swipes=6)
                            except PhoneError:
                                pass
                        else:
                            self.observe()
                        continue
                    raise
                # an on-screen-but-below-the-fold target: bring it into view first
                node = snap.nodes.get(nid)
                if auto_scroll and node is not None and node.bounds and snap.screen:
                    h = snap.screen[1]
                    if node.bounds[1] >= h * 0.94 or node.bounds[3] <= h * 0.06:
                        snap = self._reveal(node, snap)
                        fresh = [n for n in snap.nodes.values()
                                 if n.identity() == node.identity() and n.visible]
                        if not fresh:
                            continue
                        nid = fresh[0].id
                # act immediately with the id resolved from THIS snapshot: an
                # extra observe in between would invalidate the path id. The
                # signature hints let the device self-heal a re-bound row id.
                t0 = time.perf_counter()
                n0 = snap.nodes.get(nid)
                hints = {"sig": (n0.sig or "") if n0 else "",
                         "rid": (n0.rid or "") if n0 else "",
                         "text": ((n0.name or "")[:80]) if n0 else ""}
                try:
                    r = self.p.call("act", op=op, id=nid, confirm=confirm, **hints)
                except PhoneError as e:
                    if e.code == "STALE_REF" and attempt < 2:
                        time.sleep(0.4)          # a moving list re-binds rows; back off
                        self.observe()
                        continue
                    raise
                self.settle_action()
                after = self.observe()
                d = self.diff(snap, after["snap"])
                # measured on One UI: an activity transition can start emitting
                # events *after* the settle window — an empty diff right after a
                # successful act is not trustworthy; wait for stability and look again
                if not (d["added"] or d["changed"] or d["removed"]):
                    try:
                        self.p.call("wait", stable=0.5, timeout=3)
                    except PhoneError:
                        pass
                    after2 = self.observe()
                    d2 = self.diff(snap, after2["snap"])
                    if d2["added"] or d2["changed"] or d2["removed"]:
                        after, d = after2, d2
                ms = (time.perf_counter() - t0) * 1000
                self.latency.record(op, ms)
                return {"action": {**r, "target": name or target},
                        "text": d["text"], "diff": d, "mode": "diff", "ms": round(ms, 1)}
            raise last if last else PhoneError("INTERNAL", "tap retries exhausted")
        if x is None or y is None:
            raise PhoneError("BAD_TARGET", "tap needs target or x,y")
        return self._act_then_diff(f"{kind} @({x},{y})", op=op,
                                   x=float(x), y=float(y), confirm=confirm, _name=f"({x},{y})")

    def type(self, text: str, into=None, *, replace=True, submit=False, retry=True, at=None) -> dict:
        before = self.current()
        if into is not None:
            try:
                nid, name = self.resolve(into)
            except PhoneError as e:
                if e.code == "NOT_FOUND" and retry:
                    self.observe()
                    return self.type(text, into, replace=replace, submit=submit, retry=False, at=at)
                raise
            r = self.p.call("type", id=nid, text=text, replace=replace, submit=submit,
                            **({"at": at} if at else {}))
        else:
            r = self.p.call("type", text=text, replace=replace, submit=submit,
                            **({"at": at} if at else {}))
        self.settle_action()
        after = self.observe()
        d = self.diff(before, after["snap"])
        via = r.get("via", "ax-set-text")
        head = f"type · via {via}" + (" · verified" if r.get("verified") else "")
        action = {"op": "type", "target": into, "via": via, "ms": r.get("ms"),
                  "verified": bool(r.get("verified")), "value": r.get("value")}
        if r.get("submit_via"):
            action["submit_via"] = r["submit_via"]
        return {"action": action,
                "text": head + "\n" + d["text"], "diff": d, "mode": "diff"}

    # windows that must not satisfy until:/expect: (status bar, launchers)
    _CHROME_PKGS = ("com.android.systemui", "com.sec.android.app.launcher",
                    "com.google.android.apps.nexuslauncher",
                    "com.google.android.googlequicksearchbox")

    @staticmethod
    def _until_visible(snap, text: str):
        """Rows that satisfy until: exact-name visible matches win; if the
        exact row exists but is off-screen, substring matches don't count
        (measured: 'Display' matched 'Always On Display' while the Display
        row was still below the fold)."""
        from .model import _norm, find
        vis = find(snap, text=text, visible_only=True, pkg=snap.app) or \
              [n for n in find(snap, text=text, visible_only=True)
               if n.win.get("pkg") not in Session._CHROME_PKGS]
        low = _norm(text)
        exact_vis = [n for n in vis if _norm(n.name) == low]
        if exact_vis:
            return exact_vis
        any_exact = [n for n in snap.nodes.values() if _norm(n.name) == low]
        if any_exact:
            return []
        return vis

    def scroll(self, direction="down", target=None, until=None, max_swipes=8) -> dict:
        t0 = time.perf_counter()
        before = self.current()
        lines = []
        method = "ax"          # start semantic; follow what actually moves the list
        for i in range(max_swipes):
            # re-resolve the scroller every swipe: a list re-layout after the
            # previous scroll invalidates path ids (measured on a Samsung list)
            snap = self.current()
            nid = None
            scroller = None
            if target is not None:
                try:
                    nid, _ = self.resolve(target, snap=snap)
                except PhoneError:
                    nid = None
            if nid is None:
                scrollers = [n for n in snap.nodes.values() if n.role in ("list", "scroll") and n.visible]
                if scrollers:
                    scroller = max(scrollers, key=lambda n: (n.bounds[2] - n.bounds[0]) * (n.bounds[3] - n.bounds[1]) if n.bounds else 0)
                    nid = scroller.id
            if scroller is None and nid is not None:
                scroller = snap.nodes.get(nid)

            def visible_sigs(s):
                return frozenset(n.sig for n in s.nodes.values()
                                 if n.visible and n.name and n.win.get("pkg") == s.app)

            sigs_before = visible_sigs(snap)
            # One UI's main list accepts ACTION_SCROLL_FORWARD but does not move
            # (measured): when a swipe leaves the visible content unchanged, the
            # next swipe is a real gesture fling — and back to semantic once a
            # method demonstrably moves the list again.
            try:
                if method == "ax":
                    self.p.call("act", op="scroll_fwd" if direction in ("down", "right") else "scroll_back", id=nid)
                    self.settle()
                else:
                    b = scroller.bounds if (scroller and scroller.bounds) else (0, 0, *snap.screen)
                    h = b[3] - b[1]
                    x = (b[0] + b[2]) / 2
                    y1, y2 = ((b[1] + h * 0.72, b[1] + h * 0.28) if direction in ("down", "right")
                              else (b[1] + h * 0.28, b[1] + h * 0.72))
                    self.p.call("act", op="swipe", x=x, y=y1, dx=0, dy=y2 - y1, duration=280)
                    self.settle()
            except PhoneError as err:
                if err.code == "STALE_REF":
                    continue          # the list re-bound; the next swipe re-resolves
                raise
            snap = self.observe()["snap"]
            moved = visible_sigs(snap) != sigs_before
            if not moved:
                method = "fling" if method == "ax" else "ax"
            if until is not None:
                snap = self.observe()["snap"]
                hits = self._until_visible(snap, until)
                if hits:
                    # a fling keeps moving after the swipe: confirm the row is
                    # still there once the list has really stopped
                    try:
                        self.p.call("wait", stable=0.6, timeout=4)
                    except PhoneError:
                        pass
                    settled = self.observe()["snap"]
                    if self._until_visible(settled, until):
                        snap = settled
                    else:
                        snap = settled
                        continue          # momentum carried it away: keep scrolling
                    lines.append(f"scrolled {i + 1}×{direction} → '{until}' visible")
                    d = self.diff(before, snap)
                    return {"action": {"op": "scroll", "swipes": i + 1, "found": until, "via": method},
                            "text": "\n".join(lines) + "\n" + d["text"], "diff": d, "mode": "diff",
                            "ms": round((time.perf_counter() - t0) * 1000, 1)}
            elif not moved:
                d = self.diff(before, snap)
                return {"action": {"op": "scroll", "swipes": i, "end": True, "via": method},
                        "text": f"end of list after {i} swipes\n" + d["text"], "diff": d, "mode": "diff",
                        "ms": round((time.perf_counter() - t0) * 1000, 1)}
        snap = self.observe()["snap"]
        d = self.diff(before, snap)
        return {"action": {"op": "scroll", "swipes": max_swipes},
                "text": f"'{until}' not found after {max_swipes} swipes\n" + d["text"] if until
                else f"scrolled {max_swipes}×{direction}\n" + d["text"],
                "diff": d, "mode": "diff", "ms": round((time.perf_counter() - t0) * 1000, 1)}

    def swipe(self, x1, y1, x2, y2, duration=0.3) -> dict:
        before = self.current()
        self.p.call("act", op="swipe", x=float(x1), y=float(y1),
                    dx=float(x2 - x1), dy=float(y2 - y1), duration=float(duration * 1000))
        self.settle_action()
        after = self.observe()
        d = self.diff(before, after["snap"])
        return {"action": {"op": "swipe"}, "text": d["text"], "diff": d, "mode": "diff"}

    def back(self) -> dict:
        return self._act_then_diff("back", op="global", name="back")

    def button(self, name: str) -> dict:
        allowed = {"home", "back", "recents", "notifications", "quick_settings",
                   "power_dialog", "lock", "take_screenshot"}
        if name not in allowed:
            raise PhoneError("BAD_REQUEST", f"button must be one of {sorted(allowed)}")
        return self._act_then_diff(name, op="global", name=name)

    def launch(self, app: str, fresh=False) -> dict:
        pkg = KNOWN_APPS.get((app or "").strip().lower(), None)
        if pkg is not None:
            try:
                return self._launch(pkg, fresh)
            except PhoneError as e:
                if e.code != "APP_NOT_FOUND":
                    raise
                pkg = None            # the AOSP name isn't installed (One UI: samsung/sec packages)
        apps = self.apps()
        low = (app or "").lower()
        for a in apps:
            if low in a["name"].lower() or low in a["pkg"].lower():
                pkg = a["pkg"]
                break
        if pkg is None:
            raise PhoneError("APP_NOT_FOUND", f"no installed app matches '{app}' (see phone_apps)")
        return self._launch(pkg, fresh)

    def _launch(self, pkg: str, fresh: bool) -> dict:
        t0 = time.perf_counter()
        if fresh:
            # a dev device on adb gets a true force-stop; on-device it is the
            # best-effort killBackgroundProcesses
            if self.p.adb is not None:
                try:
                    self.p.adb.run("shell", "am", "force-stop", pkg)
                except PhoneError:
                    pass
            self.p.call("launch", pkg=pkg, fresh=True)
        else:
            self.p.call("launch", pkg=pkg)
        time.sleep(0.6)                       # cold start: give it the first frame
        self.settle(cap=4)
        after = self.observe()
        ms = round((time.perf_counter() - t0) * 1000, 1)
        self.latency.record("launch", ms)
        return {"action": {"op": "launch", "pkg": pkg, "ms": ms},
                "text": after["text"], "mode": "full", "app": after["app"]}

    def apps(self) -> list[dict]:
        return sorted(self.p.call("apps").get("apps", []), key=lambda a: a.get("name", "").casefold())

    def wait_for(self, text=None, *, gone=False, timeout=8.0, stable=0.0) -> dict:
        t0 = time.perf_counter()
        r = self.p.call("wait", text=text, gone=gone, timeout=timeout, stable=stable)
        out = {"action": {"op": "wait", "matched": r.get("ok"), "ms": r.get("elapsed_ms")},
               "mode": "result", "ms": round((time.perf_counter() - t0) * 1000, 1)}
        if not r.get("ok"):
            out["text"] = f"'{text}' {'still present' if not gone else 'never appeared'} after {timeout}s"
        else:
            out["text"] = (f"'{text}' {'gone' if gone else 'visible'}"
                           + (f" (stable {stable}s)" if stable else "")
                           + f" · {r.get('elapsed_ms')} ms")
        return out

    def screenshot(self, *, force=False, max_px=1280, grid=True, region=None) -> dict:
        """Fallback image of a screen the tree cannot describe (the phone refuses readable screens unless forced)."""
        r = self.p.call("screenshot", force=force, max_px=max_px, grid=grid, **({"region": region} if region else {}))
        r["text"] = f"screenshot {r.get('w')}x{r.get('h')} px (the phone screen is {r.get('sw')}x{r.get('sh')})"
        return r

    def status(self) -> dict:
        out = self.p.call("status")
        out["axdroid"] = __version__
        out["latency"] = self.latency.summary()
        return out

    # ── hub-local plans: one model round trip, many steps ─────────────

    def run(self, steps: list[dict]) -> dict:
        log = []
        failed = None
        t0 = time.perf_counter()
        for i, step in enumerate(steps, 1):
            try:
                if "tap" in step:
                    r = self.tap(step["tap"], kind=step.get("kind", "tap"), confirm=step.get("confirm", False))
                    log.append(f"{i}. tap {step['tap']}")
                elif "type" in step:
                    r = self.type(step["type"], step.get("into"), replace=step.get("replace", True),
                                  submit=step.get("submit", False))
                    log.append(f"{i}. type → {step.get('into') or 'focused'}")
                elif "scroll" in step:
                    r = self.scroll(step.get("direction", "down"), until=step.get("until"), max_swipes=step.get("max_swipes", 6))
                    log.append(f"{i}. scroll {step.get('direction', 'down')}")
                elif "launch" in step:
                    r = self.launch(step["launch"], fresh=step.get("fresh", False))
                    log.append(f"{i}. launch {step['launch']}")
                elif "button" in step:
                    r = self.button(step["button"])
                    log.append(f"{i}. button {step['button']}")
                elif step.get("back"):
                    r = self.back()
                    log.append(f"{i}. back")
                elif "wait" in step:
                    w = step["wait"]
                    r = self.wait_for(w if isinstance(w, str) else w.get("text"),
                                      gone=bool(w.get("gone")) if isinstance(w, dict) else False,
                                      timeout=w.get("timeout", 8) if isinstance(w, dict) else 8)
                    if not r["action"]["matched"]:
                        raise PhoneError("WAIT_TIMEOUT", f"wait step failed: {w}")
                    log.append(f"{i}. wait {w}")
                elif "expect" in step:
                    snap = self.current()
                    if not find(snap, text=step["expect"], visible_only=True):
                        raise PhoneError("EXPECT_FAILED", f"expected '{step['expect']}' on screen")
                    log.append(f"{i}. expect '{step['expect']}' ✓")
                    continue
                else:
                    raise PhoneError("BAD_STEP", f"unknown step {step}")
                if r.get("action") and r["action"].get("op"):
                    pass
            except PhoneError as e:
                failed = {"step": i, "step_spec": step, "error": e.to_dict()}
                log.append(f"{i}. FAILED ({e.code}): {e}")
                break
        final = self.observe()
        return {"ok": failed is None, "log": log, "failed_step": failed,
                "text": final["text"], "ms": round((time.perf_counter() - t0) * 1000, 1)}
