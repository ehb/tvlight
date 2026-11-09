#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Mutation check: each deliberate bug in a copy of the add-on must turn the selftest red.
# Run after changing test/tvlight_selftest.py:  python test/selftest_mutants.py
import os, shutil, subprocess, sys, tempfile
SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = "service.tvlight/resources/lib/tvlight.py"
SERVICE = "service.tvlight/resources/lib/kodi_service.py"
MUTANTS = [
    (ENGINE, "R/B not swapped (XR24)", "[(rr, g, b) for b, g, rr, _ in im.getdata()]", "[(b, g, rr) for b, g, rr, _ in im.getdata()]"),
    (ENGINE, "NV12 Cb/Cr swapped", "u, v = ((c, box) for c in img.split())", "v, u = ((c, box) for c in img.split())"),
    (ENGINE, "P030 Cb/Cr swapped", "cb, cr = Image.frombytes(\"LA\"", "cr, cb = Image.frombytes(\"LA\""),
    (ENGINE, "SAND reads the wrong column", "base = c * self.cstride + ", "base = max(0, c - 1) * self.cstride + "),
    (ENGINE, "full range instead of limited", "ys0, yscale, cscale = (16, 219, 224) if limited else (0, 255, 255)",
     "ys0, yscale, cscale = (0, 255, 255)"),
    (ENGINE, "BT.601 matrix ignored csc", "kr, kb, limited = csc", "kr, kb, limited = 0.299, 0.114, csc[2]"),
    (ENGINE, "right edge reads the middle", "\"right\": ((w - ew) & ~1, w, top, bottom)", "\"right\": ((w - 2 * ew) & ~1, w - ew, top, bottom)"),
    (ENGINE, "pitch ignored (YUV in place)", "\"raw\", mode, p * step, 1)", "\"raw\", mode, x1 * step, 1)"),
    (ENGINE, "tiled check dropped", "raise ValueError(\"tiled buffer", "pass  # raise ValueError(\"tiled buffer"),
    (ENGINE, "ddp push on every packet", "flags = 0x40 | (0x01 if n == len(parts) - 1 else 0)", "flags = 0x41"),
    (ENGINE, "ddp no splitting", "for i in range(0, len(data), DDP_MAX_PIXELS * 3):", "for i in range(0, len(data), len(data) or 1):"),
    (ENGINE, "span ignored", "\"count\": max(e[\"leds\"], round(e[\"leds\"] * 100 / e[\"span\"]))", "\"count\": e[\"leds\"]"),
    (ENGINE, "reverse ignored", "\"top_first\": not e[\"reverse\"]", "\"top_first\": True"),
    (ENGINE, "fps range unchecked", "number(\"fps\", 1, 60)", "number(\"fps\", 0, 60)"),
    (ENGINE, "stdin gate never times out", "if time.monotonic() - self.at > STATE_TIMEOUT:", "if False:"),
    (ENGINE, "bars: bottom not checked", "bottom_black = all(v <= black for v in profile[len(profile) - max(1, top // 2):]) if top else True", "bottom_black = True"),
    (ENGINE, "bars: no waiting", "CONFIRM, SHRINK, MAX = 1.0, 0.2, 0.18", "CONFIRM, SHRINK, MAX = 0.0, 0.0, 0.18"),
    (ENGINE, "bars: no size limit", "CONFIRM, SHRINK, MAX = 1.0, 0.2, 0.18", "CONFIRM, SHRINK, MAX = 1.0, 0.2, 1.0"),
    (ENGINE, "bars: found but not applied", "top, bottom = bars.rows, h - bars.rows", "top, bottom = 0, h"),
    (ENGINE, "top and bottom strips swapped", "\"top\": (0, w, top, top + eh), \"bottom\": (0, w, bottom - eh, bottom)",
     "\"top\": (0, w, bottom - eh, bottom), \"bottom\": (0, w, top, top + eh)"),
    (ENGINE, "top/bottom zones run downwards", "across = edge in (\"top\", \"bottom\")", "across = False"),
    (ENGINE, "bars: throttle never lets a profile through", "        self.last = now\n        return True",
     "        self.last = now\n        return False"),
    (ENGINE, "bars: threshold at +12", "EVERY, BLACK = 0.2, 6", "EVERY, BLACK = 0.2, 12"),
    (ENGINE, "P030 luma reads the column padding", "max(cx0 // 3 + 1, cx1 // 3)", "max(cx0 // 3 + 1, (cx1 + 2) // 3)"),
    (ENGINE, "P030 chroma reads the column padding", "max(cx0 // 6 + 1, (cx1 + 1) // 6)", "max(cx0 // 6 + 1, (cx1 + 5) // 6)"),
    (ENGINE, "depth ignored", "ew = max(2, int(w * depth)) & ~1", "ew = max(2, int(w * 0.1)) & ~1"),
    (ENGINE, "load_config changes the caller's dict", "for k, v in json.loads(json.dumps(raw)).items():", "for k, v in raw.items():"),
    (SERVICE, "service: screensaver ignored", "    if cond(\"System.ScreenSaverActive\"):\n        return \"off\"\n", ""),
    (SERVICE, "service: crashed worker never restarted", "        self.start_at = now + self.backoff", "        self.held = True"),
    (SERVICE, "service: backoff does not grow", "self.backoff = min(self.backoff * 2, BACKOFF_MAX)", "self.backoff = BACKOFF_FIRST"),
    (SERVICE, "service: bad settings not shown", "            notify(self.worker.reason())", "            pass"),
    (ENGINE, "SAND across: padding probes used", "row[last_k + 1:] = [row[last_k]] * (pw - last_k - 1)", "pass"),
    (ENGINE, "SAND across: columns out of order", "resize((pw, ncols), Image.BOX).convert(\"F\")", "resize((pw, ncols), Image.BOX).convert(\"F\").transpose(Image.FLIP_TOP_BOTTOM)"),
    (ENGINE, "pick prefers the GUI over video", "for fourcc in (\"XR24\", \"YU12\", \"NV12\", \"P030\", \"AR24\"):", "for fourcc in (\"XR24\", \"AR24\", \"YU12\", \"NV12\", \"P030\"):"),
    (SERVICE, "service: any add-on's settings start the pattern", 'cond("String.IsEqual(Window(addonsettings).Property(Addon.ID),%s)" % ADDON_ID)', "True"),
    (SERVICE, "service: no test pattern after saving", "self.test_until = self.clock() + TEST_AFTER", "self.test_until = 0.0"),
    (SERVICE, "service: test pattern beats off by hand", "    if test or own_settings_open():\n        return \"test\"\n", ""),
    (ENGINE, "pattern: the dot stands still", "phase = (now / PATTERN_LAP) % 1.0", "phase = 0.0"),
    (ENGINE, "stdin gate: test line not active", 'return self.line in ("on", "paused", "test")', 'return self.line in ("on", "paused")'),
    (SERVICE, "service: bars setting ignored", "\"bars\": s.getBool(\"bars\")", "\"bars\": True"),
    (SERVICE, "service: only left and right", "for e in (\"left\", \"right\", \"top\", \"bottom\")", "for e in (\"left\", \"right\")"),
    (SERVICE, "service: pause not reported", "return \"paused\" if cond(\"Player.Paused\") else \"on\"", "return \"on\""),
    (SERVICE, "service: manual off ignored", "if not cond(\"String.IsEmpty(Window(Home).Property(%s))\" % MANUAL_OFF):", "if False:"),
    (ENGINE, "P030 padding bits not masked", "P030_MASK = [v & 0x3F for v in range(256)]", "P030_MASK = [v for v in range(256)]"),
    (ENGINE, "P030 chroma biased (+2)", "return [v * 4 for v in values]", "return [v * 4 + 2 for v in values]"),
    (ENGINE, "P030 luma white not stretched", "128 + (v - 32) * P030_WHITE_SLOPE", "128 + (v - 32) * 4"),
    (ENGINE, "P030 luma biased (+2)", "v * 4 if v <= 32 else", "v * 4 + 2 if v <= 32 else"),
    (ENGINE, "planes: handles leak on a bad FourCC", "close_handles(self.card, cmd)  # also for a FourCC", "pass  # also for a FourCC"),
    (ENGINE, "bars: pending bar survives an all-dark profile", "self.cand = None  # all dark", "pass  # all dark"),
    (ENGINE, "bars: pending bar survives a dark scene", "self.cand = None  # a dark scene", "pass  # a dark scene"),
    (ENGINE, "ddp: sequence number per packet", "    seq = seq % 15 + 1\n    for n, (offset, data) in enumerate(parts):\n",
     "    for n, (offset, data) in enumerate(parts):\n        seq = seq % 15 + 1\n"),
    (ENGINE, "drm init: failure leaves run()", "            except OSError as e:\n                if card is not None:", "            except ValueError as e:\n                if card is not None:"),
    (ENGINE, "drm init: Planes failure leaks the card", "                if card is not None:\n                    os.close(card)\n", "                if card is not None:\n                    pass\n"),
    (ENGINE, "main: a system error is a bad configuration", "flush=True)\n        return 1", "flush=True)\n        return 2"),
    (ENGINE, "config: reverse as text accepted", "if not isinstance(e[\"reverse\"], bool):", "if False:"),
    (ENGINE, "config: zone count unlimited", "if zones > MAX_ZONES:", "if False:"),
    (ENGINE, "config: a list accepted as the configuration", "if not isinstance(raw, dict):", "if False:"),
    (ENGINE, "config: edges not checked for a list", "if not isinstance(cfg[\"edges\"], list):", "if False:"),
    (ENGINE, "config: edge entry not checked for an object", "if not isinstance(e, dict):", "if False:"),
    (ENGINE, "config: unknown edge key accepted", "if k not in DEFAULTS[\"edges\"][0]:", "if False:"),
    (ENGINE, "stdin gate on the wall clock", "if time.monotonic() - self.at > STATE_TIMEOUT:", "if time.time() - self.at > STATE_TIMEOUT:"),
    (SERVICE, "worker: a full pipe is not stuck", "        if done < len(data):", "        if False:"),
    (SERVICE, "service: a stuck worker is kept", "            if self.worker.stuck:", "            if False:"),
    (SERVICE, "service: start failures not caught", "except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as e:", "except KeyError as e:"),
    (SERVICE, "service: failed start without backoff", "xbmc.LOGWARNING)\n                self.later(now)\n", "xbmc.LOGWARNING)\n                pass\n"),
    (SERVICE, "worker: half-built child not reaped", "self.stop()  # reap a half-built child", "pass  # reap a half-built child"),
    (SERVICE, "service: shutdown not in a finally", "        finally:  # also when tick() raises", "        except KeyError:  # also when tick() raises"),
    (SERVICE, "service: host not trimmed", "s.getString(\"host\").strip()", "s.getString(\"host\")"),
]


