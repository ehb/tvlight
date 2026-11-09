# SPDX-License-Identifier: GPL-2.0-or-later
"""Kodi side of tvlight (started by service.py, the add-on's entry point): reads the settings, asks Kodi twice a second whether the
visualisation or fullscreen video is on screen, and hands that to the worker
(resources/lib/tvlight.py) running as its own python3 process. The worker does the
DRM reading and the DDP sending; keeping it out of Kodi's Python keeps that load off
Kodi's interpreter, and it ends by itself when its input closes."""
import json, os, shutil, subprocess, threading, time
import xbmc, xbmcaddon, xbmcgui

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo("id")
WORKER = os.path.join(ADDON.getAddonInfo("path"), "resources", "lib", "tvlight.py")
MANUAL_OFF = "tvlight.off"  # home window property: while set, the light is off by hand (cleared on Kodi restart)
TICK = 0.5                  # s between state lines; the worker drops to off after 5 s without
TEST_AFTER = 20.0           # s of test pattern after the settings were saved

def log(msg, level=xbmc.LOGINFO):
    xbmc.log("[%s] %s" % (ADDON_ID, msg), level)

def config():
    """Settings -> the worker's configuration (tvlight.load_config checks it)."""
    s = xbmcaddon.Addon().getSettings()  # fresh object: values change at runtime
    edge = lambda name: {"edge": name, "first": s.getInt(name + "_first"), "leds": s.getInt(name + "_leds"),
                         "span": s.getInt(name + "_span"), "reverse": s.getBool(name + "_reverse")}
    return {"host": s.getString("host").strip(), "port": s.getInt("port"), "fps": s.getInt("fps"),
            "response": s.getInt("response"), "depth": s.getInt("depth"), "bars": s.getBool("bars"),
            "edges": [edge(e) for e in ("left", "right", "top", "bottom")]}

def enabled():
    return xbmcaddon.Addon().getSettings().getBool("enabled")

def own_settings_open():
    """tvlight's own settings dialog is on screen. Kodi keeps the add-on id of the last
    settings dialog in this property, so it needs the dialog to be active as well."""
    cond = xbmc.getCondVisibility
    return cond("Window.IsActive(addonsettings)") and \
        cond("String.IsEqual(Window(addonsettings).Property(Addon.ID),%s)" % ADDON_ID)

def state(test=False):
    """The line for the worker: on / paused / test / off. Off while a screensaver is up:
    over a paused video it would otherwise leave the room lit with the frozen picture.
    "test" (a moving test pattern) while tvlight's settings are open, or when the caller
    asks for it (just after saving), so the effect of the settings can be seen."""
    cond = xbmc.getCondVisibility
    if not cond("String.IsEmpty(Window(Home).Property(%s))" % MANUAL_OFF):
        return "off"
    if cond("System.ScreenSaverActive"):
        return "off"
    if test or own_settings_open():
        return "test"
    if cond("Window.IsActive(visualisation)"):
        return "on"
    if cond("Window.IsActive(fullscreenvideo)"):
        return "paused" if cond("Player.Paused") else "on"
    return "off"

def notify(message):
    xbmcgui.Dialog().notification("tvlight", message, xbmcgui.NOTIFICATION_WARNING, 10000)

BAD_CONFIG = 2               # worker exit code: the settings cannot work, restarting will not help
BACKOFF_FIRST, BACKOFF_MAX = 5.0, 60.0   # s before restarting a worker that ended otherwise

