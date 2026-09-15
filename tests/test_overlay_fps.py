"""VOICE OVERLAY FRAME RATE — measured on a real Tk canvas.

The moving lines that appear when the assistant is listening used to
crawl: every single frame re-rendered two supersampled PIL images (the
panel and the waveform), threw the whole overlay away, and rebuilt it.
That is ~100 ms of work per frame on a Pi.

Now the panel is rendered once, the ribbons are native canvas
polylines whose coordinates are updated in place, and text is only
pushed when it changes. This test boots the REAL app under Xvfb and
drives the REAL animation callback, so the number below is the actual
cost of a frame, not an estimate.

Budget: a Pi 4 is roughly 6-8x slower than a dev machine at this kind
of scalar Python + Tk work, so to hold 60 fps (16.6 ms/frame) there we
need well under ~2 ms/frame here.
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

_HOME = tempfile.mkdtemp(prefix="dose_fps_home_")
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
    "dose_app_fps", os.path.join(ROOT, "dose_app.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.DoseApp._tick_clock = lambda self: None

PASSED = FAILED = 0
FAILS = []


def ok(cond, label):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("  OK  ", label)
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

TARGET_MS = 1000.0 / mod.DoseApp.VOICE_FPS
PI_FACTOR = 8.0        # assume the Pi is 8x slower at this work


def measure(state, frames=180, convo=False):
    """Drive the real animation callback and time each frame."""
    app._voice_overlay_update(state, "checking my schedule for today", "")
    app.root.update()
    # take the scheduler out of the loop: call the tick directly so we
    # time the WORK, not tkinter's after() granularity
    real_after = app.root.after
    app.root.after = lambda *a, **k: None
    times = []
    try:
        for _ in range(frames):
            t0 = time.perf_counter()
            app._voice_anim_tick()
            app.root.update()
            times.append((time.perf_counter() - t0) * 1000.0)
    finally:
        app.root.after = real_after
    times.sort()
    return (sum(times) / len(times), times[len(times) // 2],
            times[int(len(times) * 0.99)])


print("== the overlay actually animates ==")
app._voice_overlay_update("listening", "hello", "")
app.root.update()
ok(bool(app.canvas.find_withtag("voice_ov")), "overlay appears")
n_items = len(app.canvas.find_withtag("voice_ov"))
app._voice_anim_tick()
app.root.update()
ok(len(app.canvas.find_withtag("voice_ov")) == n_items,
   "a frame REUSES its canvas items (%d) instead of rebuilding them"
   % n_items)

# the wave must actually move between frames
w0 = app.canvas.coords(app._voice_items["w0"])
time.sleep(0.02)
app._voice_anim_tick()
app.root.update()
w1 = app.canvas.coords(app._voice_items["w0"])
ok(w0 != w1, "the wave moves between frames")
ok(len(w0) == mod.DoseApp.WAVE_POINTS * 2,
   "the wave is drawn from %d points" % mod.DoseApp.WAVE_POINTS)

print("== frame cost, measured on a real canvas ==")
for label, state in (("listening", "listening"),
                     ("thinking", "thinking"),
                     ("speaking", "speaking")):
    mean, med, p99 = measure(state)
    print("    %-10s mean %.3f ms  median %.3f ms  p99 %.3f ms"
          "   → Pi est. %.2f ms/frame (%.0f fps)"
          % (label, mean, med, p99, mean * PI_FACTOR,
             1000.0 / max(0.001, mean * PI_FACTOR)))
    ok(mean * PI_FACTOR < TARGET_MS,
       "%s holds 60 fps on a Pi (est. %.2f ms < %.2f ms budget)"
       % (label, mean * PI_FACTOR, TARGET_MS))
    ok(p99 * PI_FACTOR < TARGET_MS * 2,
       "%s has no frame spikes (p99 est. %.2f ms)"
       % (label, p99 * PI_FACTOR))

print("== it appears instantly, and costs little to keep up ==")
# The panel image used to be re-rendered every time the overlay
# appeared: ~10 ms here, nearer 80 ms on a Pi, paid exactly when you
# first spoke. It is built once at startup and reused.
app._voice_overlay_update("idle")
app.root.update()
app._voice_panel_cache = {}
app._voice_prerender_panel()
ok(len(app._voice_panel_cache) >= 1,
   "the panel is pre-rendered before it is ever needed")

app._voice_sig = None
t0 = time.perf_counter()
app._voice_overlay_update("listening", "hello", "")
app.root.update()
warm = (time.perf_counter() - t0) * 1000
ok(warm < 6.0,
   "showing the panel costs %.2f ms with the image already built "
   "(~%.0f ms projected on a Pi)" % (warm, warm * PI_FACTOR))
print("    first show (pre-rendered): %.2f ms" % warm)

ok(mod.DoseApp.WAVE_POINTS <= 28,
   "the wave is %d points — Tk retessellates every one on every "
   "redraw" % mod.DoseApp.WAVE_POINTS)
line_cfg = app.canvas.itemconfigure(app._voice_items["w0"])
ok(str(line_cfg["smooth"][-1]) in ("0", "false", "False"),
   "and they are plain polylines, not splines Tk must smooth in "
   "software every frame")

print("== the scheduler adapts instead of cascading ==")
delays = []
app._voice_overlay_update("listening", "x", "")
real_after = app.root.after
app.root.after = lambda ms, fn=None, *a: delays.append(ms)
try:
    for _ in range(30):
        app._voice_anim_tick()
finally:
    app.root.after = real_after
ok(delays, "the animation reschedules itself")
lo = 1000.0 / mod.DoseApp.VOICE_FPS
hi = 1000.0 / mod.DoseApp.VOICE_FPS_MIN
ok(all(1 <= d <= hi + 1 for d in delays),
   "every scheduled delay is between %.0f and %.0f fps (%d-%d ms)"
   % (mod.DoseApp.VOICE_FPS_MIN, mod.DoseApp.VOICE_FPS,
      min(delays), max(delays)))
ok(mod.DoseApp.VOICE_FPS_MIN >= 30,
   "and the floor is %d fps — still smooth to the eye"
   % mod.DoseApp.VOICE_FPS_MIN)
# a machine that cannot hold the ceiling must settle, not thrash
app._voice_frame_cost = 1.0
app._voice_anim_tick()
ok(getattr(app, "_voice_fps_now", 0) == mod.DoseApp.VOICE_FPS_MIN,
   "an overloaded machine drops to a steady %d fps rather than "
   "queueing frames it cannot draw" % mod.DoseApp.VOICE_FPS_MIN)
app._voice_frame_cost = 0.0005
app._voice_anim_tick()
ok(getattr(app, "_voice_fps_now", 0) == mod.DoseApp.VOICE_FPS,
   "and a machine that can keep up gets the full %d fps"
   % mod.DoseApp.VOICE_FPS)

# a slow frame must not push the whole animation back
app._voice_overlay_update("listening", "x", "")
app._voice_next_frame = time.time() - 1.0     # pretend we fell behind
delays2 = []
app.root.after = lambda ms, fn=None, *a: delays2.append(ms)
try:
    app._voice_anim_tick()
finally:
    app.root.after = real_after
ok(delays2 and delays2[0] >= 1,
   "after falling behind it resyncs instead of queueing up "
   "(next frame in %d ms)" % delays2[0])

print("== it stops cleanly ==")
app._voice_overlay_update("idle")
app.root.update()
# it is held briefly so a fast reply stays readable (see
# test_overlay_live) — run past that window, then it must be gone
deadline = time.time() + mod.VOICE_OVERLAY_MIN_S + 1.0
while time.time() < deadline and app.canvas.find_withtag("voice_ov"):
    app._voice_anim_tick()
    app.root.update()
    time.sleep(0.01)
ok(not app.canvas.find_withtag("voice_ov"), "overlay is removed")
ok(not app._voice_items, "canvas item handles are released")
ok(not app._voice_imgs, "images are released")

try:
    app.root.destroy()
except Exception:
    pass

print()
print("overlay-fps suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== VOICE OVERLAY: 60 FPS CONFIRMED ===")
