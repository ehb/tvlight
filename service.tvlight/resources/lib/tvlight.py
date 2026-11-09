#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# tvlight: the screen edges of a Kodi box -> WLED via DDP.
#
# Reads Kodi's DRM planes directly (Raspberry Pi 4, vc4), averages the screen edges
# into zones and streams them to WLED. Only active while Kodi shows the music
# visualisation or fullscreen video; otherwise it sends nothing and WLED falls back
# to its normal light after its realtime timeout (2.5 s).
#
# Normally started by the Kodi add-on (service.tvlight, resources/lib/kodi_service.py) as
#     python3 tvlight.py --worker      config as the first stdin line, then the state
# By hand, for tests on the box:
#     python3 tvlight.py --dry   prints zones for a default layout, sends nothing
import ctypes, fcntl, json, mmap, os, socket, struct, sys, threading, time
from PIL import Image

# --- configuration -----------------------------------------------------------

# Edges: "first" = first LED of this edge in WLED's main segment, "leds" = LEDs on it
# (0 = edge unused), "span" = how much of the edge the strip covers, in percent from
# the start -- the top for left/right, the left for top/bottom (a strip shorter than
# the picture keeps the scale of a full one and drops the rest), "reverse" = LED 0 sits
# at the bottom (left/right) or at the right (top/bottom).
DEFAULTS = {
    "host": "", "port": 4048,  # WLED
    "fps": 20,                 # frames per second while active
    "response": 35,            # percent of the way to the new colour per frame; 100 = no smoothing
    "depth": 10,               # sampled edge depth, percent of the width (left/right) or height
    "bars": True,              # cut letterbox bars burnt into the video
    "edges": [{"edge": "left", "first": 0, "leds": 0, "span": 100, "reverse": False},
              {"edge": "right", "first": 0, "leds": 0, "span": 100, "reverse": False},
              {"edge": "top", "first": 0, "leds": 0, "span": 100, "reverse": False},
              {"edge": "bottom", "first": 0, "leds": 0, "span": 100, "reverse": False}],
}
EDGES = ("left", "right", "top", "bottom")

MAX_ZONES = 10000  # per edge: more zones than that is a typo (1 % span of 10000 LEDs), and the picture has fewer pixels

def load_config(raw=None):
    """Defaults + raw (dict), checked. ValueError names the first bad value."""
    cfg = json.loads(json.dumps(DEFAULTS))
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("the configuration must be an object, not %s" % type(raw).__name__)
    for k, v in json.loads(json.dumps(raw)).items():  # a copy: the caller's dict stays as it was
        if k not in cfg:
            raise ValueError("unknown setting %r" % k)
        cfg[k] = v
    def number(name, lo, hi):
        v = cfg[name]
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            raise ValueError("%s must be a whole number %d..%d, not %r" % (name, lo, hi, v))
    number("port", 1, 65535); number("fps", 1, 60); number("response", 1, 100); number("depth", 1, 50)
    if not isinstance(cfg["bars"], bool):
        raise ValueError("bars must be true or false")
    if not isinstance(cfg["host"], str):
        raise ValueError("host must be text")
    if not isinstance(cfg["edges"], list):
        raise ValueError("edges must be a list")
    seen = set()
    for e in cfg["edges"]:
        if not isinstance(e, dict):
            raise ValueError("every entry of edges must be an object, not %r" % (e,))
        for k in e:
            if k not in DEFAULTS["edges"][0]:
                raise ValueError("unknown edge setting %r" % k)
        e.update({k: v for k, v in DEFAULTS["edges"][0].items() if k not in e})
        if e["edge"] not in EDGES or e["edge"] in seen:
            raise ValueError("edge must be one of %s, each once: %r" % (", ".join(EDGES), e["edge"]))
        seen.add(e["edge"])
        for name, lo, hi in (("first", 0, 100000), ("leds", 0, 10000), ("span", 1, 100)):
            v = e[name]
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                raise ValueError("%s %s must be a whole number %d..%d, not %r" % (e["edge"], name, lo, hi, v))
        if not isinstance(e["reverse"], bool):
            raise ValueError("%s reverse must be true or false, not %r" % (e["edge"], e["reverse"]))
        zones = round(e["leds"] * 100 / e["span"])
        if zones > MAX_ZONES:
            raise ValueError("%s: %d LEDs at %d %% span need %d zones, at most %d are supported"
                             % (e["edge"], e["leds"], e["span"], zones, MAX_ZONES))
    return cfg

def zones_from_config(cfg):
    """Zones in the form the main loop uses: the picture edge is cut into count zones,
    the first use of them (from the top) go to the LEDs from offset on."""
    return [{"edge": e["edge"], "offset": e["first"], "use": e["leds"],
             "count": max(e["leds"], round(e["leds"] * 100 / e["span"])), "top_first": not e["reverse"]}
            for e in cfg["edges"] if e["leds"] > 0]

ROWS = 270        # rows looked at per plane (every n-th row); 10 per zone at 27 zones
EDGE = 0.10       # default sampled edge depth (frame_colors), fraction of the width
PAUSED_FPS = 2    # paused video: the picture stays, the OSD may come and go; WLED needs data within 2.5 s
GATE_INTERVAL = 1.0
RESCAN_INTERVAL = 1.0
KEEPALIVE = 2.0   # s: repeat the last colours while frames can't be read, then stop
STATE_TIMEOUT = 5.0  # s without a state line from the add-on -> off

# --- DRM ioctls --------------------------------------------------------------

def _iowr(nr, size):
    return (3 << 30) | (size << 16) | (0x64 << 8) | nr