class Worker:
    """The worker process. Writes to its input never block Kodi: the pipe is non-blocking,
    and a write that does not go through (the pipe is full, so the worker has stopped
    reading) marks the worker as stuck; the service then stops and restarts it."""

    def __init__(self, cfg):
        self.last, self.stuck, self.proc = "", False, None
        try:
            python = shutil.which("python3") or "/usr/bin/python3"
            self.proc = subprocess.Popen([python, WORKER, "--worker"], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                os.set_blocking(self.proc.stdin.fileno(), False)
            except (OSError, ValueError, AttributeError):
                pass  # no non-blocking pipes here: the write below is then as bounded as the OS makes it
            threading.Thread(target=self._log, daemon=True).start()  # before the write: a worker that
            self.send(json.dumps(cfg))  # ends at once still gets its last words logged
            log("worker %d started: %s" % (self.proc.pid, json.dumps(cfg)))
        except BaseException:
            self.stop()  # reap a half-built child, then let the caller back off
            raise

    def _log(self):
        for raw in self.proc.stdout:
            self.last = raw.decode("utf-8", "replace").rstrip()
            log(self.last)
        log("worker %d ended, exit %s" % (self.proc.pid, self.proc.wait()))

    def exit_code(self):
        """None while running."""
        return self.proc.poll()

    def reason(self):
        """The worker's last log line without its prefix, for a notification."""
        return self.last.split("tvlight: ", 1)[-1] or "stopped"

    def send(self, line):
        """One line to the worker, never blocking. A gone worker is noticed through
        exit_code(); a full pipe sets stuck."""
        data = (line + "\n").encode()
        try:
            done = os.write(self.proc.stdin.fileno(), data)
        except BlockingIOError:
            done = 0
        except (OSError, ValueError):  # worker gone
            return
        if done < len(data):
            self.stuck = True

    def stop(self):
        """End of input, then terminate, then kill: each step waits a bounded time."""
        if self.proc is None:
            return
        try:
            self.proc.stdin.close()  # end of input: the worker exits on its own
        except (OSError, ValueError):
            pass
        for step in (lambda: None, self.proc.terminate, self.proc.kill):
            step()
            try:
                self.proc.wait(2)
                return
            except subprocess.TimeoutExpired:
                pass

class Service(xbmc.Monitor):
    """Keeps one worker running while enabled. A worker that ends with BAD_CONFIG stays
    down until the settings change (and says why on screen); one that ends otherwise --
    a crash -- is restarted after BACKOFF_FIRST seconds, doubling up to BACKOFF_MAX."""

    def __init__(self, make_worker=Worker, clock=time.monotonic):
        super().__init__()
        self.make_worker, self.clock = make_worker, clock
        self.worker, self.dirty = None, True
        self.held, self.start_at, self.backoff, self.started = False, 0.0, BACKOFF_FIRST, 0.0
        self.test_until = 0.0

    def onSettingsChanged(self):
        self.dirty = True  # restart the worker with the new settings
        self.test_until = self.clock() + TEST_AFTER  # and show them on the test pattern

    def tick(self):
        now = self.clock()
        if self.dirty:
            self.dirty = False
            if self.worker:
                self.worker.stop()
                self.worker = None
            self.held, self.start_at, self.backoff = False, 0.0, BACKOFF_FIRST
        if self.worker is None and not self.held and now >= self.start_at and enabled():
            try:
                self.worker, self.started = self.make_worker(config()), now
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:
                # no interpreter, no process or thread left, a pipe that fails: back off like a crash
                log("worker could not be started (%s), retry in %d s" % (e, self.backoff), xbmc.LOGWARNING)
                self.later(now)
        if self.worker is None:
            return
        code = self.worker.exit_code()
        if code is None:
            self.worker.send(state(test=now < self.test_until))
            if self.worker.stuck:  # the worker stopped reading its input
                log("worker does not read its input, restarting in %d s" % self.backoff, xbmc.LOGWARNING)
                self.worker.stop()
                self.worker = None
                self.later(now)
                return
            if now - self.started > BACKOFF_MAX:
                self.backoff = BACKOFF_FIRST  # ran long enough: the next crash starts over
            return
        if code == BAD_CONFIG:
            self.held = True
            notify(self.worker.reason())
        else:
            if code == -15:  # SIGTERM: systemd stopping Kodi takes the worker down first
                log("worker ended by SIGTERM (Kodi stopping?), restart in %d s" % self.backoff)
            else:
                log("worker crashed (exit %s), restart in %d s" % (code, self.backoff), xbmc.LOGWARNING)
            self.later(now)
        self.worker = None

    def later(self, now):
        """Start the next worker after the current pause, which doubles up to BACKOFF_MAX."""
        self.start_at = now + self.backoff
        self.backoff = min(self.backoff * 2, BACKOFF_MAX)

    def loop(self):
        try:
            while not self.abortRequested():
                self.tick()
                if self.waitForAbort(TICK):
                    break
        finally:  # also when tick() raises: the worker must not outlive the service
            if self.worker:
                self.worker.stop()
            log("stopped")
