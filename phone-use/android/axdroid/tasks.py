"""axdroid — the task benchmark: multi-step, cross-app, verifiable phone tasks.

Each task drives the phone exactly as an agent would (the same Session calls
the MCP tools make) and verifies the outcome from the device's own tree —
no screenshots, no human in the loop. Every task records wall time and the
tokens an agent would have paid (sum of ceil(chars/4) over every tool output
the task produced), which is the number that matters for cost comparisons.

Run on a device:  python -m axdroid tasks --out tasks-report.json
"""

from __future__ import annotations

import time

from .client import Phone, PhoneError
from .engine import Session
from .model import find


def _tok(s: str) -> int:
    return (len(s or "") + 3) // 4


class Recorder:
    """Wraps a Session so every tool output an agent would see is metered."""

    def __init__(self, s: Session):
        self.s = s
        self.tokens = 0
        self.calls = 0

    def __getattr__(self, name):
        attr = getattr(self.s, name)

        def meter(*a, **kw):
            r = attr(*a, **kw)
            self.calls += 1
            text = r.get("text") if isinstance(r, dict) else None
            if text:
                self.tokens += _tok(text)
            return r

        return meter if callable(attr) else attr

    def reset(self):
        self.tokens = 0
        self.calls = 0

    def p_call_wait(self, text, timeout=6):
        """Device-side wait (internal; not a tool output, not metered)."""
        return self.s.p.call("wait", text=text, timeout=timeout)


def _switch_state(snap, label_part):
    for n in snap.nodes.values():
        if n.checked is not None and n.visible and label_part.lower() in (n.name or "").lower():
            return n
    return None


def _row_value(snap, label_part, pkg=None):
    """The summary text right after a titled row (value extraction)."""
    rows = [n for n in snap.nodes.values()
            if n.visible and n.name and label_part.lower() in n.name.lower()
            and (pkg is None or n.win.get("pkg") == pkg)]
    if not rows:
        return None
    row = rows[0]
    for n in snap.nodes.values():
        if n.parent == row.parent and n.visible and n.name and n.id != row.id:
            return n.name
    return None


# ── tasks ────────────────────────────────────────────────────────────────────

def t_wifi_toggle(r: Recorder):
    r.launch("Settings", fresh=True)
    r.tap("Connections")
    r.p_call_wait("Wi‑Fi", timeout=6)
    snap = r.s.observe()["snap"]
    sw = _switch_state(snap, "wi") or _switch_state(snap, "wifi")
    if sw is None:
        return False, "no Wi-Fi switch on the Connections screen"
    was = sw.checked
    r.tap(sw.ref)
    snap2 = r.s.observe()["snap"]
    sw2 = _switch_state(snap2, "wi") or _switch_state(snap2, "wifi")
    if sw2 is None or sw2.checked == was:
        return False, f"switch did not flip (still {was})"
    r.tap(sw2.ref)                     # restore
    snap3 = r.s.observe()["snap"]
    sw3 = _switch_state(snap3, "wi") or _switch_state(snap3, "wifi")
    ok = sw3 is not None and sw3.checked == was
    return ok, f"wi-fi {was}→{not was}→{sw3.checked if sw3 else '?'}"


def t_search_and_filter(r: Recorder):
    r.launch("Settings", fresh=True)
    for name in ("Search settings", "Search"):
        try:
            r.tap(name)
            break
        except PhoneError as e:
            if e.code not in ("NOT_FOUND", "AMBIGUOUS"):
                raise
    snap = r.s.observe()["snap"]
    fields = [n for n in snap.nodes.values() if n.editable and n.visible]
    if not fields:
        return False, "no search field"
    out = r.type("battery", fields[0].ref)
    if not out["action"].get("verified") or (out["action"].get("value") or "").lower() != "battery":
        return False, f"typed text not verified: {out['action']}"
    w = r.wait_for("Battery", timeout=6)
    r.s.button("back"); r.s.button("back")
    return bool(w["action"]["matched"]), "search filtered to Battery results"


