#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Draw the add-on artwork: service.tvlight/resources/icon.png (512x512, solid PNG) and
fanart.jpg (1920x1080, under 1000 KB) -- the sizes and formats kodi-addon-checker accepts.
A dark TV with a coloured glow spilling from its edges onto the wall.

    python tools/make_artwork.py
"""
import os
from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "service.tvlight", "resources")
WALL = (14, 16, 22)
BEZEL = (40, 43, 52)
# glow per edge, as the light would pick it up from a picture: warm left, cool right
GLOW = {"left": (255, 120, 40), "top": (255, 70, 150), "right": (40, 140, 255), "bottom": (90, 220, 160)}

def scene(w, h, tv_w, tv_h, glow_px, gain):
    img = Image.new("RGB", (w, h), WALL)
    x0, y0 = (w - tv_w) // 2, (h - tv_h) // 2
    x1, y1 = x0 + tv_w, y0 + tv_h
    light = Image.new("RGB", (w, h), (0, 0, 0))
    d = ImageDraw.Draw(light)
    band = glow_px // 2
    d.rectangle((x0 - band, y0, x0, y1), fill=GLOW["left"])
    d.rectangle((x1, y0, x1 + band, y1), fill=GLOW["right"])
    d.rectangle((x0, y0 - band, x1, y0), fill=GLOW["top"])
    d.rectangle((x0, y1, x1, y1 + band), fill=GLOW["bottom"])
    light = light.filter(ImageFilter.GaussianBlur(glow_px / 3.5))
    light = light.point(lambda v: min(255, int(v * gain)))  # the blur thins it out: lift it again
    img = ImageChops.screen(img, light)
    d = ImageDraw.Draw(img)
    r = max(4, tv_h // 40)
    d.rounded_rectangle((x0, y0, x1, y1), radius=r, fill=BEZEL)
    m = max(3, tv_h // 45)
    # a dim picture on the screen in the same colours: the light comes out of it
    sx0, sy0, sx1, sy1 = x0 + m, y0 + m, x1 - m, y1 - m
    pic = Image.new("RGB", (2, 2))
    pic.putdata([GLOW["top"], GLOW["top"], GLOW["left"], GLOW["right"]])
    pic = pic.resize((sx1 - sx0, sy1 - sy0), Image.BILINEAR)
    bottom = Image.new("RGB", pic.size, GLOW["bottom"])
    fade = Image.linear_gradient("L").resize(pic.size)
    pic = Image.composite(bottom, pic, fade.point(lambda v: v // 3))
    pic = pic.point(lambda v: v * 45 // 100)
    mask = Image.new("L", pic.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, pic.width - 1, pic.height - 1), radius=max(2, r - m // 2), fill=255)
    img.paste(pic, (sx0, sy0), mask)
    return img

def main():
    icon = scene(512, 512, 300, 170, 150, 2.6)
    icon.save(os.path.join(OUT, "icon.png"), optimize=True)
    fanart = scene(1920, 1080, 1000, 563, 420, 1.1)  # calmer: Kodi puts text over it
    fanart.save(os.path.join(OUT, "fanart.jpg"), quality=85, optimize=True)
    for f in ("icon.png", "fanart.jpg"):
        p = os.path.join(OUT, f)
        im = Image.open(p)
        print(f, im.size, im.mode, os.path.getsize(p) // 1024, "KB")

if __name__ == "__main__":
    main()
