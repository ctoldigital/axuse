"""Host-side OCR for opaque screens (canvas, images, video frames).

axdroid itself is screenshot-free: the on-device app never captures pixels.
This module is the *host* escape hatch for screens with no semantics — it
screencaps over adb (a dev/USB affordance that exists already) and reads
text with the Windows OCR engine (Windows.Media.Ocr, offline).
"""

from __future__ import annotations

import asyncio
import io


async def _recognize(png: bytes) -> dict:
    from winrt.windows.graphics.imaging import BitmapDecoder, SoftwareBitmap, BitmapAlphaMode
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    stream = InMemoryRandomAccessStream()
    writer = stream.get_output_stream_at(0)
    dw = DataWriter(writer)
    dw.write_bytes(png)
    await dw.store_async()
    await writer.flush_async()

    decoder = await BitmapDecoder.create_async(stream)
    bmp = await decoder.get_software_bitmap_async()
    if bmp is None:
        raise RuntimeError("could not decode screencap")
    eng = OcrEngine.try_create_from_user_profile_languages()
    if eng is None:
        from winrt.windows.globalization import Language
        eng = OcrEngine.try_create_from_language(Language("en-US"))
    if eng is None:
        raise RuntimeError("no Windows OCR language installed")
    # OCR wants BGRA8; convert if needed
    from winrt.windows.graphics.imaging import BitmapPixelFormat
    if bmp.bitmap_pixel_format != BitmapPixelFormat.BGRA8:
        bmp2 = SoftwareBitmap.convert(bmp, BitmapPixelFormat.BGRA8, BitmapAlphaMode.PREMUL)
    else:
        bmp2 = bmp
    res = await eng.recognize_async(bmp2)
    lines = []
    for line in res.lines:
        lines.append({"text": line.text,
                      "x": int(line.words[0].bounding_rect.x) if line.words else 0,
                      "y": int(line.words[0].bounding_rect.y) if line.words else 0})
    return {"text": "\n".join(l["text"] for l in lines), "lines": lines}


def ocr_png(png: bytes) -> dict:
    # upscale 2x first: Windows OCR drops decimal points in small rendered
    # text ("$27.38" -> "$27 38") and receipts need the digits exact
    import io
    from PIL import Image
    img = Image.open(io.BytesIO(png)).convert("RGB")
    img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return asyncio.run(_recognize(buf.getvalue()))


def colors_png(png: bytes, x: int, y: int, w: int, h: int, top: int = 8) -> dict:
    """Dominant colors of a screen region (hex + share) — for pixel-only
    content (color swatches, palettes, drawings) that carries no a11y
    semantics. Quantized 5 bits/channel."""
    import collections
    from PIL import Image
    img = Image.open(io.BytesIO(png)).convert("RGB")
    x, y = max(0, int(x)), max(0, int(y))
    w = int(w) if w else img.width
    h = int(h) if h else img.height
    region = img.crop((x, y, min(img.width, x + w), min(img.height, y + h)))
    q = region.quantize(colors=64, method=Image.Quantize.MEDIANCUT).convert("RGB")
    counts = collections.Counter(q.getdata())
    total = sum(counts.values()) or 1
    out = []
    for (r, g, b), n in counts.most_common(top):
        out.append({"hex": f"#{r:02x}{g:02x}{b:02x}", "share": round(n / total, 3)})
    return {"region": [x, y, w, h], "colors": out}