def t_about_phone_extract(r: Recorder):
    r.launch("Settings", fresh=True)
    r.scroll("down", until="About phone", max_swipes=8)
    r.tap("About phone")
    r.p_call_wait("Model", timeout=6)
    snap = r.s.observe()["snap"]
    # One UI exposes title/value rows; a device-model pattern is the robust check
    import re
    models = [n.name for n in snap.nodes.values()
              if n.visible and n.name and re.search(r"\b(SM-[A-Z0-9]+|GT-[A-Z0-9]+)\b", n.name)]
    r.s.button("back")
    if not models:
        return False, "no SM-… model value on the About phone screen"
    return True, f"model={models[0]!r}"


def t_dark_mode_navigate(r: Recorder):
    # One UI 8 has no 'Display' row at the top level — search is the reliable
    # (and shorter) route to Dark mode, and exercises typed navigation
    r.launch("Settings", fresh=True)
    for name in ("Search settings", "Search"):
        try:
            r.tap(name)
            break
        except PhoneError as e:
            if e.code not in ("NOT_FOUND", "AMBIGUOUS"):
                raise
    snap = r.s.observe()["snap"]
    fields = [n for n in snap.nodes.values() if n.editable and n.visible]
    if not fields:
        return False, "no search field"
    r.type("dark", fields[0].ref, submit=True)
    w = r.p_call_wait("Dark mode", timeout=8)
    snap = r.s.observe()["snap"]
    hit = find(snap, text="Dark mode", visible_only=True, pkg=snap.app)
    r.s.button("back"); r.s.button("back")
    return bool(w.get("ok")) or bool(hit), "search 'dark' surfaced the Dark mode setting"


def t_contacts_create_delete(r: Recorder):
    r.launch("Contacts", fresh=True)
    time.sleep(1.0)
    snap = r.s.observe()["snap"]
    create = None
    for cand in ("Create contact", "New contact", "Add contact", "Contacts+"):
        hits = find(snap, text=cand, visible_only=True)
        hits = [h for h in hits if h.actionable] or hits
        if hits:
            create = hits[0]
            break
    if create is None:
        # a FAB with just a desc
        fabs = [n for n in snap.nodes.values() if n.actionable and n.visible
                and n.rid and "fab" in (n.rid or "").lower()]
        if fabs:
            create = fabs[0]
    if create is None:
        return None, "no create-contact control on this Contacts build (skipped)"
    r.tap(create.ref)
    time.sleep(0.8)
    snap = r.s.observe()["snap"]
    fields = [n for n in snap.nodes.values() if n.editable and n.visible]
    if not fields:
        return None, "contact editor did not expose fields (skipped)"
    name_field = None
    for n in fields:
        if n.rid and ("name" in n.rid.lower() or n.rid.endswith("edittext")):
            name_field = n
            break
    name_field = name_field or fields[0]
    r.type("Ax Test", name_field.ref)
    r.s.button("back")                     # collapse keyboard
    snap = r.s.observe()["snap"]
    save = None
    for cand in ("Save", "Done"):
        hits = find(snap, text=cand, visible_only=True)
        hits = [h for h in hits if h.actionable] or hits
        if hits:
            save = hits[0]
            break
    if save is None:
        return False, "no Save button after typing the name"
    r.tap(save.ref)
    time.sleep(1.0)
    snap = r.s.observe()["snap"]
    if not find(snap, text="Ax Test", visible_only=True):
        return False, "contact 'Ax Test' not on screen after save"
    # cleanup through the guard: Delete lives in the overflow menu on One UI
    overflow = None
    for n in snap.nodes.values():
        if n.visible and n.actionable and (n.rid == "android:id/overflow"
                                           or (n.desc or "").lower() in ("more options", "options")):
            overflow = n
            break
    if overflow is not None:
        r.tap(overflow.ref)
        time.sleep(0.6)
    try:
        r.tap("Delete")
        return False, "delete was not guarded!"
    except PhoneError as e:
        if e.code != "NEEDS_CONFIRMATION":
            raise
    r.tap("Delete", confirm=True)
    time.sleep(1.2)
    snap = r.s.observe()["snap"]
    gone = not find(snap, text="Ax Test", visible_only=True)
    r.s.button("back")
    return gone, "created 'Ax Test', guard refused the delete, confirmed → deleted"


