#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Build the installable add-on zip: dist/service.tvlight-<version>.zip with the
service.tvlight/ folder and LICENSE.txt, as Kodi's "Install from zip file" expects.

    python tools/build_zip.py
"""
import os, re, sys, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON = "service.tvlight"

def build(out_dir=os.path.join(ROOT, "dist")):
    xml = open(os.path.join(ROOT, ADDON, "addon.xml"), encoding="utf-8").read()
    version = re.search(r'<addon[^>]*\bversion="([^"]+)"', xml).group(1)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "%s-%s.zip" % (ADDON, version))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(os.path.join(ROOT, ADDON)):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for f in sorted(files):
                if f.endswith(".pyc"):
                    continue
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, ROOT).replace(os.sep, "/"))
        z.write(os.path.join(ROOT, "LICENSE"), ADDON + "/LICENSE.txt")
    return path

if __name__ == "__main__":
    print(build(*sys.argv[1:]))