class FbCmd2(ctypes.Structure):
    _fields_ = [("fb_id", ctypes.c_uint32), ("width", ctypes.c_uint32),
                ("height", ctypes.c_uint32), ("pixel_format", ctypes.c_uint32),
                ("flags", ctypes.c_uint32), ("handles", ctypes.c_uint32 * 4),
                ("pitches", ctypes.c_uint32 * 4), ("offsets", ctypes.c_uint32 * 4),
                ("pad", ctypes.c_uint32), ("modifier", ctypes.c_uint64 * 4)]

class Prime(ctypes.Structure):
    _fields_ = [("handle", ctypes.c_uint32), ("flags", ctypes.c_uint32),
                ("fd", ctypes.c_int32)]

class PlaneRes(ctypes.Structure):
    _fields_ = [("plane_id_ptr", ctypes.c_uint64), ("count_planes", ctypes.c_uint32),
                ("pad", ctypes.c_uint32)]

class GetPlane(ctypes.Structure):
    _fields_ = [("plane_id", ctypes.c_uint32), ("crtc_id", ctypes.c_uint32),
                ("fb_id", ctypes.c_uint32), ("possible_crtcs", ctypes.c_uint32),
                ("gamma_size", ctypes.c_uint32), ("count_format_types", ctypes.c_uint32),
                ("format_type_ptr", ctypes.c_uint64)]

class ObjProps(ctypes.Structure):
    _fields_ = [("props_ptr", ctypes.c_uint64), ("prop_values_ptr", ctypes.c_uint64),
                ("count_props", ctypes.c_uint32), ("obj_id", ctypes.c_uint32),
                ("obj_type", ctypes.c_uint32)]

class GetProp(ctypes.Structure):
    _fields_ = [("values_ptr", ctypes.c_uint64), ("enum_blob_ptr", ctypes.c_uint64),
                ("prop_id", ctypes.c_uint32), ("flags", ctypes.c_uint32),
                ("name", ctypes.c_char * 32), ("count_values", ctypes.c_uint32),
                ("count_enum_blobs", ctypes.c_uint32)]

class PropEnum(ctypes.Structure):
    _fields_ = [("value", ctypes.c_uint64), ("name", ctypes.c_char * 32)]

GETFB2 = _iowr(0xCE, ctypes.sizeof(FbCmd2))
OBJ_GETPROPERTIES = _iowr(0xB9, ctypes.sizeof(ObjProps))
GETPROPERTY = _iowr(0xAA, ctypes.sizeof(GetProp))
OBJ_PLANE = 0xEEEEEEEE
PRIME_H2FD = _iowr(0x2D, ctypes.sizeof(Prime))
GETPLANERES = _iowr(0xB5, ctypes.sizeof(PlaneRes))
GETPLANE = _iowr(0xB6, ctypes.sizeof(GetPlane))
GEM_CLOSE = (1 << 30) | (8 << 16) | (0x64 << 8) | 0x09
SET_CLIENT_CAP = (1 << 30) | (16 << 16) | (0x64 << 8) | 0x0D
DROP_MASTER = (0x64 << 8) | 0x1F
O_CLOEXEC = 0o2000000
MOD_VENDOR_BROADCOM = 0x07   # DRM format modifier: vendor in the top byte,
MOD_SAND128 = 4              # type in the low byte, column height in between
P030_WHITE_SLOPE = (235 - 128) / (58 - 32)   # luma code per probe step from grey to white
P030_MASK = [v & 0x3F for v in range(256)]   # top byte of a P030 word: bits 30-31 are padding

# YCbCr -> RGB: (Kr, Kb) per matrix. The video plane carries the matrix and range Kodi
# chose in its COLOR_ENCODING / COLOR_RANGE properties -- the same ones the display
# converts with. Video is limited range (16-235); treating it as full range turns
# black into 16 and loses ~14 % saturation.
MATRICES = {"ITU-R BT.601 YCbCr": (0.299, 0.114), "ITU-R BT.709 YCbCr": (0.2126, 0.0722),
            "ITU-R BT.2020 YCbCr": (0.2627, 0.0593)}
DEFAULT_CSC = (0.2126, 0.0722, True)  # BT.709 limited: if the properties can't be read


def open_card():
    """The DRM node that drives the display, opened without keeping DRM master. The
    first opener of a node becomes master; if that is us, Kodi gets "failed to authorize
    drm magic" and shows only the boot splash -- so this runs only once Kodi is on
    screen, and drops master at once. Reading planes/fbs needs no master as root.

    Which cardN is the display differs between kernels (on a Pi the 3D unit v3d has a
    node too, sometimes card0, sometimes card1): take the first node with planes."""
    import glob
    for path in sorted(glob.glob("/dev/dri/card[0-9]*")):
        card = os.open(path, os.O_RDWR | O_CLOEXEC)
        try:
            fcntl.ioctl(card, DROP_MASTER)
        except OSError:
            pass  # not master (Kodi has it) -- the normal case
        try:
            fcntl.ioctl(card, SET_CLIENT_CAP, struct.pack("QQ", 2, 1))  # universal planes
            res = PlaneRes()
            fcntl.ioctl(card, GETPLANERES, res)
            if res.count_planes:
                return card
        except OSError:
            pass  # a render-only node (v3d): no KMS
        os.close(card)
    raise OSError("no DRM node with planes under /dev/dri")


