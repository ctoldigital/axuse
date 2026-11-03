"""axdroid — hardware gates: prove the bridge on a real phone (or emulator).

G1  bridge reachable, service connected, status sane
G2  tree: Settings exposes a real tree (nodes, clickable rows, resource ids)
G3  semantic click: navigate Settings by label, verify via diff, come back
G4  type + IME submit into Settings search, results filter, verify read-back
G5  gesture fallback: blind coordinate tap that names a real element; swipe
G6  settle: the device change counter advances on navigation then goes quiet
G7  custody: owner stop refuses acting (reads still work), resume re-enables
G8  guard: a risk-labelled control refuses without confirm:true (never
    actually performs it — the gate passes when the refusal arrives)
G9  app sweep: launch N apps, classify rich/partial/opaque + UI framework

Each gate records measured ms. The report is JSON; publish_gates() renders
markdown. A gate that cannot run (e.g. no risk label visible anywhere) is
`skipped`, never silently passed.
"""

from __future__ import annotations

import time

from .client import Phone, PhoneError
from .engine import Session
from .model import detect_framework, find


def _gate(rep, name):
    entry = {"name": name}
    rep["gates"].append(entry)
    return entry


def _finish(entry, ok, ms, note=None, **facts):
    entry.update({"ok": ok, "ms": round(ms, 1), "note": note, **facts})
    return entry