def run(mutants, check):
    red = 0
    for path, name, old, new in mutants:
        d = tempfile.mkdtemp()
        shutil.copytree(SRC, os.path.join(d, "r"), ignore=shutil.ignore_patterns(".git", "__pycache__", "dist"))
        target = os.path.join(d, "r", path)
        src = open(target, encoding="ascii").read()
        if src.count(old) != 1:
            print("SKIP   %-40s anchor found %d times" % (name, src.count(old)))
            shutil.rmtree(d)
            continue
        open(target, "w", encoding="ascii", newline="").write(src.replace(old, new))
        r = subprocess.run([sys.executable, os.path.join(d, "r", check)], capture_output=True, text=True, timeout=300)
        # Caught = the selftest ran to its end and reported a failed check (exit 1, a FAIL
        # line, the summary line). A traceback, a missing interpreter or a timeout is a crash
        # of the mutant, not proof that the tests notice the bug.
        fails = [l for l in r.stdout.splitlines() if l.startswith("FAIL")]
        broken = any(w in l for l in fails for w in ("NameError", "SyntaxError", "ImportError"))  # the mutant itself is broken
        caught = r.returncode == 1 and bool(fails) and " FAILED: " in r.stdout and "Traceback" not in r.stderr and not broken
        red += caught
        verdict = "caught" if caught else ("CRASH" if broken or r.returncode not in (0, 1) or "Traceback" in r.stderr else "MISSED")
        print("%-6s %-40s %s" % (verdict, name, (fails[0][5:70] if fails else r.stderr.strip()[-60:])))
        shutil.rmtree(d)
    print("%d/%d mutants caught" % (red, len(mutants)))
    return 0 if red == len(mutants) else 1


if __name__ == "__main__":
    sys.exit(run(MUTANTS, "test/tvlight_selftest.py"))
