"""phone_read_grid's reader: pixel-drawn mazes of several sizes, with the
controls drawn under the grid and a toolbar divider above it, must come back
as an exact matrix (walls, goal, and the cell holding the piece)."""
import io
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PIL = pytest.importorskip("PIL")
pytest.importorskip("cv2")
from PIL import Image, ImageDraw  # noqa: E402

from androidworld.ocr import grid_from_png  # noqa: E402


def _maze_png(n: int, seed: int):
    rnd = random.Random(seed)
    cell = 492 // n
    img = Image.new("RGB", (1080, 2400), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 330, 1080, 332], fill=(220, 220, 220))        # toolbar divider
    ox, oy = 8, 338
    px, py = rnd.randrange(n), rnd.randrange(n)
    if (px, py) == (n - 1, n - 1):
        px = 0
    truth = []
    for i in range(n):
        row = []
        for j in range(n):
            goal = (i, j) == (n - 1, n - 1)
            wall = rnd.random() < 0.35 and (i, j) != (px, py) and not goal
            col = (0, 0, 0) if wall else ((0, 128, 0) if goal else (255, 255, 255))
            x0, y0 = ox + j * cell, oy + i * cell
            d.rectangle([x0, y0, x0 + cell, y0 + cell], fill=col, outline=(60, 60, 60))
            sym = "#" if wall else ("G" if goal else ".")
            if (i, j) == (px, py):                                  # an "X" drawn with two strokes
                a, b = int(cell * .28), int(cell * .72)
                d.line([x0 + a, y0 + a, x0 + b, y0 + b], fill=(0, 0, 0), width=max(2, cell // 14))
                d.line([x0 + a, y0 + b, x0 + b, y0 + a], fill=(0, 0, 0), width=max(2, cell // 14))
                sym += "*"
            row.append(sym)
        truth.append(row)
    for k, label in enumerate(("Up", "Down", "Left", "Right")):    # controls under the grid
        x0 = 8 + k * 110
        d.rectangle([x0, oy + n * cell + 12, x0 + 100, oy + n * cell + 70],
                    fill=(239, 239, 239), outline=(118, 118, 118))
        d.text((x0 + 22, oy + n * cell + 30), label, fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), truth


@pytest.mark.parametrize("n", [3, 4, 5, 6, 8, 10])
@pytest.mark.parametrize("region", [(0, 0, 0, 0), (0, 300, 1080, 900)])
def test_maze_read_exactly(n, region):
    png, truth = _maze_png(n, seed=n)
    res = grid_from_png(png, *region)
    assert "error" not in res, res
    assert (res["rows"], res["cols"]) == (n, n)
    assert res["matrix"] == truth


def test_blank_region_is_an_error_not_a_guess():
    img = Image.new("RGB", (1080, 2400), "white")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    assert "error" in grid_from_png(buf.getvalue(), 0, 300, 1080, 900)