class Planes:
    """Active planes and their fb formats. A full scan of all planes runs once
    a second; per frame only the planes found active get queried (the fb ids
    flip every frame). debugfs state would cost ~85 ms and the modeset locks."""

    def __init__(self, card):
        self.card = card
        fcntl.ioctl(card, SET_CLIENT_CAP, struct.pack("QQ", 2, 1))  # universal planes
        res = PlaneRes()
        fcntl.ioctl(card, GETPLANERES, res)
        ids = (ctypes.c_uint32 * res.count_planes)()
        res.plane_id_ptr = ctypes.addressof(ids)
        fcntl.ioctl(card, GETPLANERES, res)
        self.all_ids = list(ids)
        self.active_ids, self.formats, self.last_scan = [], {}, 0.0
        self.csc, self.plane_of = {}, {}

    def _csc(self, pid):
        """(Kr, Kb, limited) from the plane's COLOR_ENCODING / COLOR_RANGE properties."""
        props = {}
        op = ObjProps(obj_id=pid, obj_type=OBJ_PLANE)
        fcntl.ioctl(self.card, OBJ_GETPROPERTIES, op)
        ids = (ctypes.c_uint32 * op.count_props)()
        vals = (ctypes.c_uint64 * op.count_props)()
        op.props_ptr, op.prop_values_ptr = ctypes.addressof(ids), ctypes.addressof(vals)
        fcntl.ioctl(self.card, OBJ_GETPROPERTIES, op)
        for pr, val in zip(ids, vals):
            gp = GetProp(prop_id=pr)
            fcntl.ioctl(self.card, GETPROPERTY, gp)
            name = gp.name.decode()
            if name not in ("COLOR_ENCODING", "COLOR_RANGE") or not gp.count_enum_blobs:
                continue
            enums = (PropEnum * gp.count_enum_blobs)()
            values = (ctypes.c_uint64 * gp.count_values)()
            gp.enum_blob_ptr, gp.values_ptr = ctypes.addressof(enums), ctypes.addressof(values)
            fcntl.ioctl(self.card, GETPROPERTY, gp)
            props[name] = {e.value: e.name.decode() for e in enums}.get(val)
        if props.get("COLOR_ENCODING") not in MATRICES:
            return DEFAULT_CSC
        return MATRICES[props["COLOR_ENCODING"]] + (props.get("COLOR_RANGE") != "YCbCr full range",)

    def colorspace(self, fourcc):
        """(Kr, Kb, limited) of the plane showing fourcc; read once per scan."""
        pid = self.plane_of.get(fourcc)
        if pid not in self.csc:
            try:
                self.csc[pid] = self._csc(pid) if pid is not None else DEFAULT_CSC
            except OSError:
                self.csc[pid] = DEFAULT_CSC
        return self.csc[pid]

    def _fb(self, pid):
        gp = GetPlane(plane_id=pid)
        fcntl.ioctl(self.card, GETPLANE, gp)
        return gp.fb_id if gp.crtc_id and gp.fb_id else 0

    def _format(self, fb_id):
        if fb_id not in self.formats:
            cmd = getfb2(self.card, fb_id)
            try:
                self.formats[fb_id] = fourcc_of(cmd)
            finally:
                close_handles(self.card, cmd)  # also for a FourCC that does not decode
        return self.formats[fb_id]

    def current(self):
        """{fourcc: fb_id} of the planes on screen."""
        if time.time() - self.last_scan > RESCAN_INTERVAL:
            self.active_ids = [p for p in self.all_ids if self._fb(p)]
            self.formats, self.csc, self.last_scan = {}, {}, time.time()
        out = {}
        for pid in self.active_ids:
            fb = self._fb(pid)
            if fb:
                fourcc = self._format(fb)
                out[fourcc], self.plane_of[fourcc] = fb, pid
        return out


def fourcc_of(cmd):
    """The pixel format as text; a code that is not ASCII becomes a ValueError."""
    try:
        return struct.pack("<I", cmd.pixel_format).decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("pixel format %#x not handled" % cmd.pixel_format) from None

def getfb2(card, fb_id):
    cmd = FbCmd2(fb_id=fb_id)
    fcntl.ioctl(card, GETFB2, cmd)
    return cmd

def close_handles(card, cmd):
    for hd in set(cmd.handles[i] for i in range(4) if cmd.handles[i]):
        fcntl.ioctl(card, GEM_CLOSE, struct.pack("II", hd, 0))

def edge_colors(card, fb_id, wanted, csc=DEFAULT_CSC, depth=EDGE, bars=None):
    """{(edge, count): [(r, g, b)] * count, top first} of one framebuffer on screen.

    One GETFB2 for all edges: a 60 fps video can destroy the fb while the first edge
    is being read, and a second GETFB2 then fails with ENOENT (measured: 70 % of the
    frames). The GEM handles and the mapping keep the buffer valid after the fb id is
    gone."""
    cmd = getfb2(card, fb_id)
    try:
        check_format(cmd)  # before mapping anything
        pr = Prime(handle=cmd.handles[0], flags=O_CLOEXEC)
        fcntl.ioctl(card, PRIME_H2FD, pr)
        try:
            size = os.lseek(pr.fd, 0, os.SEEK_END)
            mm = mmap.mmap(pr.fd, size, mmap.MAP_SHARED, mmap.PROT_READ)
        finally:
            os.close(pr.fd)
        try:
            return frame_colors(mm, cmd, wanted, csc, depth, bars)
        finally:
            # On an exception the traceback can still hold an image sharing the mapping;
            # then the mapping goes with the garbage collector instead.
            try:
                mm.close()
            except BufferError:
                pass
    finally:
        close_handles(card, cmd)

def check_format(cmd):
    """(fourcc, sand) of a framebuffer this module can read, else ValueError."""
    fourcc = fourcc_of(cmd)
    mod = cmd.modifier[0]
    sand = mod >> 56 == MOD_VENDOR_BROADCOM and mod & 0xFF == MOD_SAND128
    if sand:
        if fourcc not in ("NV12", "P030"):
            raise ValueError("SAND128 %s not handled" % fourcc)
    elif mod not in (0, 0xFFFFFFFFFFFFFF):  # 0 = linear, the other = DRM_FORMAT_MOD_INVALID
        raise ValueError("tiled buffer %s mod %#x" % (fourcc, mod))
    elif fourcc not in ("AR24", "XR24", "YU12", "NV12"):
        raise ValueError("format %s not handled" % fourcc)
    return fourcc, sand