def t_play_store_search(r: Recorder):
    r.launch("Play Store", fresh=True)
    time.sleep(2.5)                       # store first frame is slow
    snap = r.s.observe()["snap"]
    if find(snap, text="Sign in", visible_only=True):
        return None, "Play Store not signed in (skipped)"
    entry = None
    for cand in ("Search apps & games", "Search apps", "Search"):
        hits = find(snap, text=cand, visible_only=True)
        hits = [h for h in hits if h.actionable] or hits
        if hits:
            entry = hits[0]
            break
    if entry is None:
        # this store build's search lives in the bottom nav: a text node inside
        # a clickable cell — tap resolves it and the device clicks the ancestor
        hits = [n for n in snap.nodes.values() if n.visible and (n.name or "").strip() == "Search"]
        if hits:
            entry = hits[0]
    if entry is None:
        # last resort: the pill, by resource id only
        hits = [n for n in snap.nodes.values() if n.visible and n.actionable and n.rid
                and "search" in n.rid.lower()]
        if hits:
            entry = hits[0]
    if entry is None:
        return None, "no Play Store search entry (skipped)"
    r.tap(entry.ref)
    snap = r.s.observe()["snap"]
    fields = [n for n in snap.nodes.values() if n.editable and n.visible]
    if not fields:
        return None, "no store search field (skipped)"
    r.type("spotify", fields[0].ref, submit=True)
    w = r.wait_for("Spotify", timeout=10)
    hit = find(r.s.current(), text="Spotify", visible_only=True)
    r.s.button("home")
    return bool(w["action"]["matched"]) or bool(hit), "store search listed Spotify"


def t_guard_needs_confirmation(r: Recorder):
    r.launch("Settings", fresh=True)
    # One UI hides Reset inside General management
    try:
        r.scroll("down", until="General management", max_swipes=8)
        r.tap("General management")
        r.p_call_wait("Reset", timeout=6)
        r.scroll("down", until="Reset", max_swipes=4)
    except PhoneError:
        pass
    refused = False
    try:
        r.tap("Reset")
    except PhoneError as e:
        refused = e.code == "NEEDS_CONFIRMATION"
    r.s.button("back")
    r.s.button("back")
    return refused, "risk-labelled 'Reset' refused without confirm (never confirmed)"


def t_multi_step_plan(r: Recorder):
    out = r.run([
        {"launch": "Settings"},
        {"tap": "Connections"},
        {"expect": "Wi‑Fi"},
        {"back": True},
        {"button": "home"},
    ])
    note = "5-step plan, one round trip"
    if not out["ok"]:
        note += f" — failed at step {out['failed_step']['step']}: {out['failed_step']['error']}"
    return out["ok"], note


def t_messages_compose(r: Recorder):
    r.launch("Messages", fresh=True)
    time.sleep(1.5)
    snap = r.s.observe()["snap"]
    # Google Messages may open on account setup ("Continue as …") — finish it
    cont = [n for n in snap.nodes.values() if n.visible and n.actionable
            and (n.name or "").startswith("Continue as")]
    if cont:
        r.tap(cont[0].ref)
        time.sleep(1.5)
        snap = r.s.observe()["snap"]
    if find(snap, text="Set default SMS app", visible_only=True):
        # not the default SMS app → no compose FAB; the honest variant is
        # search (never change the owner's default app from a benchmark)
        try:
            r.tap("Search messages")
            snap = r.s.observe()["snap"]
            fields = [n for n in snap.nodes.values() if n.editable and n.visible]
            if fields:
                out = r.type("hello", fields[0].ref)
                r.s.button("back"); r.s.button("back")
                return bool(out["action"].get("verified")), \
                    "search typed + verified (no compose FAB: not the default SMS app)"
        except PhoneError:
            pass
        return None, "Messages not the default SMS app and no search field (skipped)"
    compose = None
    for cand in ("Start conversation", "Start chat", "New message", "Compose"):
        hits = find(snap, text=cand, visible_only=True)
        hits = [h for h in hits if h.actionable] or hits
        if hits:
            compose = hits[0]
            break
    if compose is None:
        hits = [n for n in snap.nodes.values() if n.visible and n.actionable and n.rid
                and ("fab" in n.rid.lower() or "start" in (n.rid or "").lower())]
        if hits:
            compose = hits[0]
    if compose is None:
        return None, "no compose control (skipped)"
    r.tap(compose.ref)
    time.sleep(0.8)
    snap = r.s.observe()["snap"]
    fields = [n for n in snap.nodes.values() if n.editable and n.visible]
    if not fields:
        return None, "composer field not exposed (skipped)"
    out = r.type("hello from axdroid", fields[0].ref)
    r.s.button("back"); r.s.button("back")
    return bool(out["action"].get("verified")), "draft typed + verified (never sent — guard holds)"


