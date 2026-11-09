#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Selftest for tvlight.py without a Pi: synthetic frames in every format tvlight reads,
the colour conversion, the zone mapping and the state gate. Runs on the PC (fcntl is
stubbed where missing) and on the Pi. Exit code 0 = all green.

    python test/tvlight_selftest.py
"""
import json, os, re, struct, sys, types

if "fcntl" not in sys.modules:
    try:
        import fcntl  # noqa: F401
    except ImportError:  # Windows: only the ioctl wrappers need it, none run here
        sys.modules["fcntl"] = types.ModuleType("fcntl")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON_DIR = os.path.join(ROOT, "service.tvlight")
LIB = os.path.join(ADDON_DIR, "resources", "lib")
sys.path.insert(0, LIB)
import tvlight as T  # noqa: E402

FAILS = []
def check(name, ok, detail=""):
    print("%-4s %s %s" % ("ok" if ok else "FAIL", name, "" if ok else detail))
    if not ok:
        FAILS.append(name)

# --- test picture --------------------------------------------------------------
# Left edge: TOP_LEFT in the upper half, BOT_LEFT in the lower; right edge RIGHT;
# everything between EDGE_PX and w - EDGE_PX is MIDDLE and must not leak into the
# zones. EDGE_PX covers every edge width tvlight uses (10 % linear = 192 px, two
# SAND columns = 256 px at 8 bit, 192 px at 10 bit).
W, H, EDGE_PX = 1920, 1080, 288
TOP_LEFT, BOT_LEFT, RIGHT, MIDDLE = (200, 40, 40), (30, 60, 180), (20, 160, 90), (250, 250, 250)
CSC = (0.2126, 0.0722, True)  # BT.709 limited, what the Pi reports for video
COUNT = 27
WANTED = {("left", COUNT), ("right", COUNT)}

def rgb_to_ycc(rgb, csc=CSC):
    kr, kb, limited = csc
    r, g, b = (c / 255 for c in rgb)
    y = kr * r + (1 - kr - kb) * g + kb * b
    cb, cr = (b - y) / (2 * (1 - kb)), (r - y) / (2 * (1 - kr))
    if limited:
        return round(16 + 219 * y), round(128 + 224 * cb), round(128 + 224 * cr)
    return round(255 * y), round(128 + 255 * cb), round(128 + 255 * cr)

def row(left, middle, right, unit, n_left=EDGE_PX, width=None):
    """One row: unit(colour) gives the bytes (or samples) of one pixel or pair. width
    defaults to the current W (tests may change W/H for other picture sizes)."""
    width = W if width is None else width
    return unit(left) * n_left + unit(middle) * (width - 2 * n_left) + unit(right) * n_left

def fourcc_code(s):
    return struct.unpack("<I", s.encode())[0]

def fb(fourcc, pitches, offsets, mod=0, w=None, h=None):
    w, h = W if w is None else w, H if h is None else h
    cmd = T.FbCmd2(width=w, height=h, pixel_format=fourcc_code(fourcc))
    for i, (p, o) in enumerate(zip(pitches, offsets)):
        cmd.pitches[i], cmd.offsets[i], cmd.modifier[i] = p, o, mod
    return cmd

def pad(b, pitch):
    return b + b"\0" * (pitch - len(b))

def ycc_rows():
    """(y_top, y_bot, cb..., cr...) code values for the left edges, right, middle, black."""
    return {k: rgb_to_ycc(c) for k, c in
            (("tl", TOP_LEFT), ("bl", BOT_LEFT), ("r", RIGHT), ("m", MIDDLE), ("k", BAR_RGB[0]))}

BAR_RGB = [(0, 0, 0)]  # colour of the bars in the test frames; a test may make them dark grey

def stack(top, bot, black, rows, bar):
    """rows rows: bar black, the upper half top, the lower half bot, bar black."""
    return black * bar + top * (rows // 2 - bar) + bot * (rows // 2 - bar) + black * bar

def frame_xr24(bar=0):
    pitch = W * 4 + 64
    px = lambda c: bytes((c[2], c[1], c[0], 0))
    top, bot = pad(row(TOP_LEFT, MIDDLE, RIGHT, px), pitch), pad(row(BOT_LEFT, MIDDLE, RIGHT, px), pitch)
    black = pad(row((0, 0, 0), (0, 0, 0), (0, 0, 0), px), pitch)
    return stack(top, bot, black, H, bar), fb("XR24", [pitch], [0])

def frame_yu12(bar=0):
    q = ycc_rows()
    py, pc = W + 32, W // 2 + 16
    def plane(i, pitch, n, width, rows, b):
        r = lambda k: pad(row(q[k][i:i + 1], q["m"][i:i + 1] if k != "k" else q["k"][i:i + 1],
                              q["r"][i:i + 1] if k != "k" else q["k"][i:i + 1], bytes, n, width), pitch)
        return stack(r("tl"), r("bl"), r("k"), rows, b)
    y = plane(0, py, EDGE_PX, W, H, bar)
    u, v = plane(1, pc, EDGE_PX // 2, W // 2, H // 2, bar // 2), plane(2, pc, EDGE_PX // 2, W // 2, H // 2, bar // 2)
    return y + u + v, fb("YU12", [py, pc, pc], [0, len(y), len(y) + len(u)])

def nv12_rows(q, uv_unit):
    """Luma and interleaved chroma rows (unpadded): top half, bottom half, black."""
    ly = lambda k, m="m", r="r": row(q[k][0:1], q[m][0:1], q[r][0:1], bytes)
    lc = lambda k, m="m", r="r": row(q[k][1:3], q[m][1:3], q[r][1:3], uv_unit, EDGE_PX // 2, W // 2)
    return ly("tl"), ly("bl"), ly("k", "k", "k"), lc("tl"), lc("bl"), lc("k", "k", "k")

def frame_nv12(bar=0):
    yt, yb, yk, ct, cb, ck = nv12_rows(ycc_rows(), bytes)
    p = W + 64
    y = stack(pad(yt, p), pad(yb, p), pad(yk, p), H, bar)
    c = stack(pad(ct, p), pad(cb, p), pad(ck, p), H // 2, bar // 2)
    return y + c, fb("NV12", [p, p], [0, len(y)])

def sand(fourcc, rows, bar):
    """Broadcom SAND128: 128-byte columns, each colh rows: luma rows, then chroma rows."""
    yt, yb, yk, ct, cb, ck = rows
    colh = H * 3 // 2
    out = []
    for c in range(len(yt) // 128):
        s = slice(c * 128, (c + 1) * 128)
        out.append(stack(yt[s], yb[s], yk[s], H, bar) + stack(ct[s], cb[s], ck[s], H // 2, bar // 2))
    mod = (T.MOD_VENDOR_BROADCOM << 56) | (colh << 8) | T.MOD_SAND128
    return b"".join(out), fb(fourcc, [len(yt), len(yt)], [0, 128 * H], mod)

def frame_nv12_sand(bar=0):
    return sand("NV12", nv12_rows(ycc_rows(), bytes), bar)

def frame_p030_sand(bar=0, pad_value=0, pad_bits=0):
    """P030: three 10-bit samples per little-endian word, bits 0-9, 10-19, 20-29 (bits
    30-31 are padding: pad_bits sets them). Rows are padded to whole 96-px columns with
    pad_value (a width like 1280 leaves the last column part empty)."""
    q = {k: tuple(v * 4 for v in c) for k, c in ycc_rows().items()}
    def packed(samples):
        samples = samples + [pad_value] * (-len(samples) % 96)
        words = [samples[i] | samples[i + 1] << 10 | samples[i + 2] << 20 | pad_bits << 30
                 for i in range(0, len(samples), 3)]
        return struct.pack("<%dI" % len(words), *words)
    ly = lambda k, m="m", r="r": packed(row([q[k][0]], [q[m][0]], [q[r][0]], list))
    lc = lambda k, m="m", r="r": packed(row(list(q[k][1:3]), list(q[m][1:3]), list(q[r][1:3]), list, EDGE_PX // 2, W // 2))
    return sand("P030", (ly("tl"), ly("bl"), ly("k", "k", "k"), lc("tl"), lc("bl"), lc("k", "k", "k")), bar)

def frame_p030_flat(y, cb, cr, pad_bits=0):
    """A P030 picture of one colour (10-bit samples y, cb, cr; pad_bits in bits 30-31)."""
    def packed(samples):
        samples = samples + [0] * (-len(samples) % 96)
        return struct.pack("<%dI" % (len(samples) // 3),
                           *[samples[i] | samples[i + 1] << 10 | samples[i + 2] << 20 | pad_bits << 30
                             for i in range(0, len(samples), 3)])
    ly, lc = packed([y] * W), packed([cb, cr] * (W // 2))
    return sand("P030", (ly, ly, ly, lc, lc, lc), 0)

def zones_match(name, colors, tol):
    left, right = colors["left", COUNT], colors["right", COUNT]
    half = COUNT // 2  # zone 13 straddles the middle row
    def worst(zs, want):
        return max(max(abs(a - b) for a, b in zip(z, want)) for z in zs)
    wl_top, wl_bot = worst(left[:half], TOP_LEFT), worst(left[half + 1:], BOT_LEFT)
    wr = worst(right, RIGHT)
    check("%s: left top/bottom, right within %d" % (name, tol), max(wl_top, wl_bot, wr) <= tol,
          "worst %d/%d/%d; left[0] %s left[-1] %s right[0] %s" % (wl_top, wl_bot, wr, left[0], left[-1], right[0]))

# --- tests -----------------------------------------------------------------------

def test_formats():
    # P030 reads 6-bit probes: up to 2 code steps per channel on a flat area, B = Y +
    # 1.856 Cb turns that into ~6.5 RGB steps. Real pictures average it out (<= 3
    # against exact unpacking). Hence its tolerance 8 in FORMATS.
    for name, make, tol in FORMATS:
        buf, cmd = make()
        try:
            colors = T.frame_colors(buf, cmd, WANTED, CSC)
        except Exception as e:  # report and go on with the next format
            check(name, False, "%s: %s" % (type(e).__name__, e))
            continue
        zones_match(name, colors, tol)


def test_p030_flat():
    """Flat P030 pictures: the neutral ends must come out neutral (limited black must not
    reconstruct as RGB 6, 0, 7), and the two padding bits of each word must not count."""
    want = {"black": (64, 512, 512, (0, 0, 0)), "grey": (512, 512, 512, (130, 130, 130)),
            "white": (940, 512, 512, (255, 255, 255))}
    for pad_bits in (0, 1, 2, 3):
        for name, (y, cb, cr, rgb) in want.items():
            buf, cmd = frame_p030_flat(y, cb, cr, pad_bits)
            c = T.frame_colors(buf, cmd, {("left", 9), ("top", 8)}, CSC)
            worst = max(max(abs(a - b) for a, b in zip(z, rgb)) for z in c["left", 9] + c["top", 8])
            check("P030 flat %s, padding bits %d: within 1 of %s" % (name, pad_bits, rgb),
                  worst <= 1, (worst, c["left", 9][0], c["top", 8][0]))

def test_p030_padding_bits():
    """The test picture with both padding bits set in every word reads like the clean one."""
    buf, cmd = frame_p030_sand(pad_bits=3)
    zones_match("P030 SAND128, padding bits set", T.frame_colors(buf, cmd, WANTED, CSC), 8)

FORMATS = (("XR24 linear", frame_xr24, 0), ("YU12 linear", frame_yu12, 3), ("NV12 linear", frame_nv12, 3),
           ("NV12 SAND128", frame_nv12_sand, 3), ("P030 SAND128", frame_p030_sand, 8))
ACROSS = 20  # top/bottom zones: 96 px each, so zones 0-2 and 17-19 lie inside the 288 px edge colours

def test_top_bottom():
    def worst(zs, want):
        return max(max(abs(a - b) for a, b in zip(z, want)) for z in zs)
    for name, make, tol in FORMATS:
        buf, cmd = make()
        try:
            c = T.frame_colors(buf, cmd, {("top", ACROSS), ("bottom", ACROSS)}, CSC)
        except Exception as e:
            check(name + " top/bottom", False, "%s: %s" % (type(e).__name__, e))
            continue
        top, bot = c["top", ACROSS], c["bottom", ACROSS]
        w = [worst(top[:3], TOP_LEFT), worst(top[3:17], MIDDLE), worst(top[17:], RIGHT),
             worst(bot[:3], BOT_LEFT), worst(bot[3:17], MIDDLE), worst(bot[17:], RIGHT)]
        check("%s: top and bottom zones left->right within %d" % (name, tol), max(w) <= tol,
              "worst %s; top %s bottom %s" % (w, top[:4], bot[:4]))

BAR = 132  # rows of black at the top and the bottom: a 2.4:1 film in a 16:9 frame

def test_grey_bars():
    """Dark grey "bars" (Y about 27, a dark studio wall) must not count as letterbox: the
    threshold frame_colors applies is video black + Bars.BLACK."""
    BAR_RGB[0] = (13, 13, 13)
    try:
        y = rgb_to_ycc(BAR_RGB[0])[0]
        buf, cmd = frame_yu12(BAR)
        bars = T.Bars()
        for now in (0.0, 0.5, 1.2, 2.4):
            T.frame_colors(buf, cmd, WANTED, CSC, bars=bars, now=now)
        check("frame: dark grey rows (Y %d) are no bars" % y, bars.rows == 0, bars.rows)
    finally:
        BAR_RGB[0] = (0, 0, 0)

def test_letterbox():
    half = COUNT // 2
    for name, make, tol in FORMATS:
        if name.startswith("XR24"):
            continue  # bars are looked for in video planes only
        buf, cmd = make(BAR)
        plain = T.frame_colors(buf, cmd, WANTED, CSC)
        check("%s letterbox: without detection the top zone is black" % name,
              max(plain["left", COUNT][0]) <= tol, plain["left", COUNT][0])
        bars = T.Bars()
        T.frame_colors(buf, cmd, WANTED, CSC, bars=bars, now=0.0)
        early = T.frame_colors(buf, cmd, WANTED, CSC, bars=bars, now=0.5)["left", COUNT][0]
        check("%s letterbox: not taken before %.1f s" % (name, T.Bars.CONFIRM), max(early) <= tol, early)
        c = T.frame_colors(buf, cmd, WANTED | {("top", ACROSS)}, CSC, bars=bars, now=1.5)
        left = c["left", COUNT]
        w = max(max(abs(a - b) for a, b in zip(z, TOP_LEFT)) for z in left[:half])
        wb = max(max(abs(a - b) for a, b in zip(z, BOT_LEFT)) for z in left[half + 1:])
        check("%s letterbox: bars %d rows found, zones span the picture" % (name, bars.rows),
              abs(bars.rows - BAR) <= 8 and max(w, wb) <= tol, "rows %d, worst %d/%d, left[0] %s" % (bars.rows, w, wb, left[0]))
        tz = c["top", ACROSS][:3]
        check("%s letterbox: top strip below the bar" % name,
              max(max(abs(a - b) for a, b in zip(z, TOP_LEFT)) for z in tz) <= tol, tz)
        bz = T.frame_colors(buf, cmd, {("bottom", ACROSS)}, CSC, bars=bars, now=1.6)["bottom", ACROSS][:3]
        check("%s letterbox: bottom strip above the bar" % name,
              max(max(abs(a - b) for a, b in zip(z, BOT_LEFT)) for z in bz) <= tol, bz)

def test_bars_logic():
    H_, rpv, black = 1080, 4, 16 + T.Bars.BLACK
    n = H_ // rpv
    def prof(top=0, bottom=0, value=100, extra=()):
        p = [value] * n
        for i in range(top // rpv):
            p[i] = 16
        for i in range(n - bottom // rpv, n):
            p[i] = 16
        for i, v in extra:
            p[i] = v
        return p
    b = T.Bars()
    lb = prof(132, 132)
    b.update(lb, rpv, H_, black, 0.0)
    check("bars: first sight is not taken", b.update(lb, rpv, H_, black, 0.5) == 0, b.rows)
    check("bars: taken after confirm", b.update(lb, rpv, H_, black, 1.1) == 132, b.rows)
    check("bars: all dark keeps them", b.update([16] * n, rpv, H_, black, 2.0) == 132, b.rows)
    for name, gap in (("all dark", [16] * n), ("over the size limit", prof(400, 400))):
        b = T.Bars()
        b.update(lb, rpv, H_, black, 0.0)
        b.update(gap, rpv, H_, black, 0.5)
        got = b.update(lb, rpv, H_, black, 1.1)
        check("bars: a %s profile in between restarts the wait" % name, got == 0, got)
        check("bars: ... and the bar is taken a full %.1f s later" % T.Bars.CONFIRM,
              b.update(lb, rpv, H_, black, 2.2) == 132, b.rows)
    b = T.Bars()
    b.update(lb, rpv, H_, black, 0.0); b.update(lb, rpv, H_, black, 1.1)
    full = prof()
    b.update(full, rpv, H_, black, 3.0)
    check("bars: picture in the bar shrinks them after %.1f s" % T.Bars.SHRINK,
          b.update(full, rpv, H_, black, 3.0 + T.Bars.SHRINK + 0.01) == 0, b.rows)
    b = T.Bars()
    sky = prof(120, 0)  # below MAX, so the bottom check is what rejects it
    for t_ in (0.0, 2.0, 4.0):
        b.update(sky, rpv, H_, black, t_)
    check("bars: dark sky (bottom bright) is no bar", b.rows == 0, b.rows)
    b = T.Bars()
    subs = prof(132, 132, extra=[(n - 20, 180), (n - 19, 180)])  # subtitle rows inside the bottom bar
    for t_ in (0.0, 1.2):
        b.update(subs, rpv, H_, black, t_)
    check("bars: subtitles in the bottom bar do not break them", b.rows == 132, b.rows)
    b = T.Bars()
    huge = prof(400, 400)
    for t_ in (0.0, 2.0):
        b.update(huge, rpv, H_, black, t_)
    check("bars: above %d %% is a dark scene, not a bar" % (T.Bars.MAX * 100), b.rows == 0, b.rows)
    b = T.Bars()
    studio = prof(232, 232)
    studio = [27 if v == 16 else v for v in studio]  # dark studio background, not black
    for t_ in (0.0, 1.2, 2.4):
        b.update(studio, rpv, H_, 16 + T.Bars.BLACK, t_)
    check("bars: dark background at 27 is no bar (seen in a talk show)", b.rows == 0, b.rows)
    b = T.Bars()
    for t_ in (0.0, 1.2):
        b.update(lb, rpv, H_, black, t_)
    b.update(prof(), rpv, 720, black, 2.0)
    check("bars: another picture size starts over", b.rows == 0 and b.h == 720, (b.rows, b.h))
    b = T.Bars()
    seq = [b.due(x) for x in (0.0, 0.1, 0.19, 0.25, 0.3, 0.46)]
    check("bars: profile at most every %.1f s" % T.Bars.EVERY, seq == [True, False, False, True, False, True], seq)

def test_depth():
    """depth is applied: at 20 % the left strip (384 px) reaches into MIDDLE."""
    buf, cmd = frame_yu12()
    near = T.frame_colors(buf, cmd, {("left", COUNT)}, CSC, depth=0.10)["left", COUNT][0]
    wide = T.frame_colors(buf, cmd, {("left", COUNT)}, CSC, depth=0.20)["left", COUNT][0]
    check("depth 20 %% mixes in the middle colour", max(abs(a - b) for a, b in zip(near, wide)) > 20, (near, wide))

def test_odd_width():
    """1280x720 10-bit SAND: the last 96-px column holds only 32 px, the rest is padding
    (bright here, to make any read of it visible)."""
    global W, H
    saved = W, H
    W, H = 1280, 720
    try:
        buf, cmd = frame_p030_sand(pad_value=1020)
        c = T.frame_colors(buf, cmd, {("right", 18), ("top", 20)}, CSC)
        w = max(max(abs(a - b) for a, b in zip(z, RIGHT)) for z in c["right", 18])
        check("P030 1280 px: right edge ignores the column padding", w <= 8, (w, c["right", 18][0]))
        w = max(max(abs(a - b) for a, b in zip(z, RIGHT)) for z in c["top", 20][-2:])
        check("P030 1280 px: top zones at the right ignore the padding", w <= 8, (w, c["top", 20][-2:]))
        # a right strip exactly as deep as the partial column: any padding read shows
        c = T.frame_colors(buf, cmd, {("right", 18)}, CSC, depth=32 / 1280)
        w = max(max(abs(a - b) for a, b in zip(z, RIGHT)) for z in c["right", 18])
        check("P030 1280 px: a strip inside the partial column ignores the padding", w <= 8, (w, c["right", 18][0]))
    finally:
        W, H = saved

def test_test_pattern():
    wanted = {("left", 27), ("right", 27), ("top", 20)}
    a = T.test_pattern(wanted, 0.0)
    b = T.test_pattern(wanted, T.PATTERN_LAP / 2)
    bright = lambda zs: [i for i, c in enumerate(zs) if max(c) >= 250]
    check("pattern: one bright dot per edge", all(len(bright(a[k])) == 1 for k in a), {k: bright(a[k]) for k in a})
    check("pattern: edges in their own colours", a["left", 27][0] == T.PATTERN["left"] and
          max(a["right", 27][5]) < 40 and a["top", 20][0] == T.PATTERN["top"], (a["left", 27][0], a["top", 20][0]))
    check("pattern: the dot moves (half a lap = half the edge)",
          bright(b["left", 27]) == [13] and bright(b["top", 20]) == [10], (bright(b["left", 27]), bright(b["top", 20])))
    # through the zone mapping: a strip with LED 0 at the bottom sees the dot run upwards
    z = {"edge": "left", "count": 27, "use": 27, "top_first": False}
    leds = T.zone_colors(a, z)
    check("pattern: reversed strip lights its last LED first", bright(leds) == [26], bright(leds))

def test_format_handles_closed():
    """Planes._format closes the GEM handles of GETFB2 even when the FourCC is not ASCII."""
    closed = []
    real = T.getfb2, T.close_handles
    T.getfb2 = lambda card, fb_id: fb("XR24", [4], [0])
    T.close_handles = lambda card, cmd: closed.append(cmd)
    try:
        p = T.Planes.__new__(T.Planes)
        p.card, p.formats = None, {}
        ok = p._format(7)
        check("planes: a valid fb is read and its handles closed", ok == "XR24" and len(closed) == 1, (ok, len(closed)))
        T.getfb2 = lambda card, fb_id: T.FbCmd2(fb_id=fb_id, pixel_format=0xFFFFFFFF)
        try:
            p._format(8)
            check("planes: a non-ASCII FourCC is refused", False, "accepted")
        except ValueError:
            check("planes: a non-ASCII FourCC is refused", True)
        check("planes: handles closed after the failed decode", len(closed) == 2, len(closed))
    finally:
        T.getfb2, T.close_handles = real

def test_pick():
    cases = (({"AR24": 1, "YU12": 2}, "YU12"), ({"AR24": 1, "P030": 2}, "P030"),
             ({"XR24": 1, "NV12": 2}, "XR24"), ({"AR24": 1}, "AR24"), ({}, None))
    got = [T.pick(planes)[1] for planes, _ in cases]
    check("pick: GUI alone > video > GUI with alpha", got == [w for _, w in cases], got)

def test_config_copy():
    raw = {"edges": [{"edge": "left", "leds": 5}]}
    T.load_config(raw)
    check("load_config leaves the caller's dict alone", raw == {"edges": [{"edge": "left", "leds": 5}]}, raw)

def test_rejects():
    for name, cmd in (("tiled linear-only format", fb("XR24", [W * 4], [0], mod=0x0100000000000001)),
                      ("SAND128 YU12", fb("YU12", [W, W // 2, W // 2], [0, 0, 0],
                                           mod=(T.MOD_VENDOR_BROADCOM << 56) | T.MOD_SAND128)),
                      ("unknown fourcc", fb("RG16", [W * 2], [0]))):
        try:
            T.check_format(cmd)
            check("rejects " + name, False, "no ValueError")
        except ValueError:
            check("rejects " + name, True)

def test_ycc():
    lim = (0.2126, 0.0722, True)
    check("709 limited black/white", T.ycc_to_rgb([16, 235], [128, 128], [128, 128], lim) == [(0, 0, 0), (255, 255, 255)],
          T.ycc_to_rgb([16, 235], [128, 128], [128, 128], lim))
    worst = 0
    for rgb in ((255, 0, 0), (0, 255, 0), (0, 0, 255), (200, 40, 40), (30, 60, 180), (128, 128, 128)):
        for csc in ((0.299, 0.114, False), (0.2126, 0.0722, True), (0.2627, 0.0593, True)):
            y, u, v = rgb_to_ycc(rgb, csc)
            back = T.ycc_to_rgb([y], [u], [v], csc)[0]
            worst = max(worst, max(abs(a - b) for a, b in zip(back, rgb)))
    check("ycc round trip 601/709/2020 within 2", worst <= 2, "worst %d" % worst)

def test_zone_colors():
    colors = {("left", 4): [(1, 1, 1), (2, 2, 2), (3, 3, 3), (4, 4, 4)]}
    z = {"edge": "left", "count": 4, "use": 3, "top_first": True}
    check("zone use cuts the bottom", T.zone_colors(colors, z) == [(1, 1, 1), (2, 2, 2), (3, 3, 3)])
    z["top_first"] = False
    check("zone bottom first reverses", T.zone_colors(colors, z) == [(3, 3, 3), (2, 2, 2), (1, 1, 1)])

def test_ddp():
    def parse(p):
        flags, seq, dtype, did, offset, length = struct.unpack(">BBBBIH", p[:10])
        return flags, seq, dtype, did, offset, length, p[10:]
    # a real two-sided layout: two zones back to back -> two packets, push only on the last
    pk, seq = T.ddp_packets([(27, bytes([2] * 63)), (0, bytes([1] * 81))], 0)
    f = [parse(p) for p in pk]
    check("ddp two zones -> two packets, sorted by offset", [x[4] for x in f] == [0, 81], [x[4] for x in f])
    check("ddp push only on the last packet", [x[0] for x in f] == [0x40, 0x41], [hex(x[0]) for x in f])
    check("ddp header: type RGB8, id 1, length", all(x[2] == 0x0B and x[3] == 1 and x[5] == len(x[6]) for x in f))
    check("ddp one sequence number for the whole frame", [x[1] for x in f] == [1, 1] and seq == 1, ([x[1] for x in f], seq))
    # a long strip: 1000 pixels -> 480 + 480 + 40
    data = bytes(range(256)) * 12  # 3072 bytes, cut to 3000 below
    pk, _ = T.ddp_packets([(5, data[:3000])], 0)
    f = [parse(p) for p in pk]
    check("ddp 1000 px -> 3 packets of <= 480 px", [x[5] for x in f] == [1440, 1440, 120], [x[5] for x in f])
    check("ddp byte offsets continue", [x[4] for x in f] == [15, 15 + 1440, 15 + 2880], [x[4] for x in f])
    check("ddp payload unchanged when joined", b"".join(x[6] for x in f) == data[:3000])
    check("ddp every packet fits 1500 bytes", all(len(p) + 28 <= 1500 for p in pk), [len(p) for p in pk])
    _, s = T.ddp_packets([(0, b"\0" * 3)] , 15)
    check("ddp sequence wraps 15 -> 1", s == 1, s)
    # WLED's optional out-of-sequence filter (wled00/e131.cpp, handleDDPPacket), ported
    # line by line: it remembers the sequence number of the last push and rejects a packet
    # that looks late. Frames of 1 to 40 packets, many frames in a row, must all get through.
    def wled_rejects(sn, last):
        if last and sn:
            if last > 5:
                return last - 5 < sn < last
            return sn > 10 + last or sn < last
        return False
    dropped, last, s = 0, 0, 0
    for frame in range(300):
        npk = 1 + frame % 40
        pk, s = T.ddp_packets([(0, bytes(3 * T.DDP_MAX_PIXELS * npk))], s)
        for p in pk:
            flags, sn = p[0], p[1]
            if wled_rejects(sn & 0xF, last):
                dropped += 1
            elif flags & 1:
                last = sn & 0xF
    check("ddp: WLED's out-of-sequence filter drops nothing of long frames", dropped == 0, dropped)

EXAMPLE = {"host": "192.0.2.1", "port": 4048, "fps": 20, "response": 35, "depth": 10, "bars": True,
          "edges": [{"edge": "left", "first": 0, "leds": 27, "span": 100, "reverse": False},
                    {"edge": "right", "first": 27, "leds": 21, "span": 78, "reverse": False},
                    {"edge": "top", "first": 0, "leds": 0, "span": 100, "reverse": False},
                    {"edge": "bottom", "first": 0, "leds": 0, "span": 100, "reverse": False}]}

def test_config():
    cfg = T.load_config(EXAMPLE)
    z = T.zones_from_config(cfg)
    want = [{"edge": "left", "offset": 0, "use": 27, "count": 27, "top_first": True},
            {"edge": "right", "offset": 27, "use": 21, "count": 27, "top_first": True}]
    check("config: 27 left + 21 right covering 78 % -> the old zones", z == want, z)
    check("config defaults: no LEDs -> no zones", T.zones_from_config(T.load_config({})) == [])
    z = T.zones_from_config(T.load_config({"edges": [{"edge": "left", "leds": 10, "reverse": True}]}))
    check("config: reverse -> bottom first, missing keys filled", z == [{"edge": "left", "offset": 0, "use": 10,
                                                                         "count": 10, "top_first": False}], z)
    for name, raw in (("unknown key", {"colour": 1}), ("fps 0", {"fps": 0}), ("fps text", {"fps": "20"}),
                      ("response 0", {"response": 0}), ("bool as number", {"port": True}),
                      ("edge diagonal", {"edges": [{"edge": "diagonal", "leds": 5}]}),
                      ("bars as number", {"bars": 1}),
                      ("edge twice", {"edges": [{"edge": "left", "leds": 5}, {"edge": "left", "leds": 5}]}),
                      ("span 0", {"edges": [{"edge": "left", "leds": 5, "span": 0}]}),
                      ("negative leds", {"edges": [{"edge": "left", "leds": -1}]}),
                      ("reverse as text", {"edges": [{"edge": "left", "leds": 5, "reverse": "false"}]}),
                      ("reverse as number", {"edges": [{"edge": "left", "leds": 5, "reverse": 0}]}),
                      ("configuration as a list", []),
                      ("configuration as text", "x"),
                      ("edges as an object", {"edges": {"edge": "left"}}),
                      ("edges as a number", {"edges": 5}),
                      ("edge entry as text", {"edges": ["left"]}),
                      ("edge entry as null", {"edges": [None]}),
                      ("unknown edge key", {"edges": [{"edge": "left", "leds": 5, "revers": True}]}),
                      ("a million zones", {"edges": [{"edge": "left", "leds": 10000, "span": 1}]}),
                      ("host as list", {"host": ["a"]})):
        try:
            T.load_config(raw)
            check("config rejects " + name, False, "accepted")
        except ValueError:
            check("config rejects " + name, True)

class StopLoop(Exception):
    pass

def test_drm_init_retry():
    """A DRM failure while opening the card or reading the planes is retried inside the
    loop (the card closed again); it neither ends the worker nor counts as a bad
    configuration (exit 2, which would hold the worker until the settings change)."""
    import time as time_
    opened, made, sleeps = [], [], []
    def fake_open():
        if len(opened) == 0:
            opened.append(None)
            raise OSError("no DRM node with planes under /dev/dri")
        fd = os.open(os.devnull, os.O_RDONLY)
        opened.append(fd)
        return fd
    class FakePlanes:
        def __init__(self, card):
            made.append(card)
            if len(made) == 1:
                raise OSError("GETPLANERES failed")
        def current(self):
            raise ValueError("no plane on screen")
    def fake_sleep(t):
        sleeps.append(t)
        if len(sleeps) >= 8:
            raise StopLoop()
    closed_fds, real_close = [], os.close
    def spy_close(fd):
        closed_fds.append(fd)
        real_close(fd)
    real = T.open_card, T.Planes, time_.sleep
    T.open_card, T.Planes, time_.sleep = fake_open, FakePlanes, fake_sleep
    os.close = spy_close
    try:
        try:
            T.run(T.load_config(dict(EXAMPLE)), lambda: None, dry=True)
            check("drm init: the loop keeps running", False, "returned")
        except StopLoop:
            check("drm init: the loop keeps running", True)
        except OSError as e:
            check("drm init: an OSError does not leave run()", False, e)
    finally:
        T.open_card, T.Planes, time_.sleep = real
        os.close = real_close
    fds = [f for f in opened if f is not None]
    check("drm init: opened again after both failures", len(opened) == 3 and len(made) == 2, (len(opened), len(made)))
    check("drm init: the card is closed when Planes fails", made[0] in closed_fds, (made[0], closed_fds))
    for f in fds[1:]:
        real_close(f)
    # main(): an OSError out of run() is a retryable failure (exit 1), never the bad-config exit 2
    real_run = T.run
    def boom(*a, **k):
        raise OSError("drm gone")
    T.run = boom
    try:
        code = T.main(["--dry"])
    finally:
        T.run = real_run
    check("main: an OSError in run() exits 1, not 2", code == 1, code)

def test_stdin_gate():
    import threading
    r, w = os.pipe()
    rf, wf = os.fdopen(r, "r"), os.fdopen(w, "w")
    real_exit, exited = T.os._exit, threading.Event()
    T.os._exit = lambda code: exited.set()
    try:
        g = T.StdinGate(rf)
        check("stdin gate starts off", g.state() == (False, False, False))
        for line, want in (("on", (True, False, False)), ("paused", (True, True, False)), ("test", (True, False, True)), ("off", (False, False, False))):
            wf.write(line + "\n"); wf.flush()
            for _ in range(100):
                if g.state() == want:
                    break
                __import__("time").sleep(0.01)
            check("stdin gate %s" % line, g.state() == want, g.state())
        wf.write("on\n"); wf.flush(); __import__("time").sleep(0.05)
        real_time = T.time.time
        T.time.time = lambda: real_time() + 86400  # the wall clock jumps (NTP): no reason to go off
        try:
            check("stdin gate ignores a wall-clock jump", g.state() == (True, False, False), g.state())
        finally:
            T.time.time = real_time
        g.at -= T.STATE_TIMEOUT + 1
        check("stdin gate goes off without lines", g.state() == (False, False, False), g.state())
        wf.close()
        check("stdin gate exits on end of input", exited.wait(2))
    finally:
        T.os._exit = real_exit

NOTES = []  # notifications shown by the fake Kodi

def fake_kodi(settings, cond):
    """Minimal xbmc / xbmcaddon for the Kodi side (resources/lib/kodi_service.py):
    settings dict, cond(name) -> bool."""
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGINFO, xbmc.LOGWARNING, xbmc.log, xbmc.getCondVisibility = 1, 2, (lambda m, l=1: None), cond
    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.NOTIFICATION_WARNING, xbmcgui.shown = "warning", NOTES
    class Dialog:
        def notification(self, heading, message, icon=None, time=0): NOTES.append(message)
    xbmcgui.Dialog = Dialog
    class Monitor:
        def abortRequested(self): return True
        def waitForAbort(self, t): return True
    xbmc.Monitor = Monitor
    xbmcaddon = types.ModuleType("xbmcaddon")
    read = []
    class S:
        def _get(self, k): read.append(k); return settings[k]
        getInt = getBool = getString = _get
    class Addon:
        def getAddonInfo(self, k): return {"id": "service.tvlight", "path": ADDON_DIR}[k]
        def getSettings(self): return S()
    xbmcaddon.Addon = Addon
    sys.modules["xbmc"], sys.modules["xbmcaddon"], sys.modules["xbmcgui"] = xbmc, xbmcaddon, xbmcgui
    import importlib.util
    spec = importlib.util.spec_from_file_location("tvlight_service", os.path.join(LIB, "kodi_service.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, read

def test_service_lifecycle(svc):
    """Service.tick with a fake worker and clock: bad settings hold and notify, a crash
    restarts after a growing pause, a settings change starts over."""
    clock = [0.0]
    made, plan = [], []  # plan: exit code each new worker will have (None = keeps running)
    class FakeWorker:
        def __init__(self, cfg):
            self.code, self.sent, self.stopped, self.stuck = plan.pop(0) if plan else None, [], False, False
            made.append(self)
        def exit_code(self): return self.code
        def reason(self): return "no LEDs configured on any edge"
        def send(self, line): self.sent.append(line)
        def stop(self): self.stopped = True
    s = svc.Service(make_worker=FakeWorker, clock=lambda: clock[0])
    del NOTES[:]
    plan[:] = [2]
    s.tick(); s.tick(); clock[0] = 100.0; s.tick()
    check("service: bad settings -> one worker, held, reason on screen",
          len(made) == 1 and s.held and NOTES == ["no LEDs configured on any edge"], (len(made), s.held, NOTES))
    s.onSettingsChanged(); plan[:] = [None]
    s.tick()
    check("service: settings change starts a new worker", len(made) == 2 and not s.held and made[1].sent, len(made))
    check("service: test pattern right after saving", made[1].sent[-1] == "test", made[1].sent[-1:])
    clock[0] += svc.TEST_AFTER + 0.1; s.tick()
    check("service: back to the picture after %d s" % svc.TEST_AFTER, made[1].sent[-1] != "test", made[1].sent[-1:])
    made[1].code = 1  # crash
    s.tick()  # notices the exit
    s.tick()
    check("service: no restart right after a crash", len(made) == 2, len(made))
    clock[0] += svc.BACKOFF_FIRST + 0.1; plan[:] = [1]
    s.tick()
    check("service: restart after %d s" % svc.BACKOFF_FIRST, len(made) == 3, len(made))
    s.tick()  # crashes again at once
    clock[0] += svc.BACKOFF_FIRST + 0.1; s.tick()
    check("service: the pause grows after repeated crashes", len(made) == 3, len(made))
    clock[0] += svc.BACKOFF_FIRST * 2; s.tick()
    check("service: ... and it still restarts", len(made) == 4, len(made))
    check("service: crashes do not notify", NOTES == ["no LEDs configured on any edge"], NOTES)
    s.onSettingsChanged(); s.tick()
    check("service: settings change stops the old worker", made[3].stopped and len(made) == 5, (made[3].stopped, len(made)))

def test_service_robustness(svc):
    """Starting and feeding the worker fails in ways that must not take the service down:
    no interpreter, an unstartable thread, a child that exits at once, a child that stops
    reading, a tick that raises at shutdown. Real child processes where it is portable."""
    import subprocess, tempfile, threading, time as time_
    d = tempfile.mkdtemp()
    def script(name, body):
        path = os.path.join(d, name)
        open(path, "w", encoding="ascii").write(body)
        return path
    real = svc.WORKER, svc.subprocess.Popen, svc.threading.Thread, svc.log
    real_which = svc.shutil.which
    svc.shutil.which = lambda name: sys.executable  # no python3 on a Windows PC
    spawned = []
    class Spy(real[1]):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            spawned.append(self)
    clock = [0.0]
    def service():
        return svc.Service(make_worker=svc.Worker, clock=lambda: clock[0])
    try:
        svc.log = lambda *a, **k: None
        # 1. no interpreter: backoff, no exception
        def missing(*a, **k):
            raise FileNotFoundError("python3")
        svc.subprocess.Popen = missing
        s = service()
        try:
            s.tick()
            check("service: a missing interpreter does not raise", s.worker is None)
        except Exception as e:
            check("service: a missing interpreter does not raise", False, repr(e))
        check("service: ... and backs off like a crash", s.start_at == clock[0] + svc.BACKOFF_FIRST and
              s.backoff == svc.BACKOFF_FIRST * 2, (s.start_at, s.backoff))
        svc.subprocess.Popen = Spy
        # 2. a half-built worker (the thread cannot start): the child is reaped
        svc.WORKER = script("reads.py", "import sys\nsys.stdin.read()\n")
        def no_thread(*a, **k):
            raise RuntimeError("can't start new thread")
        svc.threading.Thread = no_thread
        s = service()
        try:
            s.tick()
            check("service: a worker that cannot finish starting does not raise", s.worker is None and len(spawned) == 1)
        except Exception as e:
            check("service: a worker that cannot finish starting does not raise", False, repr(e))
        check("service: ... and its child is reaped", spawned and spawned[0].poll() is not None,
              spawned and spawned[0].poll())
        svc.threading.Thread = real[2]
        # 3. a child that exits at once: ordinary crash path
        svc.WORKER = script("exits.py", "import sys\nsys.exit(3)\n")
        del spawned[:]
        s = service()
        try:
            for _ in range(100):
                s.tick()
                if s.worker is None:
                    break
                time_.sleep(0.05)
            check("service: a child that exits at once goes through the backoff", s.worker is None and
                  s.start_at > clock[0] and spawned and spawned[0].returncode == 3, (s.worker, s.start_at, [p.returncode for p in spawned]))
        except Exception as e:
            check("service: a child that exits at once goes through the backoff", False, repr(e))
        # 4. a child that stops reading: the pipe fills, the write does not block, the service restarts it
        svc.WORKER = script("sleeps.py", "import time\ntime.sleep(60)\n")
        del spawned[:]
        s = service()
        t0 = time_.monotonic()
        s.tick()
        w = s.worker
        for _ in range(200000):
            if w is None or w.stuck:
                break
            w.send("paused")
        check("worker: writes to a child that does not read end as stuck, without blocking",
              w is not None and w.stuck and time_.monotonic() - t0 < 20, time_.monotonic() - t0)
        s.tick()
        check("service: a stuck worker is stopped and restarted after the pause",
              s.worker is None and spawned[0].poll() is not None and s.start_at > clock[0],
              (s.worker, spawned[0].poll()))
        check("service: stopping a stuck worker is bounded", time_.monotonic() - t0 < 30, time_.monotonic() - t0)
    finally:
        svc.WORKER, svc.subprocess.Popen, svc.threading.Thread, svc.log = real
        svc.shutil.which = real_which
        for p in spawned:
            if p.poll() is None:
                p.kill()
    # 5. shutdown runs even if a tick raises
    stopped = []
    class W:
        stuck = False
        def exit_code(self): return None
        def send(self, line): pass
        def stop(self): stopped.append(True)
    s = svc.Service(make_worker=lambda cfg: W(), clock=lambda: 0.0)
    s.abortRequested = lambda: False
    s.waitForAbort = lambda t: False
    calls = []
    def tick():
        calls.append(1)
        if len(calls) == 1:
            s.worker = W()
        else:
            raise RuntimeError("boom")
    s.tick = tick
    try:
        s.loop()
    except RuntimeError:
        pass
    check("service: the worker is stopped when the loop dies", stopped == [True], stopped)

def settings_xml():
    import xml.etree.ElementTree as ET
    root = ET.parse(os.path.join(ADDON_DIR, "resources", "settings.xml")).getroot()
    out = {}
    for s in root.iter("setting"):
        d = s.find("default")
        out[s.get("id")] = (s.get("type"), d.text if d is not None else None, s.get("label"), s.get("help"))
    labels = {e.get("label") for e in root.iter() if e.get("label")} | \
             {e.get("help") for e in root.iter("setting") if e.get("help")} | \
             {h.text for h in root.iter("heading")}
    return out, labels

def test_service():
    xml, labels = settings_xml()
    typed = {"boolean": lambda d: d == "true", "integer": int, "string": lambda d: d or ""}
    defaults = {k: typed[t](d) for k, (t, d, _, _) in xml.items()}
    example = dict(defaults, host=" 192.0.2.1 ", left_leds=27, right_first=27, right_leds=21, right_span=78)
    state = {"vis": False, "full": False, "paused": False, "manual": False, "saver": False,
             "dialog": False, "dialog_id": "service.tvlight"}
    def cond(name):
        if name.startswith("String.IsEmpty(Window(Home).Property(%s))" % svc.MANUAL_OFF):
            return not state["manual"]
        m = re.match(r"String\.IsEqual\(Window\(addonsettings\)\.Property\(Addon\.ID\),(.*)\)$", name)
        if m:
            return state["dialog_id"] == m.group(1)
        return {"Window.IsActive(visualisation)": state["vis"], "Window.IsActive(fullscreenvideo)": state["full"],
                "Player.Paused": state["paused"], "System.ScreenSaverActive": state["saver"],
                "Window.IsActive(addonsettings)": state["dialog"]}[name]
    svc, read = fake_kodi(example, cond)
    cfg = svc.config()
    check("service config == the example setup", cfg == EXAMPLE, cfg)
    check("service config passes load_config", T.zones_from_config(T.load_config(cfg)) ==
          T.zones_from_config(T.load_config(EXAMPLE)))
    svc.enabled()
    check("every setting in settings.xml is read by kodi_service.py", not (set(xml) - set(read)), set(xml) - set(read))
    check("kodi_service.py reads no setting missing from settings.xml", not (set(read) - set(xml)), set(read) - set(xml))
    nobars, _ = fake_kodi(dict(example, bars=False, top_leds=30, top_reverse=True), cond)
    c = nobars.config()
    check("service passes bars off and the top edge on", c["bars"] is False and
          c["edges"][2] == {"edge": "top", "first": 0, "leds": 30, "span": 100, "reverse": True}, c)
    fresh, _ = fake_kodi(defaults, cond)
    check("fresh install (defaults) is valid, no LEDs yet", T.zones_from_config(T.load_config(fresh.config())) == [])
    svc, _ = fake_kodi(example, cond)
    for name, st, want in (("menu", {}, "off"), ("visualisation", {"vis": True}, "on"),
                           ("video", {"full": True}, "on"), ("video paused", {"full": True, "paused": True}, "paused"),
                           ("paused music is not video", {"paused": True}, "off"),
                           ("off by hand", {"vis": True, "manual": True}, "off"),
                           ("screensaver over paused video", {"full": True, "paused": True, "saver": True}, "off"),
                           ("own settings open -> test pattern", {"dialog": True}, "test"),
                           ("own settings over a video -> test pattern", {"dialog": True, "full": True}, "test"),
                           ("another add-on's settings", {"dialog": True, "dialog_id": "plugin.video.youtube"}, "off"),
                           ("last settings were ours, dialog closed", {"dialog": False}, "off"),
                           ("off by hand beats the test pattern", {"dialog": True, "manual": True}, "off")):
        state.update({"vis": False, "full": False, "paused": False, "manual": False, "saver": False,
                      "dialog": False, "dialog_id": "service.tvlight"}, **st)
        check("service state " + name, svc.state() == want, svc.state())
    state.update({"vis": True, "full": False, "paused": False, "manual": False, "saver": False,
                  "dialog": False, "dialog_id": "service.tvlight"})  # visualisation on screen
    test_service_lifecycle(svc)
    test_service_robustness(svc)
    # every label/help/heading id has a string
    po = open(os.path.join(ADDON_DIR, "resources", "language", "resource.language.en_gb", "strings.po"),
              encoding="utf-8").read()
    ids = set(__import__("re").findall(r'msgctxt "#(\d+)"', po))
    missing = sorted(l for l in labels if l not in ids)
    check("every settings label has an en_gb string", not missing, missing)
    # translations: UTF-8 (the one place with non-ASCII text), every string translated
    re_ = __import__("re")
    lang_dir = os.path.join(ADDON_DIR, "resources", "language")
    for lang in sorted(os.listdir(lang_dir)):
        if lang == "resource.language.en_gb":
            continue
        raw = open(os.path.join(lang_dir, lang, "strings.po"), "rb").read()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            check(lang + " is UTF-8", False, e)
            continue
        entries = dict(re_.findall(r'msgctxt "#(\d+)"\nmsgid ".*"\nmsgstr "(.*)"', text.replace("\r\n", "\n")))
        untranslated = sorted(i for i in ids if not entries.get(i))
        check(lang + ": every string translated", not untranslated, untranslated)

def test_worker_process():
    """The worker as the add-on starts it (Linux only: it needs fcntl)."""
    import subprocess
    try:
        import fcntl  # noqa: F401
    except ImportError:
        print("skip worker process (no fcntl here)")
        return
    if isinstance(sys.modules.get("fcntl"), types.ModuleType) and not hasattr(sys.modules["fcntl"], "ioctl"):
        print("skip worker process (fcntl stubbed)")
        return
    w = os.path.join(LIB, "tvlight.py")
    def run(lines, timeout=5):
        return subprocess.run([sys.executable, w, "--worker"], input="".join(l + "\n" for l in lines),
                              capture_output=True, text=True, timeout=timeout)
    r = run(['{"fps": 0}'])
    check("worker: bad config -> exit 2 + reason", r.returncode == 2 and "bad configuration" in r.stdout, r.stdout)
    r = run(["{}"])
    check("worker: no LEDs -> exit 2 + reason", r.returncode == 2 and "no LEDs" in r.stdout, r.stdout)
    cfg = dict(EXAMPLE, host="127.0.0.1", port=9)
    r = run([json.dumps(cfg), "off", "off"])
    check("worker: end of input -> exit 0", r.returncode == 0, (r.returncode, r.stdout[-200:]))

def test_addon_metadata():
    """What kodi-addon-checker (the check the official repository runs) looks at, as far as
    read from its code: folder = id, artwork declared in <assets> with its sizes, formats and
    limits, file types it accepts, plus the texts a listing needs."""
    import xml.etree.ElementTree as ET
    from PIL import Image
    root = ET.parse(os.path.join(ADDON_DIR, "addon.xml")).getroot()
    check("addon: folder name is the add-on id", root.get("id") == os.path.basename(ADDON_DIR), root.get("id"))
    check("addon: version x.y.z", re.match(r"^\d+\.\d+\.\d+$", root.get("version") or "") is not None, root.get("version"))
    meta = [e for e in root.iter("extension") if e.get("point") == "xbmc.addon.metadata"][0]
    for tag in ("summary", "description", "disclaimer"):
        langs = sorted(e.get("lang") for e in meta.findall(tag))
        check("addon: %s in en_GB and de_DE" % tag, langs == ["de_DE", "en_GB"], langs)
    check("addon: license and platform", meta.findtext("license") == "GPL-2.0-or-later" and meta.findtext("platform"),
          (meta.findtext("license"), meta.findtext("platform")))
    news = meta.findtext("news") or ""
    check("addon: news names this version", ("v" + root.get("version")) in news, news[:40])
    assets = meta.find("assets")
    icon = Image.open(os.path.join(ADDON_DIR, assets.findtext("icon")))
    check("artwork: icon PNG 256 or 512, solid", icon.format == "PNG" and icon.size in ((256, 256), (512, 512))
          and icon.mode in ("RGB", "L"), (icon.format, icon.size, icon.mode))
    fpath = os.path.join(ADDON_DIR, assets.findtext("fanart"))
    fan = Image.open(fpath)
    check("artwork: fanart 1280x720/1920x1080/3840x2160, <= 1000 KB",
          fan.size in ((1280, 720), (1920, 1080), (3840, 2160)) and os.path.getsize(fpath) <= 1000 * 1024,
          (fan.size, os.path.getsize(fpath)))
    entry = [l for l in open(os.path.join(ADDON_DIR, "service.py"), encoding="ascii") if l.strip() and not l.startswith("#")]
    check("addon: entry point short (checker warns above 15 lines)", len(entry) <= 15, len(entry))
    ext = [e for e in root.iter("extension") if e.get("point") == "xbmc.service"][0]
    check("addon: service extension has only the attributes the schema allows",
          set(ext.attrib) <= {"point", "id", "name", "library"}, sorted(ext.attrib))
    ok_ext = set("py xml gif png jpg jpeg md txt po json markdown yml rst ini flv wav mp4 html css lst pkla g "
                 "template in cfg xsd directory help list mpeg pls info ttf xsp theme yaml dict crt ico".split())
    odd = [os.path.relpath(os.path.join(r, f), ADDON_DIR) for r, ds, fs in os.walk(ADDON_DIR) for f in fs
           if "__pycache__" not in r and f.rsplit(".", 1)[-1].lower() not in ok_ext]
    check("addon: only file types the checker accepts", not odd, odd)

def run_test(fn):
    """An exception inside a test is a failed check (and the other tests still run), not a
    traceback that hides which check noticed what."""
    try:
        fn()
    except Exception as e:
        check(fn.__name__, False, "raised %s: %s" % (type(e).__name__, e))

def run_all(extra=()):
    run_test(test_formats)
    run_test(test_p030_flat)
    run_test(test_p030_padding_bits)
    run_test(test_top_bottom)
    run_test(test_letterbox)
    run_test(test_depth)
    run_test(test_odd_width)
    run_test(test_config_copy)
    run_test(test_pick)
    run_test(test_format_handles_closed)
    run_test(test_test_pattern)
    run_test(test_grey_bars)
    run_test(test_bars_logic)
    run_test(test_rejects)
    run_test(test_ycc)
    run_test(test_zone_colors)
    run_test(test_ddp)
    run_test(test_config)
    run_test(test_drm_init_retry)
    run_test(test_stdin_gate)
    run_test(test_service)
    run_test(test_worker_process)
    run_test(test_addon_metadata)
    for fn in extra:
        run_test(fn)
    print("\nALL OK" if not FAILS else "\n%d FAILED: %s" % (len(FAILS), ", ".join(FAILS)))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    run_all()
