"""phone_scan_list: open every row of a list, collect each detail screen, and
report which records are identical.

Built for tasks an agent does badly one step at a time — "exact duplicates",
"how many … with …", "delete everything that …" over dozens of similar rows.
It reads only what the accessibility tree shows (the same text the agent
sees), through the phone's own MCP tools; it changes nothing on the phone
(tap a row, read, back).

`call(name, args) -> str` is the phone's tool endpoint.
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict

_LINE = re.compile(r'^(?P<ind>\s*)- (?P<role>[^\s:×]+)(?:×\d+)?(?P<rest>.*)$')
_REF = re.compile(r'\s*\[(e\d+)\]\s*$')
_PAIR = re.compile(r'"((?:[^"\\]|\\.)*)"\s*\[(e\d+)\]')
_FIND = re.compile(r'^(e\d+)\s+(\S+)\s.*?bounds=\[(-?\d+),(-?\d+),(-?\d+),(-?\d+)\]')


def parse_tree(text: str) -> list[dict]:
    """Lines of a phone_observe render -> [{ind, role, label, ref}]."""
    out = []
    for raw in text.splitlines():
        m = _LINE.match(raw)
        if not m:
            continue
        ind = len(m.group("ind"))
        role, rest = m.group("role"), m.group("rest").strip()
        if rest.startswith(":"):                      # a run: `cell×3: "a" [e1] "b" [e2]`
            for lab, ref in _PAIR.findall(rest[1:]):
                out.append({"ind": ind, "role": role, "label": lab, "ref": ref})
            continue
        ref = None
        mr = _REF.search(rest)
        if mr:
            ref = mr.group(1)
            rest = rest[:mr.start()]
        out.append({"ind": ind, "role": role, "label": rest.strip(), "ref": ref})
    return out


def find_list(nodes: list[dict], hint: str = "") -> tuple[int, list[dict]] | None:
    """The list whose direct children are the rows: the one with the most
    child cells/groups that carry a ref (or the one matching `hint`)."""
    best = None
    for i, n in enumerate(nodes):
        if n["role"] not in ("list", "scroll", "grid", "pager") or not n["ref"]:
            continue
        if hint and hint.lower() not in (n["label"] or "").lower():
            continue
        kids = []
        for k in nodes[i + 1:]:
            if k["ind"] <= n["ind"]:
                break
            if k["ind"] == n["ind"] + 2 and k["role"] in ("cell", "group", "button") and k["ref"]:
                kids.append(k)
        if len(kids) >= 2 and (best is None or len(kids) > len(best[1])):
            best = (i, kids)
    return best


def parse_find(text: str) -> dict[str, tuple[int, int]]:
    """phone_find output -> {ref: (top, bottom)}"""
    out = {}
    for line in text.splitlines():
        m = _FIND.match(line.strip())
        if m:
            out[m.group(1)] = (int(m.group(4)), int(m.group(6)))
    return out


def align(prev: list[tuple[str, int]], new: list[tuple[str, int]], tol: int = 6,
          done: list[tuple[str, int]] | None = None,
          expected: int | None = None, rel: float = 0.35) -> tuple[int | None, set[int]]:
    """How far did the list scroll between two windows?  prev/new are
    [(label, top)].  Returns (shift, indexes of rows in `new` already seen):
    the shift is chosen so the most rows match by label AND position."""
    votes: dict[int, int] = {}
    for pl, pt in prev:
        for nl, nt in new:
            if pl == nl:
                votes[pt - nt] = votes.get(pt - nt, 0) + 1
    if expected:
        # we know about how far the list was asked to move: rows with equal
        # labels also vote for wrong shifts (one row pitch off, or ~0), so
        # only shifts near the expected one count — except at the end of the
        # list, where it moves less and nearly every row of the window overlaps
        near = {d: v for d, v in votes.items() if expected * (1 - rel) <= d <= expected * (1 + rel)}
        if near:
            votes = near
        else:
            end = {d: v for d, v in votes.items() if -tol <= d < expected * (1 - rel)}
            best = max(end.items(), key=lambda kv: kv[1]) if end else None
            # rows cut at an edge change their top, so they cannot vote: the
            # shift must explain every other row the two views have in common
            plabels = {pl for pl, _pt in prev}
            common = sum(1 for nl, _nt in new if nl in plabels)
            if best and best[1] >= max(2, min(len(prev), len(new)) - 1, 0) or                     (best and best[1] >= max(2, common - 1)):
                votes = {best[0]: best[1]}
            else:
                votes = {}
    if not votes:
        return None, set()
    # merge shifts within tolerance, keep the best supported
    best_shift, best_n = None, 0
    for s in list(votes):
        n = sum(v for d, v in votes.items() if abs(d - s) <= tol)
        if n > best_n or (n == best_n and best_shift is not None and abs(s) < abs(best_shift)):
            best_shift, best_n = s, n
    seen = set()
    ref_rows = prev if done is None else done      # rows that were actually read
    for j, (nl, nt) in enumerate(new):
        if any(pl == nl and abs((pt - nt) - best_shift) <= tol for pl, pt in ref_rows):
            seen.add(j)
    return best_shift, seen


def _clean(detail: str) -> list[str]:
    """Detail screen render -> normalised lines (refs and indentation gone)."""
    out = []
    for raw in detail.splitlines():
        s = raw.strip()
        if not s.startswith("-") or s.startswith("- keyboard"):
            continue
        s = _REF.sub("", s[1:].strip())
        s = re.sub(r"\[e\d+\]", "", s).strip()
        if s:
            out.append(s)
    return out


def _segments(label: str) -> list[str]:
    """the quoted pieces of a row label ('"Recipe photo" · "Title" · "Desc"')"""
    segs = re.findall(r'"((?:[^"\\]|\\.)*)"', label)
    if not segs:
        segs = [s.strip() for s in label.split("·")]
    return [s for s in segs if len(s) >= 4]


def _belongs(label: str, lines: list[str]) -> bool:
    """does the detail screen show what the row label says?  Every piece of
    the label must appear in the detail text."""
    body = " ".join(lines)
    segs = _segments(label)
    return bool(segs) and all(s in body for s in segs)


def _open_and_read(call, ref: str, label: str) -> tuple[list[str], bool]:
    """Tap the row and read its detail screen, re-reading (and re-tapping
    once) until the screen shows THIS row: a detail screen read too early is
    still the previous one, and two rows would then look identical."""
    call("phone_tap", {"target": ref})
    lines: list[str] = []
    for attempt in range(4):
        call("phone_wait", {"stable": 1, "timeout": 3})
        lines = _clean(call("phone_observe", {"mode": "full", "max_lines": 500}))
        if _belongs(label, lines):
            return lines, True
    return lines, False


STEP_FRACTION = 0.5


def _pitch(tops: list[int]) -> int | None:
    """the row-to-row distance when the rows sit on an even grid"""
    if len(tops) < 3:
        return None
    d = [b - a for a, b in zip(tops, tops[1:])]
    m = sorted(d)[len(d) // 2]
    return m if m > 20 and all(abs(x - m) <= 4 for x in d) else None


def locate(prev_whole: list[tuple[str, int]], new_whole: list[tuple[str, int]],
           expected: int, tol: int = 6, pitch: int | None = None) -> tuple[int | None, bool]:
    """How far the list moved between two reads, from the fully visible rows
    only.  Returns (shift, verified).  On an even grid the remainder modulo the
    pitch is exact; the whole number of rows is the one nearest the commanded
    distance — checked: every row of the new window that overlaps the old one
    must carry the same label there.  A smaller shift is accepted only if it
    verifies (the end of the list, where the list moves less than asked)."""
    if not pitch:
        pp = _pitch([t for _l, t in prev_whole])
        pn = _pitch([t for _l, t in new_whole])
        if not pp or not pn or abs(pp - pn) > 3:
            return None, False
        pitch = pp
    if not prev_whole or not new_whole:
        return None, False
    delta = (prev_whole[0][1] - new_whole[0][1]) % pitch
    k0 = max(0, round((expected - delta) / pitch))
    for k in range(k0, -1, -1):
        c = delta + k * pitch
        overlap = [(nl, nt) for nl, nt in new_whole if any(abs(pt - (nt + c)) <= tol for _pl, pt in prev_whole)]
        if not overlap:
            if k == k0:
                return c, False        # no whole row in common: cannot verify, trust the commanded distance
            continue
        ok = sum(1 for nl, nt in overlap if any(pl == nl and abs(pt - (nt + c)) <= tol for pl, pt in prev_whole))
        if ok == len(overlap):
            return c, True
    return None, False


def align_grid(prev_whole: list[tuple[str, int]], new_whole: list[tuple[str, int]],
               expected: int, tol: int = 6) -> int | None:
    """Scroll shift for a list whose rows sit on an even grid.  The remainder
    of the shift modulo the pitch is read exactly from the row positions; the
    number of whole rows moved is the one that puts the shift closest to the
    distance we asked for (a step varies by ~50 px, far less than half a pitch)."""
    pp = _pitch([t for _l, t in prev_whole])
    pn = _pitch([t for _l, t in new_whole])
    if not pp or not pn or abs(pp - pn) > 3 or not prev_whole or not new_whole:
        return None
    pitch = pp
    delta = (prev_whole[0][1] - new_whole[0][1]) % pitch
    k = round((expected - delta) / pitch)
    cand = delta + k * pitch
    return cand if cand >= -tol else None


def _list_bounds(call) -> tuple[int, int, int]:
    """(x, top, bottom) of the biggest list on screen, from phone_find"""
    best = (540, 400, 2200)
    area = 0
    for line in call("phone_find", {"role": "list"}).splitlines():
        m = re.search(r"bounds=\[(-?\d+),(-?\d+),(-?\d+),(-?\d+)\]", line)
        if m:
            l, t, r, b = map(int, m.groups())
            if (r - l) * (b - t) > area:
                area, best = (r - l) * (b - t), ((l + r) // 2, t, b)
    return best


def _step_down(call, x: int, top: int, bottom: int) -> None:
    """scroll the list by about 55 % of its height — a plain scroll swipe jumps
    nearly a whole window and rows would slip past between two reads"""
    dist = int((bottom - top) * STEP_FRACTION)
    y1 = bottom - 150
    # duration 1: no fling, so the list moves about as far as the finger
    call("phone_swipe", {"x1": x, "y1": y1, "x2": x, "y2": y1 - dist, "duration": 1})


def scan_list(call, hint: str = "", max_rows: int = 150, max_swipes: int = 60) -> str:
    def window():
        txt = call("phone_observe", {"mode": "full", "max_lines": 500})
        nodes = parse_tree(txt)
        fl = find_list(nodes, hint)
        if not fl:
            return None, None
        _, kids = fl
        bounds = parse_find(call("phone_find", {"role": "cell"}))
        rows = [(k["label"], bounds[k["ref"]][0], k["ref"], bounds[k["ref"]][1] - bounds[k["ref"]][0])
                for k in kids if k["ref"] in bounds]
        return rows, nodes

    # start at the top
    # (a scroll right after an app start can be swallowed while the list is
    # still settling: the top is reached only when two scrolls, a second
    # apart, both leave the list unchanged)
    still = 0
    for _ in range(max_swipes):
        r = call("phone_scroll", {"direction": "up", "max_swipes": 1})
        if "end of list" in r or "no visible change" in r:
            still += 1
            if still >= 2:
                break
            time.sleep(1.0)
        else:
            still = 0
    rows, nodes = window()
    if not rows:
        return ("phone_scan_list: no list of rows found on screen. Open the list first "
                "(rows must be cells), or pass `list` with part of the list's name.")
    records: list[dict] = []
    notes: list[str] = []
    prev_all: list[tuple[str, int]] = []
    prev_done: list[tuple[str, int]] = []
    prev_whole: list[tuple[str, int]] = []
    cum = 0                      # accumulated scroll distance since the first window
    pitch = None                 # row-to-row distance on an even grid
    first_top = None
    read_idx: set[int] = set()   # absolute row indexes already read
    lx, ltop, lbottom = _list_bounds(call)
    expected = int((lbottom - ltop) * STEP_FRACTION * 1.07)   # a swipe moves the list ~7 % further
    full_h = 0
    label_seen: dict[str, int] = {}
    swipes = 0
    while True:
        full_h = max([h for *_x, h in rows] + [full_h])
        # a row cut off at the top/bottom edge is opened only once it is fully
        # in view (tapping it scrolls the list and loses our place)
        # fully in view = full height (a row cut by a few px at the top reports
        # the clipped edge as its top, which would skew the grid phase)
        # rows differ in height (a row with a description is taller): a short
        # row is whole too when it does not touch the list's top/bottom edge
        # (the first window starts at the list's top, so its first row may)
        first = not prev_all
        whole = [h >= full_h - 2 or (bool(l) and (first or t > ltop + 3) and t + h < lbottom - 3)
                 for l, t, _r, h in rows]
        snap = list(rows)
        cur = [(l, t) for l, t, _r, _h in rows]
        new_whole = [(l, t) for (l, t, _r, _h), w in zip(rows, whole) if w]
        if pitch is None and not prev_all:
            pitch = _pitch([t for _l, t in new_whole])
            first_top = new_whole[0][1] if new_whole else None
        shift, seen_idx = None, set()
        if prev_all:
            shift, verified = locate(prev_whole, new_whole, expected, pitch=pitch)
            if shift is None:                          # not an even grid: fall back to label votes
                shift, seen_idx = align(prev_all, cur, done=prev_done, expected=expected)
            else:
                seen_idx = {j for j, ((l, t, _r, _h), w) in enumerate(zip(rows, whole))
                            if w and any(pl == l and abs(pt - (t + shift)) <= 6 for pl, pt in prev_done)}
                if not verified:
                    notes.append(f"INCOMPLETE: after swipe {swipes} the scroll distance could not be verified "
                                 f"(no row in common between two views), so reading stopped here — "
                                 f"the rows below were NOT read")
                    return _report(records, notes + _index_notes(read_idx))
            if shift is not None and shift > 0.65 * expected:
                expected = shift                       # follow the real step length
            if shift is not None:
                cum += shift
        if prev_all and shift is not None and abs(shift) <= 6 and len(prev_all) == len(cur):
            break                                    # the list did not move: that was the end
        if prev_all and shift is None:
            notes.append(f"INCOMPLETE: no overlap between the views at swipe {swipes}; reading stopped, later rows were NOT read")
            return _report(records, notes + _index_notes(read_idx))
        for j, (label, top, _old_ref, _h) in enumerate(list(rows)):
            if not whole[j]:
                continue
            if pitch:
                idx = round((top + cum - first_top) / pitch)
                if idx in read_idx:
                    continue
            elif j in seen_idx:
                continue
            if len(records) >= max_rows:
                notes.append(f"stopped at {max_rows} rows")
                break
            # references are renumbered each time the list is re-read: find
            # this row again by its label and position in the current window
            ref = next((r for l, t, r, _h in rows if l == label and abs(t - top) <= 6), None)
            if ref is None:
                notes.append(f"row {len(records) + 1} ({label!r}) vanished from the window; skipped")
                continue
            label_seen[label] = label_seen.get(label, 0) + 1
            ordinal = label_seen[label]
            if pitch:
                read_idx.add(idx)
            lines, verified = _open_and_read(call, ref, label)
            if not verified:
                notes.append(f"row {len(records) + 1} ({label!r}): the detail screen could not be matched to the row")
            ok = False
            for _ in range(2):
                call("phone_back", {})
                # the list may not be drawn yet: look again before pressing
                # back a second time (which would leave the app)
                for _look in range(3):
                    call("phone_wait", {"stable": 1, "timeout": 3})
                    rows2, _n = window()
                    # (the detail screen's tabs look like a list too: only
                    # the row we opened proves the list is back)
                    if rows2 and any(l == label and abs(t - top) <= 6 for l, t, _r, _h in rows2):
                        break
                    time.sleep(0.5)
                if rows2 and any(l == label and abs(t - top) <= 6 for l, t, _r, _h in rows2):
                    ok = True
                    rows = rows2
                    break
            records.append({"label": label, "ordinal": ordinal, "top": top, "lines": lines})
            if not ok:
                notes.append(f"lost the list after opening row {len(records)} ({label!r}); stopped")
                return _report(records, notes)
        else:
            if len(records) >= max_rows or swipes >= max_swipes:
                break
            # only rows we actually read count as "seen" for the next window
            prev_all = [(l, t) for l, t, _r, _h in snap]
            prev_done = [(l, t) for (l, t, _r, _h), w in zip(snap, whole) if w]
            prev_whole = list(prev_done)
            _step_down(call, lx, ltop, lbottom)
            swipes += 1
            rows2, _n = window()
            if not rows2:
                notes.append("the list disappeared while scrolling")
                break
            # a swipe right after a detail screen can be swallowed (the list
            # was still settling): a view identical to the last one is
            # the end of the list only if it stays identical after a retry
            for _retry in range(2):
                if [(l, t) for l, t, _r, _h in rows2] != prev_all:
                    break
                time.sleep(1.0)
                _step_down(call, lx, ltop, lbottom)
                r3, _n = window()
                if not r3:
                    break
                rows2 = r3
            rows = rows2
            continue
        break
    return _report(records, notes + _index_notes(read_idx))


def _index_notes(read_idx: set[int]) -> list[str]:
    if not read_idx:
        return []
    miss = [i for i in range(min(read_idx), max(read_idx) + 1) if i not in read_idx]
    return [f"missing row positions (never fully in view): {miss}"] if miss else []


def _report(records: list[dict], notes: list[str]) -> str:
    if not records:
        return "phone_scan_list: no rows read. " + "; ".join(notes)
    # lines present in every record are screen chrome (toolbar, buttons): drop them
    common = set(records[0]["lines"])
    for r in records[1:]:
        common &= set(r["lines"])
    for r in records:
        r["body"] = [x for x in r["lines"] if x not in common]
    groups: "OrderedDict[tuple, list[int]]" = OrderedDict()
    for i, r in enumerate(records, 1):
        groups.setdefault((r["label"], tuple(r["body"])), []).append(i)
    out = [f"scanned {len(records)} rows, each opened and read (top to bottom).",
           "Only TEXT was read. A type / category / status shown only as an icon or colour "
           "is NOT in these records: read it in the app (filter, detail or edit screen) "
           "— never infer it from a title."]
    sets = [v for v in groups.values() if len(v) > 1]
    if sets:
        out.append("IDENTICAL records (same row label and every field equal):")
        for v in sets:
            r = records[v[0] - 1]
            ords = ", ".join(f"#{i} (the {records[i-1]['ordinal']}{_suf(records[i-1]['ordinal'])} row labelled this way)" for i in v)
            out.append(f"  - {ords}: {r['label']}")
    else:
        out.append("IDENTICAL records: none.")
    same_label = {}
    for i, r in enumerate(records, 1):
        same_label.setdefault(r["label"], []).append(i)
    out.append("RECORDS (#row | row label | detail fields):")
    for i, r in enumerate(records, 1):
        out.append(f"#{i} | {r['label']} | " + " ¦ ".join(r["body"]))
    out.extend(notes)
    return "\n".join(out)


def _suf(n: int) -> str:
    return "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