def _cell_symbol(cell) -> tuple[str, bool]:
    """(colour symbol, has a glyph/mark inside) for one grid cell (RGB array)."""
    import numpy as np
    h, w = cell.shape[:2]
    inner = cell[int(h * 0.15):max(int(h * 0.15) + 1, int(h * 0.85)),
                 int(w * 0.15):max(int(w * 0.15) + 1, int(w * 0.85))].reshape(-1, 3).astype(int)
    lum = inner.mean(axis=1)
    # the cell's body colour is the majority colour, not the glyph on it
    q = (inner // 48)
    keys, counts = np.unique(q, axis=0, return_counts=True)
    major = keys[counts.argmax()]
    body = inner[(q == major).all(axis=1)].mean(axis=0)
    r, g, b = body
    if min(r, g, b) > 215:
        sym, light = ".", True
    elif max(r, g, b) < 60:
        sym, light = "#", False
    else:
        light = bool(body.mean() > 128)
        # nearest named colour by hue
        mx, mn = max(r, g, b), min(r, g, b)
        if mx - mn < 30:
            sym = "g"           # grey
        elif g >= r and g >= b:
            sym = "G" if b < g * 0.8 else "C"
        elif r >= g and r >= b:
            sym = "R" if g < r * 0.6 else ("Y" if b < r * 0.6 else "P")
        else:
            sym = "B"
    other = (lum < 110) if light else (lum > 150)
    return sym, bool(other.mean() > 0.02)


def grid_from_png(png: bytes, x: int = 0, y: int = 0, w: int = 0, h: int = 0,
                  rows: int = 0, cols: int = 0) -> dict:
    """Read a pixel-drawn grid (maze, board, tile map) as a matrix.

    The region only has to CONTAIN the grid: its bounding box is found from
    the non-white pixels (the outline / walls). Rows and columns are detected
    (the smallest n whose square cells are each a single colour) unless given.
    Every cell becomes a symbol: `.` white, `#` black, G/R/B/Y/C/P/g colours;
    a trailing `*` marks a glyph or mark drawn inside the cell (a player
    marker, a letter)."""
    import io
    import numpy as np
    from PIL import Image
    img = Image.open(io.BytesIO(png)).convert("RGB")
    x0, y0 = max(0, int(x)), max(0, int(y))
    x1 = min(img.width, x0 + int(w)) if w else img.width
    y1 = min(img.height, y0 + int(h)) if h else img.height
    reg = np.asarray(img.crop((x0, y0, x1, y1)))
    ink = reg.min(axis=2) < 235
    # ignore hairlines that span the whole region (a toolbar divider, a page
    # border): an isolated row/column of ink with no ink beside it
    def thin_lines(frac):
        """runs (<= 4 px) of near-full ink with no ink next to them"""
        out, i, n = [], 0, len(frac)
        while i < n:
            if frac[i] > 0.97:
                j = i
                while j + 1 < n and frac[j + 1] > 0.97:
                    j += 1
                before = frac[i - 1] if i > 0 else 0
                after = frac[j + 1] if j + 1 < n else 0
                if j - i + 1 <= 4 and before < 0.5 and after < 0.5:
                    out.append((i, j))
                i = j + 1
            else:
                i += 1
        return out
    for i, j in thin_lines(ink.mean(axis=1)):
        ink[i:j + 1, :] = False
    for i, j in thin_lines(ink.mean(axis=0)):
        ink[:, i:j + 1] = False
    ys, xs = np.where(ink)
    if len(ys) < 20:
        return {"error": "no grid found: the region is blank"}
    # the grid is the largest connected block of ink: controls drawn below
    # or beside it (buttons, labels) are separate blocks and must not widen
    # its box (a "5x5" maze came out as 553x581 because of the buttons)
    import cv2
    near = cv2.dilate(ink.astype(np.uint8), np.ones((7, 7), np.uint8))
    ncomp, lab, stats, _ = cv2.connectedComponentsWithStats(near, connectivity=8)
    if ncomp > 2:
        best = max(range(1, ncomp), key=lambda k: int(stats[k, cv2.CC_STAT_WIDTH]) * int(stats[k, cv2.CC_STAT_HEIGHT]))
        ys, xs = np.where((lab == best) & ink)
    gx0, gx1, gy0, gy1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    W, H = gx1 - gx0, gy1 - gy0
    grid = reg[gy0:gy1, gx0:gx1]

    def cells(nr, nc):
        out = []
        for i in range(nr):
            row = []
            for j in range(nc):
                c = grid[int(i * H / nr):int((i + 1) * H / nr), int(j * W / nc):int((j + 1) * W / nc)]
                row.append(_cell_symbol(c))
            out.append(row)
        return out

    def uniform(nr, nc):
        bad = 0
        for i in range(nr):
            for j in range(nc):
                c = grid[int(i * H / nr):int((i + 1) * H / nr), int(j * W / nc):int((j + 1) * W / nc)]
                c = c[int(len(c) * .12):int(len(c) * .88), int(c.shape[1] * .12):int(c.shape[1] * .88)]
                if len(c) == 0:
                    return False
                flat = (c.reshape(-1, 3) // 48)
                _, counts = np.unique(flat, axis=0, return_counts=True)
                if counts.max() / len(flat) < 0.85:
                    bad += 1
        return bad == 0

    if rows and cols:
        nr, nc = rows, cols
    else:
        nr = nc = 0
        for n in range(2, 17):                       # columns; rows from square cells
            m = int(round(H / (W / n)))
            if m >= 1 and abs(H - m * W / n) <= 0.04 * H and uniform(m, n):
                nr, nc = m, n
                break
        if not nr:
            return {"error": "could not detect the grid size; pass rows and cols",
                    "box": [x0 + int(gx0), y0 + int(gy0), int(W), int(H)]}
    sym = cells(nr, nc)
    return {"rows": nr, "cols": nc,
            "box": [x0 + int(gx0), y0 + int(gy0), int(W), int(H)],
            "cell_px": [round(W / nc), round(H / nr)],
            "matrix": [[s + ("*" if mk else "") for s, mk in row] for row in sym]}


def video_text_from_file(path: str, win_s: float = 2.0, step_s: float = 0.5,
                         sample_fps: float = 10.0) -> dict:
    """Read the text messages of a 'constant text over per-frame noise' video
    from a real screen RECORDING (adb screenrecord: frames carry true
    timestamps, ~30 fps while the video plays).

    The old reader averaged screencaps in fixed 8 s blocks assuming a 0.5 s
    cadence; screencap really takes 0.5–1.5 s, so a "block" spanned 16–24 s
    of video and averaged several messages into garbage, and frames of the
    file list after playback ended were averaged in too. Here:
      1. decode at sample_fps with timestamps; per-pixel motion locates the
         playing video (the noise region) → crop to it;
      2. frames whose crop barely changes (paused / player gone) are dropped;
      3. OCR the mean of every win_s window (step_s apart), so many windows
         lie fully inside one message whatever its timing;
      4. a reading counts only if it repeats in ≥ 2 consecutive windows
         (boundary windows that mix two messages don't repeat), then
         consecutive duplicates collapse.
    """
    import cv2
    import numpy as np
    from PIL import Image

    cap = cv2.VideoCapture(path)
    frames, stamps = [], []
    next_t = 0.0
    while True:
        ok, f = cap.read()
        if not ok:
            break
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if t + 1e-6 < next_t:
            continue
        next_t = t + 1.0 / sample_fps
        frames.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY))
        stamps.append(t)
    cap.release()
    if len(frames) < 4:
        return {"messages": [], "windows": 0, "frames": len(frames)}
    # 1. where does it move? (a noise video changes every pixel every frame).
    #    Count the share of frame PAIRS in which a pixel changed a lot: the
    #    noise changes in nearly all of them, while the player's controls
    #    fading out after a tap change in a few. Averaging the size of the
    #    change let a one-second overlay animation (13 new frames of a ~1 fps
    #    recording) widen the crop to the controls' text.
    small = [cv2.resize(f, (f.shape[1] // 4, f.shape[0] // 4)).astype(np.int16) for f in frames]
    changed = np.mean([np.abs(small[i] - small[i - 1]) > 24 for i in range(1, len(small))], axis=0)
    mask = changed > 0.5
    if mask.sum() < 50:   # too few pairs / a faint video: the old size-of-change rule
        motion = np.mean([np.abs(small[i] - small[i - 1]) for i in range(1, len(small))], axis=0)
        mask = motion > max(6.0, float(motion.max()) * 0.25)
    ys, xs = np.where(mask)
    if len(ys) == 0:
        return {"messages": [], "windows": 0, "frames": len(frames), "note": "nothing moving on screen"}
    y0, y1, x0, x1 = ys.min() * 4, (ys.max() + 1) * 4, xs.min() * 4, (xs.max() + 1) * 4
    crops = [f[y0:y1, x0:x1].astype(np.float32) for f in frames]
    # 2. playing frames only
    playing = [False] + [float(np.mean(np.abs(crops[i] - crops[i - 1]))) > 4.0
                         for i in range(1, len(crops))]
    if len(playing) > 1:
        playing[0] = playing[1]
    # 3. sliding windows
    readings = []
    play_idx = [i for i, p in enumerate(playing) if p]
    span = max(1e-6, stamps[-1] - stamps[0])
    rate = len(play_idx) / span
    # the emulator renders the video in software and can deliver only 1-3
    # frames/s. Fixed 2 s windows then hold 0-3 frames, and widening them
    # lets a message boundary blend into a fake word for many windows. Use
    # windows of 5 consecutive playing frames, one frame apart: the blend of
    # a boundary appears in one window, a real message in several.
    if rate < 3.0:
        windows = [play_idx[k:k + 5] for k in range(0, max(0, len(play_idx) - 4))]
    else:
        windows = []
        t = stamps[0]
        while t < stamps[-1]:
            windows.append([i for i, s_ in enumerate(stamps) if t <= s_ < t + win_s and playing[i]])
            t += step_s
    for idx in windows:
        if len(idx) >= (4 if rate >= 3.0 else 5):
            avg = np.mean([crops[i] for i in idx], axis=0).astype(np.uint8)
            img = Image.fromarray(avg)
            img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            res = asyncio.run(_recognize(buf.getvalue()))
            text = " ".join(l["text"].strip() for l in res["lines"] if l["text"].strip())
            readings.append(text)
        else:
            readings.append(None)   # gap: not playing
    # 4. stable readings only, in order; a neighbouring near-identical
    #    reading ("Ahrned" next to "Ahmed") is an OCR variant of the same
    #    message, not a new one — merge it and keep the variant seen more often
    import difflib
    runs: list[list] = []   # [text, windows]
    i = 0
    while i < len(readings):
        j = i
        while j + 1 < len(readings) and readings[j + 1] == readings[i]:
            j += 1
        if readings[i] and j > i:
            runs.append([readings[i], j - i + 1])
        i = j + 1
    merged: list[dict] = []
    for text, n in runs:
        # a real message stays for seconds (many windows); a misread shows
        # up in a few — "Leo" then "Leon" both long-lived stay two messages
        prev = merged[-1] if merged else None
        # the SAME text in two neighbouring runs (split by a one-window blip:
        # "Ava" '' "Ava") is one message: a constant-text video cannot show
        # one name twice in a row distinguishably (MarkorTranscribeVideo wrote
        # "Ava,Ava")
        if prev and text in prev["votes"]:
            prev["votes"][text] += n
            prev["best"] = max(prev["votes"], key=prev["votes"].get)
            continue
        # (judged by the ABSOLUTE length of the shorter run: relative length
        # dropped a real "Leo" — 7 windows — next to "Leon" — 18 windows)
        small = 3 if rate >= 3.0 else 2
        if prev and difflib.SequenceMatcher(None, prev["best"].lower(), text.lower()).ratio() >= 0.8 \
                and min(n, prev["votes"][prev["best"]]) <= small:
            m = merged[-1]
            m["votes"][text] = m["votes"].get(text, 0) + n
            m["best"] = max(m["votes"], key=m["votes"].get)
        else:
            merged.append({"best": text, "votes": {text: n}})
    messages = [m["best"] for m in merged]
    return {"messages": messages, "windows": len(readings), "frames": len(frames),
            "playing_s": round(sum(1 for p in playing if p) / sample_fps, 1)}


def video_text(frames: list[bytes], window_s: float = 8.0) -> dict:
    """Read text from a playing 'noise + constant text' video.

    The benchmark's videos draw each message over fresh per-frame noise for
    `window_s` seconds: averaging the frames of one window cancels the noise
    (sigma / sqrt(n)) while the text stays at full contrast. Returns the
    sequence of distinct messages in order.
    """
    import numpy as np
    from PIL import Image

    if not frames:
        return {"messages": [], "windows": 0}
    imgs = [np.asarray(Image.open(io.BytesIO(p)).convert("L"), dtype=np.float32)
            for p in frames]
    per_window = max(1, int(window_s / 0.5))   # capture cadence is 0.5s
    messages: list[str] = []
    for i in range(0, len(imgs), per_window):
        chunk = imgs[i:i + per_window]
        if len(chunk) < 3:
            break
        avg = np.mean(chunk, axis=0).astype(np.uint8)
        img = Image.fromarray(avg)
        img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        res = asyncio.run(_recognize(buf.getvalue()))
        text = " ".join(l["text"].strip() for l in res["lines"] if l["text"].strip())
        if text and (not messages or text != messages[-1]):
            messages.append(text)
    return {"messages": messages, "windows": (len(imgs) + per_window - 1) // per_window}