class Frame:
    """Rectangles of one framebuffer as PIL images, in whatever layout the buffer has.

    Coordinates are luma pixels of the picture; every method reads only every n-th row
    (ROWS per plane) and only the columns asked for. buf is the whole buffer: the
    mapping on the Pi, plain bytes in the selftest.

    The two kinds of buffers want opposite reads (A/B on a Pi 4): the GUI
    planes (RGB) are uncached, so their rows come in as slices of the mapping (memcpy,
    wide loads; 2.9 instead of 11.5 ms per 1080p frame). The video decoder's buffers are
    cached; there PIL reads in place (frombuffer shares the mapping, stride = step *
    pitch) and a slice loop is the slower one (service ~18 % instead of ~10 % of a core).

    Broadcom SAND128 (what the Pi 4 HEVC decoder hands out: NV12 for 8 bit, P030 for 10
    bit) cuts the picture into columns 128 bytes wide; each column holds colh rows of 128
    bytes, first all luma rows, then the chroma rows from offsets[1] on. A rectangle is
    put together from the pieces of the columns it touches.

    P030 packs three 10-bit samples into each little-endian 32-bit word (bits 0-9,
    10-19, 20-29). The top byte of each word is the third sample's upper 6 bits (bits
    30-31 are padding and masked off), so the A channel of the words read as RGBA is a 6-bit
    probe of every third sample -- plenty for zone means, and all of it in C (exact
    unpacking with ImageMath cost 15.8 ms per frame). In chroma rows the
    samples run Cb, Cr, Cb, Cr, ...: the third sample is Cb in even words and Cr in odd
    ones, so the A bytes in pairs read as LA give Cb as L and Cr as A. One luma probe
    stands for 3 pixels, one chroma probe pair for 6."""

    def __init__(self, buf, cmd):
        self.fourcc, self.sand = check_format(cmd)
        self.buf, self.cmd, self.mv = buf, cmd, memoryview(buf)
        self.w, self.h = cmd.width, cmd.height
        self.rgb = self.fourcc in ("AR24", "XR24")
        self.ystep = max(1, self.h // ROWS)
        self.cstep = max(1, (self.h // 2) // ROWS)
        if self.sand:
            self.tenbit = self.fourcc == "P030"
            self.colw = 96 if self.tenbit else 128   # picture pixels per column
            self.cstride = 128 * ((cmd.modifier[0] >> 8) & 0xFFFFFFFFFFFF)

    def code(self, values, chroma):
        """Means of _sand images -> 8-bit code values. A P030 probe v is the sample's upper
        6 bits (sample = 16 v + 0..15), so v * 4 is the 8-bit code rounded down; that keeps
        the limited-range ends and the neutral chroma exact (black 64 -> 16, 512 -> 128,
        960 -> 240). Luma above the middle is stretched so that white (940 -> probe 58)
        reaches 235 instead of 232."""
        if not (self.sand and self.tenbit):
            return values
        if chroma:
            return [v * 4 for v in values]
        return [min(255.0, v * 4 if v <= 32 else 128 + (v - 32) * P030_WHITE_SLOPE) for v in values]

    def luma(self, x0, x1, y0, y1):
        """Y of a rectangle as a list of items (raw probes for P030, see code())."""
        if self.sand:
            return self._sand(x0, x1, y0, y1, False)
        return [self._plane(0, "L", x0, x1, y0, y1, self.ystep)]

    def release(self):
        # On an exception the traceback can still hold an image sharing the mapping;
        # then the mapping goes with the garbage collector instead.
        try:
            self.mv.release()
        except BufferError:
            pass

    # -- linear --------------------------------------------------------------
    def _plane(self, i, mode, x0, x1, y0, y1, step):
        """Rectangle of linear plane i (plane coordinates) as an item: the image shares
        the mapping (rows y0.. at stride step * pitch, columns 0..x1) and the box picks
        x0..x1. Averaging with resize(box=...) reads just the box; a crop or a float
        copy first cost 1.5 ms per YU12 frame."""
        o, p = self.cmd.offsets[i], self.cmd.pitches[i]
        n = max(1, (y1 - y0) // step)
        a = o + y0 * p
        img = Image.frombuffer(mode, (x1, n), self.mv[a:a + p * step * n], "raw", mode, p * step, 1)
        return img, (x0, 0, x1, n)

    def rgb_rect(self, x0, x1, y0, y1):
        """RGBX image of an RGB plane; the bytes are B, G, R, X (callers swap)."""
        o, p, step = self.cmd.offsets[0], self.cmd.pitches[0], self.ystep
        a = o + x0 * 4
        rows = range(y0, max(y0 + 1, y1 - step + 1), step)
        data = b"".join(self.buf[a + r * p:a + r * p + (x1 - x0) * 4] for r in rows)
        # RGBX, not RGBA: PIL premultiplies RGBA before a resize (133 ms per frame)
        return Image.frombuffer("RGBX", (x1 - x0, len(rows)), data, "raw", "RGBX", 0, 1)

    # -- SAND128 -------------------------------------------------------------
    def _sand(self, x0, x1, y0, y1, chroma):
        """List of items (one per column touched) of a SAND rectangle: luma as "F",
        chroma as a pair of items (Cb, Cr). P030 gives 6-bit probes: zone means go through
        self.code() to become 8-bit code values (the mapping is linear, so once on the
        means instead of on every image)."""
        step = self.cstep if chroma else self.ystep
        if chroma:
            y0, y1 = y0 // 2, max(y0 // 2 + 1, y1 // 2)
        n = max(1, (y1 - y0) // step)
        out = []
        for c in range(x0 // self.colw, (x1 - 1) // self.colw + 1):
            cx0, cx1 = max(x0, c * self.colw) - c * self.colw, min(x1, (c + 1) * self.colw) - c * self.colw
            base = c * self.cstride + (self.cmd.offsets[1] if chroma else 0) + y0 * 128
            rows = self.mv[base:base + 128 * step * n]
            if self.tenbit:
                a = Image.frombuffer("RGBA", (32, n), rows, "raw", "RGBA", 128 * step, 1).getchannel("A").point(P030_MASK)
                if chroma:
                    cb, cr = Image.frombytes("LA", (16, n), a.tobytes()).split()
                    # pair m holds Cb of pixels 6m+2..3 and Cr of 6m+4..5: whole pairs only
                    box = (cx0 // 6, 0, max(cx0 // 6 + 1, (cx1 + 1) // 6), n)
                    out.append(tuple((ch.crop(box).convert("F"), None) for ch in (cb, cr)))
                else:
                    # probe k is the sample of pixel 3k+2: only probes inside the picture, a
                    # partial last column (width not a multiple of 96) ends in padding
                    box = (cx0 // 3, 0, max(cx0 // 3 + 1, cx1 // 3), n)
                    out.append((a.crop(box).convert("F"), None))
            elif chroma:
                uv = Image.frombuffer("LA", (64, n), rows, "raw", "LA", 128 * step, 1)
                box = (cx0 // 2, 0, max(cx0 // 2 + 1, (cx1 + 1) // 2), n)
                out.append(tuple((ch, box) for ch in uv.split()))
            else:
                img = Image.frombuffer("L", (128, n), rows, "raw", "L", 128 * step, 1)
                out.append((img, (cx0, 0, cx1, n)))
        return out

    def sand_across(self, x0, x1, y0, y1, count):
        """Zone means (Y, Cb, Cr in 8-bit code values) across a SAND strip, left to right.

        A strip across the picture touches every column; one small PIL pipeline per
        column cost 15-24 ms per frame for a top plus a bottom strip. Here the strip rows
        of all columns are joined into one image (each column's rows lie in one piece),
        one resize averages the rows of every column, and the zones run over the
        resulting profile: a few PIL calls whatever the width. The profile keeps the
        probe resolution, so a probe whose pixel lies in the padding of a partial last
        column (width not a multiple of the column width) is never used."""
        def profile(chroma):
            ry0, ry1 = (y0 // 2, max(y0 // 2 + 1, y1 // 2)) if chroma else (y0, y1)
            step = self.cstep if chroma else self.ystep
            nr = max(step, (ry1 - ry0) // step * step)  # whole steps: columns stay aligned
            n = nr // step                                # sampled rows per column
            ncols = -(-self.w // self.colw)
            off = (self.cmd.offsets[1] if chroma else 0) + ry0 * 128
            data = b"".join(self.buf[c * self.cstride + off:c * self.cstride + off + 128 * nr]
                            for c in range(ncols))
            if self.tenbit:
                a = Image.frombytes("RGBA", (32, n * ncols), data, "raw", "RGBA", 128 * step).getchannel("A").point(P030_MASK)
                if chroma:
                    planes = Image.frombytes("LA", (16, n * ncols), a.tobytes()).split()
                else:
                    planes = (a,)
            elif chroma:
                planes = Image.frombytes("LA", (64, n * ncols), data, "raw", "LA", 128 * step).split()
            else:
                planes = (Image.frombytes("L", (128, n * ncols), data, "raw", "L", 128 * step),)
            out = []
            w_last = self.w - (ncols - 1) * self.colw  # picture pixels in the last column
            for pl in planes:
                pw = pl.width
                span = self.colw // pw  # picture pixels per probe: 1, 2 (8 bit), 3, 6 (P030)
                # row c = column c; float only for the 6-bit P030 probes (precision)
                grid = (pl.convert("F") if self.tenbit else pl).resize((pw, ncols), Image.BOX).convert("F")
                if w_last < self.colw:
                    # probes of the last column whose pixel lies in the padding take the
                    # value of the last probe inside the picture
                    last_k = max(0, (w_last - span) // span)
                    row = list(grid.crop((0, ncols - 1, pw, ncols)).getdata())
                    row[last_k + 1:] = [row[last_k]] * (pw - last_k - 1)
                    fix = Image.new("F", (pw, 1))
                    fix.putdata(row)
                    grid.paste(fix, (0, ncols - 1))
                # the columns side by side, one probe stretched over its pixels, zones over x
                line = Image.frombytes("F", (pw * ncols, 1), grid.tobytes())
                line = line.resize((pw * ncols * span, 1), Image.NEAREST)
                out.append(self.code(list(line.resize((count, 1), Image.BOX, box=(x0, 0, x1, 1)).getdata()), chroma))
            return out
        y, = profile(False)
        cb, cr = profile(True)
        return y, cb, cr

    # -- YCbCr ---------------------------------------------------------------
    def ycc_rect(self, x0, x1, y0, y1):
        """(Y, Cb, Cr) of a rectangle as lists of items (image, box or None) -- several
        for SAND, one per touched column; P030 raw probes, see code(). Zone means over
        such a list weigh each item by its width."""
        ys = self.luma(x0, x1, y0, y1)
        if self.sand:
            cs = self._sand(x0, x1, y0, y1, True)
            return ys, [c[0] for c in cs], [c[1] for c in cs]
        cx0, cx1, cy0, cy1 = x0 // 2, max(x0 // 2 + 1, x1 // 2), y0 // 2, max(y0 // 2 + 1, y1 // 2)
        if self.fourcc == "YU12":
            u = self._plane(1, "L", cx0, cx1, cy0, cy1, self.cstep)
            v = self._plane(2, "L", cx0, cx1, cy0, cy1, self.cstep)
        else:  # NV12: interleaved U, V read as L, A
            img, box = self._plane(1, "LA", cx0, cx1, cy0, cy1, self.cstep)
            u, v = ((c, box) for c in img.split())
        return ys, [u], [v]

    def luma_profile(self, x0, x1):
        """Mean luma (8-bit code values) per sampled row over columns x0..x1: (values,
        rows per value)."""
        ys = self.luma(x0, x1, 0, self.h)  # luma only: the bars need no colour
        n = min(_size(it)[1] for it in ys)
        return self.code(_zones(ys, n, False), False), self.h / n

def _size(item):
    im, box = item
    return (box[2] - box[0], box[3] - box[1]) if box else im.size

def _zones(items, count, across):
    """Zone means over items (image, box or None) that together form one rectangle side
    by side (SAND columns). across=False: zones from top to bottom (left/right edges);
    True: from left to right (top/bottom edges)."""
    if not across:
        total = sum(_size(it)[0] for it in items)
        out = [0.0] * count
        for im, box in items:
            weight = _size((im, box))[0] / total
            for i, v in enumerate(im.resize((1, count), Image.BOX, box=box).getdata()):
                out[i] += v * weight
        return out
    # across: one item -- linear planes and RGB; SAND strips go through Frame.sand_across
    if len(items) != 1:
        raise ValueError("zones across several items: use Frame.sand_across")
    im, box = items[0]
    return list(im.resize((count, 1), Image.BOX, box=box).getdata())

class Bars:
    """Letterbox bars burnt into the video: black rows at the top and the bottom.

    Kodi adds its own bars outside the plane (the video plane is then smaller), so only
    bars inside the picture land here -- films in a 16:9 broadcast or upload. The bar is
    measured at the top (the first bright row of a luma profile from the middle of the
    picture); the bottom only has to agree that its last rows are black too, because
    burnt-in subtitles often sit inside the bottom bar. "Black" is at most BLACK code
    values above video black: real bars sit at 16 (+-2 from the compression), dark
    studio backgrounds at 27-36 -- with +12 a talk show got a 232-row "bar" for 1.8 s.
    Bars above MAX of the height are no bars: even 2.76:1 in 16:9 is 14 %.
    A thicker bar is taken after CONFIRM seconds, a thinner one (picture content where
    the bar was) after SHRINK. The profile costs ~2.5 ms on a SAND frame, so it is taken
    at most every EVERY seconds."""
    CONFIRM, SHRINK, MAX = 1.0, 0.2, 0.18
    EVERY, BLACK = 0.2, 6

    def __init__(self):
        self.reset()

    def reset(self):
        self.rows, self.cand, self.since, self.h, self.last = 0, None, 0.0, None, None

    def due(self, now):
        """True if a new profile should be taken (and marks it taken)."""
        if self.last is not None and 0 <= now - self.last < self.EVERY:
            return False
        self.last = now
        return True

    def update(self, profile, rows_per_value, h, black, now):
        if h != self.h:
            self.reset()
            self.h = h
        bright = [i for i, v in enumerate(profile) if v > black]
        if not bright:
            self.cand = None  # all dark: keep what we have, and let no pending bar ripen on it
            return self.rows
        top = bright[0]
        bottom_black = all(v <= black for v in profile[len(profile) - max(1, top // 2):]) if top else True
        cand = int(top * rows_per_value) if bottom_black else 0
        if cand > self.MAX * h:
            self.cand = None  # a dark scene, not a bar: same
            return self.rows
        if abs(cand - self.rows) <= rows_per_value:
            self.cand = None
            return self.rows
        if self.cand is None or abs(cand - self.cand) > rows_per_value:
            self.cand, self.since = cand, now
        if now - self.since >= (self.SHRINK if cand < self.rows else self.CONFIRM):
            self.rows, self.cand = cand, None
        return self.rows

def frame_colors(buf, cmd, wanted, csc=DEFAULT_CSC, depth=EDGE, bars=None, now=None):
    """{(edge, count): [(r, g, b)] * count} of one frame. Left/right zones run from the
    top, top/bottom zones from the left. depth: edge depth as a fraction of the width
    (left/right) or height (top/bottom). bars: a Bars instance to cut burnt-in letterbox
    bars off the video planes (None = whole picture)."""
    f = Frame(buf, cmd)
    try:
        w, h = f.w, f.h
        top, bottom = 0, h
        if bars is not None and not f.rgb:
            now = time.time() if now is None else now
            if bars.due(now) or bars.h != h:
                prof, rpv = f.luma_profile(int(w * 0.45), int(w * 0.55))
                bars.update(prof, rpv, h, (16 if csc[2] else 0) + Bars.BLACK, now)
            top, bottom = bars.rows, h - bars.rows
        ew = max(2, int(w * depth)) & ~1
        eh = max(2, int((bottom - top) * depth)) & ~1
        rects = {"left": (0, ew, top, bottom), "right": ((w - ew) & ~1, w, top, bottom),
                 "top": (0, w, top, top + eh), "bottom": (0, w, bottom - eh, bottom)}
        out = {}
        for edge, count in wanted:
            r = rects[edge]
            across = edge in ("top", "bottom")
            if f.rgb:
                im = f.rgb_rect(*r)
                im = im.resize((count, 1) if across else (1, count), Image.BOX)
                out[edge, count] = [(rr, g, b) for b, g, rr, _ in im.getdata()]
                continue
            if across and f.sand:
                out[edge, count] = ycc_to_rgb(*f.sand_across(*r, count), csc)
                continue
            ys, us, vs = f.ycc_rect(*r)
            out[edge, count] = ycc_to_rgb(*(f.code(_zones(c, count, across), i > 0) for i, c in enumerate((ys, us, vs))), csc)
        return out
    finally:
        f.release()

def ycc_to_rgb(ys, us, vs, csc):
    """Per-zone Y, Cb, Cr means (8-bit code values) -> [(r, g, b)] with csc = (Kr, Kb,
    limited). The conversion is affine, so converting the zone means equals averaging
    the converted pixels (up to clipping)."""
    kr, kb, limited = csc
    kg = 1 - kr - kb
    ys0, yscale, cscale = (16, 219, 224) if limited else (0, 255, 255)
    out = []
    for y, u, v in zip(ys, us, vs):
        y, u, v = (y - ys0) / yscale, (u - 128) / cscale, (v - 128) / cscale
        r = y + 2 * (1 - kr) * v
        b = y + 2 * (1 - kb) * u
        g = (y - kr * r - kb * b) / kg
        out.append(tuple(min(255, max(0, int(round(c * 255)))) for c in (r, g, b)))
    return out

def pick(planes):
    """GUI alone (visualisation) > video > GUI with alpha (menus over video)."""
    for fourcc in ("XR24", "YU12", "NV12", "P030", "AR24"):
        if fourcc in planes:
            return planes[fourcc], fourcc
    return None, None

# --- DDP ---------------------------------------------------------------------

DDP_MAX_PIXELS = 480  # 1440 payload bytes: a 1500-byte MTU, the usual limit of DDP senders

def ddp_packets(spans, seq):
    """DDP packets for one frame and the next sequence number.

    spans: [(first pixel, RGB bytes)]. Longer spans are cut into packets of at most
    DDP_MAX_PIXELS; only the last packet of the frame carries PUSH, so the receiver
    shows the whole frame at once instead of each part as it arrives. Header: flags
    (version 1 | push), sequence 1..15, data type RGB 8 bit, output id 1 (display),
    byte offset, payload length.

    One sequence number per frame, shared by all its packets, counting 1..15 from frame
    to frame. WLED's optional out-of-sequence filter (wled00/e131.cpp, handleDDPPacket)
    remembers the sequence number of the last push and drops packets that look late
    (numbered a few below it): numbering every packet makes it drop packets of any
    frame with more than a few of them. The next frame's
    number is always last + 1, which the filter lets through."""
    parts = []
    for first, data in sorted(spans):
        for i in range(0, len(data), DDP_MAX_PIXELS * 3):
            parts.append((first * 3 + i, data[i:i + DDP_MAX_PIXELS * 3]))
    packets = []
    seq = seq % 15 + 1
    for n, (offset, data) in enumerate(parts):
        flags = 0x40 | (0x01 if n == len(parts) - 1 else 0)
        packets.append(struct.pack(">BBBBIH", flags, seq, 0x0B, 1, offset, len(data)) + data)
    return packets, seq

# --- state from the add-on ---------------------------------------------------

class StdinGate:
    """State lines from the add-on (kodi_service.py): "on", "paused", "test" or "off", about
    twice a second. No line for STATE_TIMEOUT -> off (Kodi hangs); end of input -> exit
    (Kodi or the add-on stopped, so the worker never outlives them)."""

    def __init__(self, stream):
        self.stream, self.line, self.at = stream, "off", time.monotonic()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.stream:
            self.line, self.at = line.strip(), time.monotonic()
        os._exit(0)

    def state(self):
        if time.monotonic() - self.at > STATE_TIMEOUT:
            return False, False, False
        return self.line in ("on", "paused", "test"), self.line == "paused", self.line == "test"

# --- test pattern ------------------------------------------------------------

PATTERN = {"left": (255, 0, 0), "right": (0, 0, 255), "top": (0, 255, 0), "bottom": (255, 200, 0)}
PATTERN_LAP, PATTERN_DIM = 3.0, 0.12  # s per run of the dot along an edge; brightness of the rest

def test_pattern(wanted, now):
    """Zone colours of a moving test picture, for setting up the strips: every edge in
    its own colour (PATTERN), dim, with one bright dot running through the *picture* --
    down the left and right edges, left to right along top and bottom, all in step. It
    goes through zone_colors like a real frame, so with the right settings the dots on
    both sides run down together; a wrong direction, first LED or edge shows at once."""
    phase = (now / PATTERN_LAP) % 1.0
    out = {}
    for edge, count in wanted:
        r, g, b = PATTERN[edge]
        dot = int(phase * count)
        out[edge, count] = [(r, g, b) if i == dot else (r * PATTERN_DIM, g * PATTERN_DIM, b * PATTERN_DIM)
                            for i in range(count)]
    return out

# --- main loop ---------------------------------------------------------------

def zone_colors(colors, zone):
    col = colors[zone["edge"], zone["count"]][:zone["use"]]
    return col if zone["top_first"] else col[::-1]

def run(cfg, make_gate, dry=False):
    """Main loop; cfg from load_config. make_gate() -> object with state() -> (active,
    paused, test), called only after the checks: the stdin gate ends the process at the end of
    its input, which must not swallow the reason a bad configuration stops here."""
    zones = zones_from_config(cfg)
    if not zones:
        print("tvlight: no LEDs configured on any edge", flush=True)
        return 2
    target = (cfg["host"], cfg["port"])
    if not dry and not cfg["host"]:
        print("tvlight: no WLED host configured", flush=True)
        return 2
    gate = make_gate()
    smooth, depth, full_fps = cfg["response"] / 100, cfg["depth"] / 100, cfg["fps"]
    bars = Bars() if cfg["bars"] else None
    shown_bars = 0
    # card0 is opened only once Kodi is on screen (gate on), never at boot before
    # Kodi has taken DRM master.
    card = planes = None
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cur = [[(0.0, 0.0, 0.0)] * z["use"] for z in zones]
    seq, active, paused, test, gate_at, last_err, last_ok = 0, dry, False, False, 0.0, None, 0.0
    wanted = set((z["edge"], z["count"]) for z in zones)
    print("tvlight: %s -> %s:%d" % (", ".join("%s %d LEDs from %d" % (z["edge"], z["use"], z["offset"])
                                                for z in zones), *target), flush=True)
    send_err = None

    def zone_bytes(i):
        return bytes(min(255, int(v)) for px in cur[i] for v in px)

    def send():
        """All zones of the current frame, push on the last packet. A network error
        (WLED away, no route) is logged once and the frame dropped -- not fatal."""
        nonlocal seq, send_err
        packets, seq = ddp_packets([(z["offset"], zone_bytes(i)) for i, z in enumerate(zones)], seq)
        try:
            for p in packets:
                sock.sendto(p, target)
        except OSError as e:
            if str(e) != send_err:
                print("tvlight: send: %s" % e, flush=True)
                send_err = str(e)
            return
        send_err = None

    while True:
        t0 = time.time()
        if not dry and t0 - gate_at > GATE_INTERVAL / 2:
            now, now_paused, now_test = gate.state()
            if now_test != test and now:
                print("tvlight: %s" % ("test pattern" if now_test else "picture"), flush=True)
            if now != active:
                print("tvlight: %s" % ("on" if now else "off"), flush=True)
                cur = [[(0.0, 0.0, 0.0)] * z["use"] for z in zones]
                last_ok = 0.0
                if bars:
                    bars.reset()  # the next video may have other bars
                if planes and now:
                    planes.last_scan = 0.0  # look for a new video plane at once, not within 1 s
            active, paused, test, gate_at = now, now_paused, now_test, t0
        fps = PAUSED_FPS if paused else full_fps
        if not active:
            time.sleep(GATE_INTERVAL / 2)
            continue
        if test:  # the settings dialog is open: no picture needed, the card stays closed
            colors, fourcc = test_pattern(wanted, t0), "test"
        elif card is None:
            # A DRM node that is not there yet or a failing ioctl is a state of the box,
            # not a bad configuration: retry on the next round, and close what was opened.
            try:
                card = open_card()
                planes = Planes(card)
            except OSError as e:
                if card is not None:
                    os.close(card)
                    card = None
                if str(e) != last_err:
                    print("tvlight: drm: %s" % e, flush=True)
                    last_err = str(e)
                time.sleep(1 / fps)
                continue
        try:
            if not test:
                fb, fourcc = pick(planes.current())
                if fb is None:
                    raise ValueError("no plane on screen")
                colors = edge_colors(card, fb, wanted, planes.colorspace(fourcc), depth, bars)
        except (OSError, ValueError) as e:
            if str(e) != last_err:  # log each distinct problem once, not 20 times a second
                print("tvlight: skip frame: %s" % e, flush=True)
                last_err = str(e)
            # Hold the last colours over short gaps: after 2.5 s without data WLED leaves
            # realtime and flashes segment 0's own colour. A longer failure still falls back.
            if not dry and t0 - last_ok < KEEPALIVE:
                send()
            time.sleep(1 / fps)
            continue
        last_err, last_ok = None, t0
        if bars and bars.rows != shown_bars:  # every change of the detected bars, for diagnosis
            print("tvlight: bars %d rows of %d" % (bars.rows, bars.h or 0), flush=True)
            shown_bars = bars.rows
        # Paused: few frames, so take the picture as it is instead of easing into it
        k = 1.0 if paused else smooth
        for i, z in enumerate(zones):
            new = zone_colors(colors, z)
            cur[i] = [tuple(a + k * (b - a) for a, b in zip(c0, c1)) for c0, c1 in zip(cur[i], new)]
            if dry:
                data = zone_bytes(i)
                print(fourcc, z["edge"], "top", data[:3].hex(), "bottom", data[-3:].hex(), flush=True)
        if not dry:
            send()
        time.sleep(max(0, 1 / fps - (time.time() - t0)))

def main(argv):
    """Exit codes: 2 = the configuration cannot work (restarting will not help), 1 = the
    box failed (retryable); an exception in run() is a crash and ends the same way."""
    worker = "--worker" in argv
    try:
        if worker:
            # From the add-on: config as the first line, then state lines; nice like a
            # background job, the reading must never cost Kodi frames.
            os.nice(10)
            raw = json.loads(sys.stdin.readline() or "{}")
        else:
            raw = {}
        cfg = load_config(raw)
    except (ValueError, OSError, IndexError) as e:
        print("tvlight: bad configuration: %s" % e, flush=True)
        return 2
    dry = "--dry" in argv
    if not worker and not dry:
        print("usage: tvlight.py --dry (the add-on starts --worker itself)", flush=True)
        return 2
    if not worker:  # a dry run: one zone per side
        cfg["edges"] = [dict(e, leds=27) for e in cfg["edges"]]
    try:
        return run(cfg, (lambda: StdinGate(sys.stdin)) if worker else (lambda: None), not worker)
    except OSError as e:  # the system, not the settings: the add-on restarts the worker after a pause
        print("tvlight: error: %s" % e, flush=True)
        return 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