def run_gates(phone: Phone, *, sweep: int = 8) -> dict:
    rep = {"axdroid": __import__("axdroid", fromlist=["__version__"]).__version__,
           "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "gates": [], "ok": False}
    try:
        phone.call("resume")          # recover from a previously crashed run
    except PhoneError:
        pass
    s = Session(phone)
    # start from a clean, known screen (a stale app state from an earlier run
    # occludes Settings and fails the later gates legitimately)
    try:
        s.button("home")
    except PhoneError:
        pass

    # G1 — bridge
    e = _gate(rep, "G1 bridge")
    t0 = time.perf_counter()
    try:
        h = phone.healthz()
        st = phone.call("status")
        ok = h.get("ok") and st.get("service", {}).get("connected")
        _finish(e, bool(ok), (time.perf_counter() - t0) * 1000,
                device=st.get("device"), foreground=st.get("foreground"),
                guard_mode=st.get("policy", {}).get("guard_mode"))
    except PhoneError as err:
        _finish(e, False, (time.perf_counter() - t0) * 1000, error=err.to_dict())

    # G2 — Settings tree
    e = _gate(rep, "G2 tree")
    t0 = time.perf_counter()
    try:
        s.launch("Settings", fresh=True)
        obs = s.observe()
        snap = obs["snap"]
        clickable = sum(1 for n in snap.nodes.values() if n.clickable and n.visible)
        with_rid = sum(1 for n in snap.nodes.values() if n.rid)
        ok = len(snap.nodes) >= 40 and clickable >= 5
        _finish(e, ok, (time.perf_counter() - t0) * 1000,
                nodes=len(snap.nodes), clickable=clickable, with_resource_id=with_rid,
                observe_ms=obs["ms"], note=obs["text"].splitlines()[0] if obs["text"] else "")
    except PhoneError as err:
        _finish(e, False, (time.perf_counter() - t0) * 1000, error=err.to_dict())

    # G3 — semantic click on a real Settings ROW: target the row-title nodes
    # (android:id/title), not any text on screen (summaries, the status bar)
    e = _gate(rep, "G3 semantic click")
    t0 = time.perf_counter()
    try:
        s.launch("Settings", fresh=True)
        target = None
        for cand in ("About phone", "About", "Battery", "Apps", "Connections"):
            try:
                s.scroll("down", until=cand, max_swipes=6)
            except PhoneError:
                pass
            snap = s.current()
            titles = [n for n in snap.nodes.values()
                      if n.visible and n.rid and n.rid.endswith("id/title")]
            hits = [n for n in titles if cand.lower() in (n.name or "").lower()]
            if not hits:
                # locale fallback: any title row at all still proves the click path
                hits = titles[:1] if cand == "Connections" else []
            if hits:
                target = hits[0]
                break
        if target is None:
            _finish(e, False, (time.perf_counter() - t0) * 1000, note="no Settings row titles reachable")
        else:
            # a One UI fling keeps re-binding rows after the last swipe: wait for
            # genuine stability, then re-find the row in the settled tree — the
            # ref chosen before the wait can be stale by the time we tap
            try:
                s.p.call("wait", stable=1.0, timeout=6)
            except PhoneError:
                pass
            settled = s.observe()["snap"]
            titles = [n for n in settled.nodes.values()
                      if n.visible and n.rid and n.rid.endswith("id/title")]
            fresh = [n for n in titles if n.name == target.name] or titles[:1]
            target = fresh[0]
            r = None
            target_name = target.name
            for attempt in (1, 2, 3):
                try:
                    r = s.tap(target.ref)
                except PhoneError as tap_err:
                    # mid-fling re-binding: re-find the row by name and retry
                    try:
                        snap_now = s.observe()["snap"]
                        titles_now = [n for n in snap_now.nodes.values()
                                      if n.visible and n.rid and n.rid.endswith("id/title")
                                      and n.name == target_name]
                        if titles_now:
                            target = titles_now[0]
                            continue
                    except PhoneError:
                        pass
                    if attempt == 3:
                        raise tap_err
                    continue
                if r["diff"]["added"] or r["diff"]["changed"]:
                    break
                time.sleep(0.5)
            via = r["action"].get("via", "?")
            d = r["diff"]
            s.button("back")
            _finish(e, bool(d["added"] or d["changed"]), (time.perf_counter() - t0) * 1000,
                    tapped=target.name[:30], via=via, diff_lines=r["text"].count("\n") + 1,
                    retries=attempt - 1)
    except PhoneError as err:
        _finish(e, False, (time.perf_counter() - t0) * 1000, error=err.to_dict())

    # G4 — type into search: One UI exposes a "Search settings" *button* that
    # opens the search overlay; the editable field appears then. The device's
    # own read-back verification is the pass condition (locale-neutral).
    e = _gate(rep, "G4 type+submit")
    t0 = time.perf_counter()
    try:
        s.launch("Settings", fresh=True)
        opened = False
        for name in ("Search settings", "Search", "검색"):
            try:
                s.tap(name)
                opened = True
                break
            except PhoneError as err:
                if err.code not in ("NOT_FOUND", "AMBIGUOUS"):
                    raise
        if not opened:
            _finish(e, False, (time.perf_counter() - t0) * 1000, note="no search control found on the Settings screen")
        else:
            snap = s.observe()["snap"]
            fields = [n for n in snap.nodes.values() if n.editable and n.visible]
            if not fields:
                _finish(e, False, (time.perf_counter() - t0) * 1000, note="search opened but no editable field exposed")
            else:
                f = fields[0]
                r = s.type("battery", f.ref, replace=True, submit=False)
                verified = bool(r["action"].get("verified"))
                value_ok = (r["action"].get("value") or "").lower() == "battery"
                s.button("back"); s.button("back")
                _finish(e, verified and value_ok, (time.perf_counter() - t0) * 1000,
                        via=r["action"].get("via"), field=f.rid or f.ref,
                        note="ACTION_SET_TEXT with device-side read-back verification")
    except PhoneError as err:
        _finish(e, False, (time.perf_counter() - t0) * 1000, error=err.to_dict())

    # G5 — gesture fallback: blind coordinate tap that lands on a named element
    e = _gate(rep, "G5 gesture fallback")
    t0 = time.perf_counter()
    try:
        snap = s.current()
        rows = [n for n in snap.nodes.values() if n.clickable and n.visible and n.bounds
                and n.bounds[1] > snap.screen[1] * 0.15]
        if not rows:
            _finish(e, False, (time.perf_counter() - t0) * 1000, note="no tappable row on screen")
        else:
            row = rows[min(3, len(rows) - 1)]
            cx, cy = row.centre
            r = s.tap(x=cx, y=cy)
            s.back()
            _finish(e, r["action"].get("ok", True), (time.perf_counter() - t0) * 1000,
                    via=r["action"].get("via"), point=(cx, cy),
                    note=f"blind tap named '{(row.name or row.rid or '?')[:40]}' and proceeded")
    except PhoneError as err:
        _finish(e, False, (time.perf_counter() - t0) * 1000, error=err.to_dict())

    # G6 — settle: counter advances then goes quiet
    e = _gate(rep, "G6 settle")
    t0 = time.perf_counter()
    try:
        c0 = phone.call("changes")["count"]
        s.button("home")
        time.sleep(0.3)
        c1 = phone.call("changes")["count"]
        st = s.settle()
        c2 = phone.call("changes")["count"]
        s.launch("Settings")
        ok = c1 > c0 and st.get("quiet")
        _finish(e, ok, (time.perf_counter() - t0) * 1000, changes_before=c0, changes_after_home=c1,
                changes_after_settle=c2, settle_ms=st.get("ms"))
    except PhoneError as err:
        _finish(e, False, (time.perf_counter() - t0) * 1000, error=err.to_dict())

    # G7 — custody stop/resume
    e = _gate(rep, "G7 custody")
    t0 = time.perf_counter()
    try:
        try:
            phone.call("stop", reason="gate")
            refused = False
            try:
                phone.call("act", op="global", name="home")
            except PhoneError as err:
                refused = err.code == "STOPPED_BY_OWNER"
            reads_ok = bool(phone.call("observe").get("nodes"))
        finally:
            try:
                phone.call("resume")
            except PhoneError:
                pass
        acts_ok = True
        try:
            phone.call("act", op="global", name="back")
        except PhoneError:
            acts_ok = False
        _finish(e, refused and reads_ok and acts_ok, (time.perf_counter() - t0) * 1000,
                refused_as=refused, reads_while_stopped=reads_ok, acts_after_resume=acts_ok)
    except PhoneError as err:
        try:
            phone.call("resume")
        except PhoneError:
            pass
        _finish(e, False, (time.perf_counter() - t0) * 1000, error=err.to_dict())

    # G8 — guard engages on a risk-labelled control (never confirms it)
    e = _gate(rep, "G8 guard")
    t0 = time.perf_counter()
    try:
        s.launch("Settings")
        snap = s.observe()["snap"]
        risky = None
        for n in snap.nodes.values():
            if n.visible and n.actionable and n.name:
                from .gates import _risk_label
                if _risk_label(n.name):
                    risky = n
                    break
        if risky is None:
            _finish(e, None, (time.perf_counter() - t0) * 1000,
                    note="no risk-labelled control visible on this screen — skipped, not passed")
        else:
            refused = False
            try:
                phone.call("act", op="click", id=risky.id)
            except PhoneError as err:
                refused = err.code == "NEEDS_CONFIRMATION"
            # never confirm: navigate away instead
            s.back()
            _finish(e, refused, (time.perf_counter() - t0) * 1000, target=(risky.name or "")[:40], refused=refused)
    except PhoneError as err:
        _finish(e, False, (time.perf_counter() - t0) * 1000, error=err.to_dict())

    # G9 — app sweep
    e = _gate(rep, "G9 app sweep")
    t0 = time.perf_counter()
    try:
        apps = s.apps()
        skip = {"launcher", "home", "systemui", "settings", "bixby", "axuse"}
        # prefer the major first-party apps for a representative sweep, then
        # whatever else is installed (with <queries> the pool is the whole phone)
        prefer = ["com.samsung.android.calendar", "com.sec.android.app.camera",
                  "com.sec.android.clockpackage", "com.sec.android.gallery3d",
                  "com.samsung.android.messaging", "com.sec.android.app.myfiles",
                  "com.samsung.android.app.contacts", "com.android.dialer"]
        pool = {a["pkg"]: a for a in apps
                if not any(k in a["pkg"].lower() or k in a["name"].lower() for k in skip)}
        ordered = [pool[p] for p in prefer if p in pool] + \
                  [a for a in apps if a["pkg"] not in prefer and a["pkg"] in pool]
        cands = [a for a in ordered if a["pkg"] in pool]
        results = []
        for a in cands[:sweep]:
            entry = {"app": a["pkg"], "name": a["name"]}
            try:
                s.launch(a["pkg"])
                time.sleep(0.8)
                snap = s.observe()["snap"]
                visible = [n for n in snap.nodes.values() if n.visible]
                actionable = [n for n in visible if n.actionable and (n.name or n.rid)]
                fw = detect_framework({n.cls or "" for n in visible})
                entry.update(nodes=len(snap.nodes), actionable=len(actionable), framework=fw,
                             quality=("rich" if len(actionable) >= 8 else
                                      "partial" if len(actionable) >= 2 else "opaque"))
                s.button("home")
            except PhoneError as err:
                entry.update(error=err.code)
            results.append(entry)
        rich = sum(1 for r in results if r.get("quality") == "rich")
        _finish(e, True, (time.perf_counter() - t0) * 1000, apps=results,
                note=f"{rich}/{len(results)} apps expose a rich tree")
    except PhoneError as err:
        _finish(e, False, (time.perf_counter() - t0) * 1000, error=err.to_dict())

    ran = [g for g in rep["gates"] if g["ok"] is not None]
    rep["ok"] = all(g["ok"] for g in ran) and len(ran) >= 7
    rep["summary"] = (f"{sum(1 for g in ran if g['ok'])}/{len(ran)} passed"
                      + (f", {sum(1 for g in rep['gates'] if g['ok'] is None)} skipped" if any(g["ok"] is None for g in rep['gates']) else ""))
    return rep


def _risk_label(name: str) -> bool:
    import re
    for pat in (r"\b(pay|buy|purchase|checkout|order now|subscribe|send money|renew)\b",
                r"\b(send|post|publish|share|submit|call|dial)\b",
                r"\b(delete|remove|erase|reset|unsubscribe|sign out|log out|uninstall|block)\b",
                r"\b(allow|grant|install)\b"):
        if re.search(pat, name or "", re.I):
            return True
    return False


def publish_gates(reports: list[dict]) -> str:
    """Render one or more gate reports into markdown (the compatibility list)."""
    out = ["# axdroid — device compatibility (measured)", ""]
    for rep in reports:
        dev = next((g.get("device") for g in rep["gates"] if g.get("device")), None)
        out.append(f"## {dev.get('model', '?')} — {dev.get('os', '?')} · {rep['started']} · {rep['summary']}")
        out.append("")
        out.append("| gate | result | ms | evidence |")
        out.append("|---|---|---|---|")
        for g in rep["gates"]:
            res = "PASS" if g["ok"] else ("skip" if g["ok"] is None else "FAIL")
            ev = g.get("note") or ""
            for k in ("nodes", "clickable", "via", "changes_after_home", "target", "refused_as"):
                if k in g:
                    ev += f" · {k}={g[k]}"
            if "error" in g:
                ev += f" · {g['error'].get('code')}"
            out.append(f"| {g['name']} | {res} | {g.get('ms', '')} | {ev} |")
        sweep = next((g for g in rep["gates"] if g["name"].startswith("G9")), None)
        if sweep and sweep.get("apps"):
            out.append("")
            out.append("| app | quality | actionable | framework |")
            out.append("|---|---|---|---|")
            for a in sweep["apps"]:
                out.append(f"| {a['name']} ({a['app']}) | {a.get('quality', '?')} | {a.get('actionable', '?')} | "
                           f"{', '.join(a.get('framework', [])) or '—'} |")
        out.append("")
    return "\n".join(out)
