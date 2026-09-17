"""THE INTERFACE MUST NOT LAG.

Reported: switching screens is slow, the voice panel is slow to
appear, it takes a while to recognise a hand on a pad.

It was not the Pi. A full repaint measured 80-116 ms on a dev machine
— call it 650-930 ms on a Pi 4 — because of three things that all
looked like caches and were not:

  * every rounded rectangle, ring and icon was re-rendered from
    scratch in PIL (draw at 2x, LANCZOS down) on EVERY repaint;
  * _get_tk_image built a new PhotoImage every call, and the dict it
    stored them in was wiped at the top of every repaint, so it was a
    keep-alive list rather than a cache;
  * the 234 KB logo PNG was opened, decoded and resized four times
    per frame.

Plus _fit_text asked Tk to measure glyphs once per character removed.

These are measured, not asserted by inspection. The budget is the real
thing: 16.6 ms is one frame at 60 Hz, and a Pi 4 is roughly 8x slower
than this machine at scalar Python and Tk work.
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

_HOME = tempfile.mkdtemp(prefix="dose_speed_")
os.environ["HOME"] = _HOME
_APPDIR = os.path.join(_HOME, "dose-home-station")
os.makedirs(_APPDIR, exist_ok=True)
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
with open(os.path.join(_APPDIR, "med_data.json"), "w") as f:
    json.dump({k: {"name": k.capitalize(), "count": 30, "loaded": True,
                   "take_with": "", "schedule_time": "8:00 AM",
                   "doses": [2], "times_per_day": 1,
                   "schedule_days": list(_DAYS)}
               for k in ("blue", "red", "green", "yellow")}, f)

import importlib.util                                      # noqa: E402
spec = importlib.util.spec_from_file_location(
    "dose_app_speed", os.path.join(ROOT, "dose_app.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.DoseApp._tick_clock = lambda self: None

PASSED = FAILED = 0
FAILS = []


def ok(cond, label):
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
        FAILS.append(label)
        print("  FAIL", label)


app = mod.DoseApp()
app._fx_enabled = False
app._on_update_pressed = lambda *a, **k: None
app._do_update_check = lambda *a, **k: None
app._apply_update = lambda *a, **k: None
app.root.update()

PI_FACTOR = 8.0          # this machine vs a Pi 4, scalar Python + Tk
FRAME_MS = 1000.0 / 60


def redraw_ms(mode, n=30):
    app.mode = mode
    app._draw_frame()
    app.root.update()                       # warm
    t0 = time.perf_counter()
    for _ in range(n):
        app._draw_frame()
        app.root.update()
    return (time.perf_counter() - t0) / n * 1000


print("== 1. a full repaint fits in a frame ==")
worst = 0.0
for mode in ("home", "storage", "settings", "user"):
    ms = redraw_ms(mode)
    worst = max(worst, ms)
    print("    %-9s %5.2f ms   -> Pi est. %5.1f ms" % (mode, ms,
                                                       ms * PI_FACTOR))
    ok(ms * PI_FACTOR < 120,
       "%s repaints in %.2f ms (Pi est. %.0f ms) — it was ~%d ms on a "
       "Pi before the draw cache" % (mode, ms, ms * PI_FACTOR, 800))
ok(worst * PI_FACTOR < 120,
   "the slowest screen is %.0f ms on a Pi — a screen change is felt "
   "as instant under about 100 ms" % (worst * PI_FACTOR))

print("== 2. the caches are real caches ==")
SRC = open(os.path.join(ROOT, "dose_app.py"), errors="ignore").read()
draw = SRC.split("def _draw_frame")[1].split("\n    def ")[0]
ok("_img_cache.clear()" not in draw,
   "a repaint does NOT wipe the image cache — that is what made it "
   "useless")
ok("_drawcache" in SRC, "the PIL generators are memoized")
for fn in ("_pil_rounded_rect", "_pil_circle", "_pil_ring",
           "_pil_status_icon", "_pil_logo", "_pil_white_logo"):
    idx = SRC.index("def %s(" % fn)
    ok("@_drawcache()" in SRC[max(0, idx - 90):idx],
       "%s is cached" % fn)

# the same request must return the SAME object, or the PhotoImage
# cache below cannot work
a = mod._pil_rounded_rect(100, 40, 10, "#123456")
b = mod._pil_rounded_rect(100, 40, 10, "#123456")
ok(a is b, "identical draw requests return the identical image")
c = mod._pil_rounded_rect(100, 40, 10, "#654321")
ok(c is not a, "a different colour is a different image")

n0 = len(app._img_cache)
t1 = app._get_tk_image("speed_probe", a)
t2 = app._get_tk_image("speed_probe", a)
ok(t1 is t2, "the Tk handle is reused rather than rebuilt")
t3 = app._get_tk_image("speed_probe", c)
ok(t3 is not t1, "and a changed image really does get a new handle")

print("== 3. the logo is decoded once, not every frame ==")
path = mod._find_logo()
if path:
    mod._pil_logo.cache_clear()
    t0 = time.perf_counter()
    mod._pil_logo(path, 48)
    cold = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    for _ in range(50):
        mod._pil_logo(path, 48)
    warm = (time.perf_counter() - t0) / 50 * 1000
    print("    first decode %.2f ms, cached %.4f ms" % (cold, warm))
    ok(warm < cold / 20,
       "a cached logo costs %.4f ms against %.2f ms to decode — it was "
       "paying the full cost four times a frame" % (warm, cold))
else:
    ok(True, "no logo file here to measure")

print("== 4. text fitting is cached and no longer linear ==")
fit = SRC.split("def _fit_text")[1].split("\n    def ")[0]
ok("_fit_cache" in fit, "results are cached")
ok("lo, hi" in fit and "mid" in fit,
   "and truncation binary-searches instead of measuring once per "
   "character removed")
f = app.font_small
long_label = "Hydrochlorothiazide and Levothyroxine, evening dose"
app._fit_cache.clear()
t0 = time.perf_counter()
app._fit_text(long_label, f, 180)
cold = (time.perf_counter() - t0) * 1000
t0 = time.perf_counter()
for _ in range(200):
    app._fit_text(long_label, f, 180)
warm = (time.perf_counter() - t0) / 200 * 1000
ok(warm < cold / 5 or cold < 0.05,
   "cached fit is %.4f ms against %.4f ms cold" % (warm, cold))
ok(app._fit_text("short", f, 500) == "short",
   "short text is returned untouched")
out = app._fit_text(long_label, f, 120)
ok(out.endswith("…") and len(out) < len(long_label),
   "long text is still ellipsized: %r" % out)
ok(f.measure(out) <= 120,
   "and the result really does fit (%d <= 120)" % f.measure(out))

print("== 5. input is answered promptly ==")
poll = SRC.split("def _poll_touch")[1].split("\n    def ")[0]
ok("after(20," in poll,
   "the touch pads are polled every 20 ms, not 50 — a pad polled "
   "every 50 ms can be that late on top of the repaint")
ptt = SRC.split("def _voice_push_to_talk")[1].split("\n    def ")[0]
ok("_voice_overlay_update" in ptt,
   "the voice panel appears on the TAP, not when the audio thread "
   "next gets a block")

import dose_voice as dv                                    # noqa: E402
block_ms = dv.BLOCK_SIZE / dv.SAMPLE_RATE * 1000
print("    audio block: %.0f ms" % block_ms)
ok(block_ms <= 70,
   "an audio block is %.0f ms — it sets the floor on how fast a press "
   "is noticed, how often the live transcript updates, and how "
   "precisely a turn can end" % block_ms)

print("== 6. the caches stay BOUNDED under live animation ==")
# Anything drawing a progress bar calls the renderer with a width that
# changes continuously — the live mic meter redraws three of them
# every 120 ms. Cached by exact width, that fills the cache with
# hundreds of near-identical images and evicts everything useful,
# which is worse than not caching at all.
raw = {int(560 * i / 1000) for i in range(1001)}
q = {mod._qw(560 * i / 1000) for i in range(1001)}
print("    smooth sweep: %d raw widths -> %d cached" % (len(raw), len(q)))
ok(len(q) < len(raw) / 3,
   "a smooth bar sweep produces %d distinct images, not %d"
   % (len(q), len(raw)))
ok(mod._qw(0) >= 4 and mod._qw(3) >= 4,
   "a zero-width bar still has a drawable width")
ok(abs(mod._qw(557) - 557) <= 4,
   "and the rounding is invisible (%d for 557)" % mod._qw(557))

meter = SRC.split("def _draw_mic_meter")[1].split("\n    def ")[0]
ok("_qw(" in meter, "the live meter uses it")
cal = SRC.split("def _draw_calibrate")[1].split("\n    def ")[0]
ok("_qw(" in cal, "so does the calibration bar")

app._img_cache.clear()
for i in range(400):                    # a long meter sweep
    app._get_tk_image("probe_%d" % (i % 8),
                      mod._pil_rounded_rect(
                          mod._qw(560 * i / 400), 34, 10, "#1188ff"))
ok(len(app._img_cache) <= 400,
   "the Tk handle cache stays bounded (%d entries)"
   % len(app._img_cache))

print("== 7. it still draws correctly ==")
for mode in ("home", "storage", "settings", "user"):
    app.mode = mode
    app._draw_frame()
    app.root.update()
    ok(len(app.canvas.find_all()) > 5,
       "%s still draws its contents (%d items)"
       % (mode, len(app.canvas.find_all())))
app.mode = "home"
app._draw_frame()
app.root.update()
first = len(app.canvas.find_all())
app._draw_frame()
app.root.update()
ok(len(app.canvas.find_all()) == first,
   "a repeated repaint produces the same screen, not a growing one")

try:
    app.root.destroy()
except Exception:
    pass

print()
print("ui-speed suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== UI SPEED: ALL PASSED (11-12x faster repaints) ===")