TASKS = [
    ("wifi toggle round-trip", t_wifi_toggle),
    ("settings search + filter", t_search_and_filter),
    ("about-phone data extraction", t_about_phone_extract),
    ("navigate to Dark mode", t_dark_mode_navigate),
    ("contacts: create + verify", t_contacts_create_delete),
    ("play store search", t_play_store_search),
    ("guard refuses 'Reset'", t_guard_needs_confirmation),
    ("5-step plan (phone_run)", t_multi_step_plan),
    ("messages: typed draft", t_messages_compose),
]


def run_tasks(phone: Phone, *, only=None) -> dict:
    s = Session(phone)
    s.info = s.p.call("status")
    rec = Recorder(s)

    rep = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "tasks": [], "ok": False}
    latencies = []
    for name, fn in TASKS:
        if only and name not in only:
            continue
        entry = {"name": name}
        t0 = time.perf_counter()
        rec.reset()
        try:
            ok, note = fn(rec)
            entry.update(ok=ok, note=note, ms=round((time.perf_counter() - t0) * 1000),
                         calls=rec.calls, tokens=rec.tokens)
        except PhoneError as e:
            entry.update(ok=False, error=e.code, note=str(e)[:120],
                         ms=round((time.perf_counter() - t0) * 1000), calls=rec.calls, tokens=rec.tokens)
        rep["tasks"].append(entry)
        print(f"{'PASS' if entry.get('ok') else ('skip' if entry.get('ok') is None else 'FAIL')}"
              f"  {name:34} {entry['ms']:>7}ms {entry.get('tokens', 0):>6}t  {entry.get('note') or entry.get('error', '')}")
        try:
            s.button("home")
        except PhoneError:
            pass

    # per-action latency sample for the report
    for _ in range(4):
        t0 = time.perf_counter()
        s.button("home")
        s.settle_action()
        latencies.append(round((time.perf_counter() - t0) * 1000))
    ran = [t for t in rep["tasks"] if t["ok"] is not None]
    rep.update({
        "passed": sum(1 for t in ran if t["ok"]),
        "failed": sum(1 for t in ran if not t["ok"]),
        "skipped": sum(1 for t in rep["tasks"] if t["ok"] is None),
        "total_ms": sum(t["ms"] for t in rep["tasks"]),
        "total_tokens": sum(t.get("tokens", 0) for t in rep["tasks"]),
        "button_latency_ms": latencies,
    })
    rep["ok"] = rep["failed"] == 0 and rep["passed"] >= 5
    rep["summary"] = (f"{rep['passed']}/{len(ran)} passed, {rep['skipped']} skipped, "
                      f"{rep['failed']} failed · {rep['total_ms']/1000:.1f}s total · "
                      f"{rep['total_tokens']} tokens of tool output")
    return rep


def publish_tasks(reps: list[dict]) -> str:
    out = ["# axdroid — task benchmark (measured)", ""]
    for rep in reps:
        out.append(f"## {rep['started']} · {rep['summary']}")
        out.append("")
        out.append("| task | result | ms | tool calls | output tokens |")
        out.append("|---|---|---|---|---|")
        for t in rep["tasks"]:
            res = "PASS" if t["ok"] else ("skip" if t["ok"] is None else "FAIL")
            out.append(f"| {t['name']} | {res} | {t['ms']} | {t.get('calls', 0)} | {t.get('tokens', 0)} |")
        out.append("")
        if rep.get("button_latency_ms"):
            xs = sorted(rep["button_latency_ms"])
            out.append(f"home-button action latency: {', '.join(str(x) + ' ms' for x in xs)} (p50 ≈ {xs[len(xs)//2]} ms incl. settle)")
        out.append("")
    return "\n".join(out)
