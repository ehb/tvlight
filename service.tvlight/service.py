# SPDX-License-Identifier: GPL-2.0-or-later
# Entry point only, as Kodi asks: the service lives in resources/lib/kodi_service.py.
import os, sys
import xbmcaddon
sys.path.insert(0, os.path.join(xbmcaddon.Addon().getAddonInfo("path"), "resources", "lib"))
import kodi_service  # noqa: E402
kodi_service.Service().loop()
