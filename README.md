# tvlight

Ambient light for Kodi on a Raspberry Pi 4: tvlight reads the edges of the picture
straight from Kodi's display planes and sends the colours to a
[WLED](https://kno.wled.ge/) controller over DDP. No HDMI grabber, no capture card --
the Pi looks at what it is about to display.

It runs as a Kodi service add-on (`service.tvlight`) and is active only while the music
visualisation or a fullscreen video is on screen. In menus it sends nothing, and WLED
returns to its own light after its realtime timeout (2.5 s by default).

## Why

On LibreELEC 12 (Kodi 21 with GBM/DRM and the V4L2 video decoders) the classic software
grabbers do not see the picture: Kodi draws video and GUI on separate DRM planes, and
the video plane is often not even linear. The usual advice is an HDMI splitter plus a USB
capture stick. tvlight reads the planes directly instead, in every format the Pi 4 uses.

## Requirements

- Raspberry Pi 4 with LibreELEC 12 (Kodi 21). Other vc4/KMS setups may work; the Pi 5
  (different video decoder) is untested.
- A WLED controller on the network with DDP (on by default, UDP port 4048). LED numbers
  in tvlight are the ones WLED receives realtime data on: by default counted over the
  whole strip (a WLED ledmap applies); with WLED's "Use main segment only" relative to the
  main segment.
- Python 3 with Pillow 10 or newer on the box -- LibreELEC 12 ships both.

## Install

1. Build the zip on any machine with Python: `python tools/build_zip.py` writes
   `dist/service.tvlight-<version>.zip`.
2. Copy it to the box, e.g. into LibreELEC's `Downloads` network share
   (`\\<box address>\Downloads`) or onto a USB stick.
3. In Kodi, allow add-ons from zip files once: *Settings -> System -> Add-ons -> Unknown
   sources*.
4. *Add-ons -> Install from zip file*, then pick the zip (`Downloads` or the stick).
5. Open *Add-ons -> My add-ons -> Services -> tvlight -> Configure* and enter the WLED
   address and your strips. Until at least one edge has LEDs, tvlight does nothing and
   says so in a notification.

## Settings

| Setting | Meaning |
|---|---|
| Enabled | Off: tvlight sends nothing, the LEDs keep WLED's own light. |
| WLED address, DDP port | IP of the controller (more reliable than a host name on LibreELEC), port 4048. |
| Frames per second | How often the edges are read and sent (default 20). |
| Responsiveness (%) | How far the LEDs move towards the new colour per frame: 100 follows at once, lower is calmer. |
| Edge depth (%) | How far into the picture each edge is averaged. |
| Detect black bars | Leaves out letterbox bars burnt into the video (see below). |
| Per edge (left, right, top, bottom): number of LEDs | 0 = no strip on that edge. |
| First LED | Index of the strip's first LED in WLED's main segment. |
| Covers part of the edge (%) | For a strip shorter than the TV side: how much of the edge it covers, from the top (left/right) or from the left (top/bottom). It keeps the scale of a full strip. |
| First LED at the bottom / at the right | The strip runs the other way. |

Example: a TV with 27 LEDs on the left (0-26, top to bottom) and a shorter right strip of
21 LEDs (27-47) that covers the upper 78 % of the right side: left = 27 LEDs from 0, right
= 21 LEDs from 27 covering 78 %.

Example: one chain around the TV, starting bottom right and running counter-clockwise
(17 LEDs per side, 30 along top and bottom): right = 17 from 0, first LED at the bottom;
top = 30 from 17, first LED at the right; left = 17 from 47; bottom = 30 from 64.

**Setting up the strips with the test pattern:** while tvlight's settings dialog is open,
and for 20 s after you save, the strips show a test pattern instead of the picture: every
edge in its own colour (left red, right blue, top green, bottom yellow), dim, with one bright
dot running down the left and right edges and from left to right along top and bottom, all
in step. With the right settings the two side dots run down together and the top and bottom
dots run to the right; a dot running the wrong way means "first LED at the bottom/right" is
wrong for that edge, a colour on the wrong edge means its first LED or LED count is off. Kodi
applies changed settings when you close the dialog -- close it, watch the 20 s, open again.

**Switching it off by hand:** while the home window property `tvlight.off` is set, the
light stays off; it is cleared when Kodi restarts. Any script or skin can set it (in
Python: `xbmcgui.Window(10000).setProperty("tvlight.off", "1")`, tested). From a keymap,
Kodi's builtins `SetProperty(tvlight.off,1,home)` / `ClearProperty(tvlight.off,home)` should
do the same -- untested.

## Troubleshooting

- **Nothing lights up.** tvlight writes everything to Kodi's log, each line prefixed with
  `[service.tvlight]` (on LibreELEC `/storage/.kodi/temp/kodi.log`, also in the `Logfiles`
  network share). A configuration it cannot work with (no LEDs on any edge, no WLED
  address) also shows up as a notification on screen; it waits until you change a setting.
- **Is WLED receiving?** `http://<wled address>/json/info` shows `"live": true` and
  `"lm": "DDP"` while tvlight sends.
- **What does tvlight see?** Over ssh on the box, while a video plays:
  `python3 /storage/.kodi/addons/service.tvlight/resources/lib/tvlight.py --dry` prints
  the format of the plane on screen and the colours of the top and bottom zones, without
  sending anything (Ctrl+C to stop).
- **Light stays on the last picture** after you leave a video: it should go back to WLED's
  own light within its realtime timeout (2.5 s by default in WLED's sync settings).

## How it works

- The service (`service.py`, which starts `resources/lib/kodi_service.py`) runs inside
  Kodi: it reads the settings and twice a second whether the
  visualisation or fullscreen video is showing (and whether video is paused or a
  screensaver is up -- then the light is off). It starts `resources/lib/tvlight.py` as a
  separate `python3` process and hands it the configuration and the state over stdin. The
  reading never runs in Kodi's interpreter, and the worker ends when Kodi does. If the
  worker crashes, it is started again after 5 s (the pause doubles up to a minute if it
  keeps crashing).
- The worker finds the active planes with `GETPLANE`/`GETFB2`, maps the framebuffer via
  PRIME and reads only a sample of rows (270 per plane) and only the edge strips.
- Formats: `XR24`/`AR24` (GUI and visualisation), `YU12`/`NV12` (H.264), and the
  column-tiled Broadcom SAND128 layout the HEVC decoder hands out (`NV12` for 8 bit,
  `P030` for 10 bit).
- Colour: the YCbCr matrix and range come from the video plane's own `COLOR_ENCODING` and
  `COLOR_RANGE` properties, so the LEDs use the same conversion as the display (video is
  usually BT.709 limited range: black is code 16, not 0).
- Black bars: only bars inside the video need detecting (a widescreen film in a 16:9
  broadcast); Kodi's own bars lie outside the plane. tvlight measures the bar at the top
  and only checks that the bottom rows are black too, because burnt-in subtitles often
  sit in the bottom bar. A bar must be stable for a second before it is used.
- Paused video: 2 frames per second, so WLED stays in realtime mode at almost no cost.
- Output: one DDP frame per picture, split into packets of at most 480 pixels, with the
  push flag on the last packet only.

Cost on a Pi 4 at 20 fps, measured with two side strips and Kodi's GUI at 1080p: about
10-18 % of one core (H.264 1080p 10-13 %, HEVC 15-18 %, visualisation 10-12 %, paused video
about 1 %). Four edges add the top and bottom strips: per frame about 5 ms for H.264,
8-10 ms for HEVC and 6 ms for the visualisation, i.e. up to about 20 % of a core with HEVC.
Lower the frame rate if that is too much. A GUI drawn at 4K makes the visualisation more
expensive (not measured); if your Kodi offers *Settings -> System -> Display -> Limit GUI
size*, 1080p is plenty for the light.

## Limits

- Raspberry Pi 4 (vc4) only so far; the Pi 5 is untested. The code relies on how the vc4
  driver behaves and makes these assumptions, none of which are checked at run time:
  - a single display, with the video or GUI plane covering the whole frame and opaque
    (a video shown smaller than the screen is not accounted for);
  - YUV framebuffers consist of a single buffer object (the planes share one handle);
  - SAND128 buffers have zero luma offsets in each column;
  - the buffers are read through a plain mmap without DMA-BUF cache synchronisation, so
    a frame can in principle be read while it is still being written (not handled).
- HDR (PQ/HLG): the colours come out dim and washed out -- the BT.2020 matrix is
  applied, but there is no PQ/HLG linearisation or tone mapping. Untested on real HDR
  material.
- Burnt-in subtitles that overlap the picture colour the bottom strip.
- 10-bit HEVC is sampled at 6-bit precision per probe -- invisible after averaging, but
  not exact.
- Pillarbox bars burnt into the video (black at the sides) are not detected.
- Top and bottom edges are tested with synthetic frames only.
- WLED over DDP only.

## Ideas

- **Feed Hyperion / HyperHDR instead of (or besides) WLED.** Since the GBM/V4L2 video
  pipeline (LibreELEC 10+), there is no working software grabber for the Pi 4 in the
  Hyperion world. tvlight already reads every plane format the Pi 4 uses; sending a small
  frame (e.g. the edge strips) to Hyperion's network input would make it that grabber. Not
  planned by the author -- contributions welcome.

## For developers: traps

- **Never be the first to open `/dev/dri/card0` at boot.** The first opener becomes DRM
  master; if that is tvlight, Kodi fails with `failed to authorize drm magic` and stays on
  the splash screen. tvlight opens the card only once Kodi is on screen and drops master
  at once (reading planes needs no master as root).
- Read all edges from **one** `GETFB2`: at 60 fps the framebuffer id can be gone by the
  second call (`ENOENT`).
- The GUI planes and the decoder buffers want opposite reads: GUI rows are fast as
  slices of the mapping (memcpy), decoder buffers fast in place (`Image.frombuffer` on
  the mapping). Use `RGBX`, never `RGBA`, for the GUI planes: Pillow premultiplies RGBA
  before a resize, which cost 133 ms per frame.
- Never read `/sys/kernel/debug/dri/0/state` per frame: 85 ms and the modeset locks.

## Development

- `python test/tvlight_selftest.py` -- runs anywhere, no Pi needed: synthetic frames in every
  format (with padded pitches, letterbox bars, all four edges), the colour conversion,
  the bar logic, DDP packets, the configuration, the Kodi side against a stand-in `xbmc`,
  settings against the language files. On the Pi it
  also starts the worker process.
- `python test/selftest_mutants.py` -- builds known bugs into a copy and checks that each
  one turns the selftest red.
- On the box: `python3 service.tvlight/resources/lib/tvlight.py --dry` reads the planes
  and prints the zone colours without sending anything.
- `tools/make_artwork.py` -- draws the icon and fanart.

## How this was made

The code was written by Claude Code (Anthropic); the commits carry an `Assisted-by:`
trailer. An independent review by a second model found real bugs; those are fixed and re-tested on
the device. I directed the work, made the design
decisions and did the testing on my own Raspberry Pi 4 and WLED setup, but I have not
reviewed the code line by line. Please treat it like any outside code -- issues and
questions are welcome.

## License

GPL-2.0-or-later, see `LICENSE`.
