"""phone_scan_list: reads every row of a scrolling list exactly once and reports
identical records — checked against a simulated phone that speaks the same text
formats as the real tools (observe / find / tap / back / swipe)."""
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from androidworld import scan  # noqa: E402

LIST_TOP, LIST_BOTTOM, ROW_H, PITCH = 422, 2337, 315, 336


class FakePhone:
    """a list of `rows` (label, detail-lines); a swipe moves the list by the
    finger distance plus a little overshoot, clamped at the ends"""

    def __init__(self, rows, overshoot=40, stale_detail=False):
        self.rows = rows
        self.offset = 0
        self.screen = "list"
        self.cur = None
        self.overshoot = overshoot
        self.n = 100
        self.stale_detail = stale_detail
        self._last_detail = None
        self.max_off = max(0, 21 + len(rows) * PITCH - (LIST_BOTTOM - LIST_TOP))

    def _tops(self):
        out = []
        for i in range(len(self.rows)):
            top = LIST_TOP + 21 + i * PITCH - self.offset
            bottom = top + ROW_H
            vis_top, vis_bottom = max(top, LIST_TOP), min(bottom, LIST_BOTTOM)
            if vis_bottom - vis_top > 4:
                out.append((i, vis_top if top < LIST_TOP else top, vis_bottom - (vis_top if top < LIST_TOP else top)))
        return out

    def call(self, name, args):
        if name == "phone_observe":
            self.n += 100                     # refs are renumbered on every read
            if self.screen == "detail":
                lab, lines = self.rows[self.cur]
                shown = self._last_detail if (self.stale_detail and self._last_detail and random.random() < 0.3) else lines
                self._last_detail = lines
                return "app: x\n- image-button \"Navigate up\" [e%d]\n" % self.n + "".join(
                    f'- text "{t}" [e{self.n + 1 + i}]\n' for i, t in enumerate(shown))
            txt = "app: x\n- list \"Recipes\" [e%d]\n" % self.n
            for i, top, h in self._tops():
                txt += f'  - cell {self.rows[i][0]} [e{self.n + 1 + i}]\n'
            return txt
        if name == "phone_find":
            if args.get("role") == "list":
                return f'e1 list "Recipes" bounds=[0,{LIST_TOP},1080,{LIST_BOTTOM}]'
            return "\n".join(f'e{self.n + 1 + i} cell "" bounds=[21,{top},1059,{top + h}]'
                             for i, top, h in self._tops())
        if name == "phone_tap":
            ref = int(args["target"][1:]) - self.n - 1
            assert 0 <= ref < len(self.rows), "stale ref used"
            self.cur, self.screen = ref, "detail"
            return "tap"
        if name == "phone_back":
            self.screen = "list"
            return "back"
        if name == "phone_wait":
            return "ok"
        if name == "phone_swipe":
            dist = args["y1"] - args["y2"]
            self.offset = min(self.max_off, max(0, self.offset + dist + self.overshoot))
            return "swipe"
        if name == "phone_scroll":
            if args["direction"] == "up":
                self.offset = 0
            return "scrolled"
        raise AssertionError(name)


def _rows(n, dup_pairs=(), seed=1):
    rnd = random.Random(seed)
    titles = ["Soup", "Pasta", "Toast", "Salad", "Stew"]
    rows = []
    for i in range(n):
        t = rnd.choice(titles)
        desc = rnd.choice(["quick", "healthy", "ideal"])
        det = [f"{t} {i % 3}", f"{rnd.randint(1, 8)} servings", f"{rnd.randint(1, 9)} hrs", desc, f"uid{i}"]
        rows.append([f'"Recipe photo" · "{t} {i % 3}" · "{desc}"', det])
    for a, b in dup_pairs:                    # make rows a and b exact copies
        rows[b] = [rows[a][0], list(rows[a][1])]
    return [(r[0], r[1]) for r in rows]


@pytest.mark.parametrize("frac", [0.3, 0.4, 0.5, 0.6])
@pytest.mark.parametrize("n,dups", [(12, [(2, 3)]), (32, [(19, 20)]), (45, [(5, 40), (10, 11)]), (9, [])])
def test_scan_reads_each_row_once_and_finds_duplicates(frac, n, dups, monkeypatch):
    monkeypatch.setattr(scan, "STEP_FRACTION", frac)
    rows = _rows(n, dups)
    phone = FakePhone(rows)
    out = scan.scan_list(phone.call)
    assert f"scanned {n} rows" in out, out[:300]
    assert "INCOMPLETE" not in out and "missing" not in out
    found = [line for line in out.splitlines() if line.startswith("  - #")]
    assert len(found) == len(dups), out[:600]
    for (a, b) in dups:
        assert any(f"#{a + 1} " in f and f"#{b + 1} " in f for f in found), (a, b, found)


def test_a_stale_detail_screen_is_not_taken_for_the_row(monkeypatch):
    """a detail read too early still shows the previous row: it must be re-read
    (otherwise two rows look identical)"""
    random.seed(3)
    rows = _rows(14, [])
    out = scan.scan_list(FakePhone(rows, stale_detail=True).call)
    assert f"scanned 14 rows" in out
    assert "IDENTICAL records: none." in out, out[:500]


class VarPhone(FakePhone):
    """rows of two heights (a row with a description is taller)"""

    def __init__(self, rows, **kw):
        super().__init__(rows, **kw)
        self.hs = [ROW_H + (120 if i % 3 == 0 else 0) for i in range(len(rows))]
        self.max_off = max(0, 21 + sum(h + 21 for h in self.hs) - (LIST_BOTTOM - LIST_TOP))

    def _tops(self):
        out, y = [], LIST_TOP + 21 - self.offset
        for i, h in enumerate(self.hs):
            top, bottom = y, y + h
            vt, vb = max(top, LIST_TOP), min(bottom, LIST_BOTTOM)
            if vb - vt > 4:
                out.append((i, vt if top < LIST_TOP else top, vb - (vt if top < LIST_TOP else top)))
            y += h + 21
        return out


@pytest.mark.parametrize("n", [9, 12, 30])
def test_rows_of_different_heights_are_all_read(n):
    """a short row must not be taken for a clipped one just because other
    rows are taller (a list with only 2 described rows was read as 2 rows)"""
    out = scan.scan_list(VarPhone(_rows(n, [])).call)
    assert f"scanned {n} rows" in out, out[:400]


def test_parse_tree_and_find_list():
    txt = ('app: x\n- list "Recipes" [e47]\n'
           '  - cell "Recipe photo" · "A" · "d1" [e48]\n'
           '  - cell "Recipe photo" · "B" · "d2" [e54]\n'
           '- image-button "New Recipe" [e84]\n')
    nodes = scan.parse_tree(txt)
    idx, kids = scan.find_list(nodes)
    assert [k["ref"] for k in kids] == ["e48", "e54"]
    assert scan.parse_find('e48 cell "" bounds=[21,443,1059,758]') == {"e48": (443, 758)}
