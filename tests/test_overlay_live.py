"""THE VOICE PANEL MUST ACTUALLY BE VISIBLE.

Two ways it stopped being visible, both reproduced here against a real
Tk canvas rather than by reading the source:

  * A full repaint (_draw_frame) calls canvas.delete("all"), which
    takes the overlay's items with it. Tk then accepts coords() on
    those dead ids SILENTLY — no exception — so the animation carried
    on at 60 fps moving things that no longer existed and the panel
    simply disappeared. Anything that repainted mid-sentence did this;
    a QR presence change was the common trigger, which is why it
    showed up at the same time as the scanner flapping.

  * Replies got fast enough that a short exchange ("hello") was over
    before the panel had finished sliding up, so all you saw was a
    flash of the blue wave at the bottom of the screen.
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

_HOME = tempfile.mkdtemp(prefix="dose_ovl_home_")
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
    "dose_app_ovl", os.path.join(ROOT, "dose_app.py"))
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

SCREEN_H = mod.SCREEN_H
SCREEN_W = mod.SCREEN_W


def items():
    return app.canvas.find_withtag("voice_ov")


def panel_box():
    """Bounding box of the whole overlay, as drawn."""
    ids = items()
    if not ids:
        return None
    boxes = [app.canvas.bbox(i) for i in ids]
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def settle(seconds=0.45):
    """Run the animation for a while, like the real event loop."""
    end = time.time() + seconds
    while time.time() < end:
        app._voice_anim_tick()
        app.root.update()
        time.sleep(0.01)


print("== 1. the panel appears, as a PANEL ==")
app._voice_overlay_update("listening", "hello", "")
settle(0.4)
ok(len(items()) >= 4,
   "the overlay has all its pieces (%d items)" % len(items()))
box = panel_box()
ok(box is not None, "the overlay has a drawn area")
w, h = box[2] - box[0], box[3] - box[1]
ok(w > 400, "it is a full-width panel, not a line (%d px wide)" % w)
ok(h > 60, "it is a PANEL, not a line (%d px tall)" % h)
ok(box[3] <= SCREEN_H + 2 and box[1] >= 0,
   "and it is fully on screen (top %d, bottom %d of %d)"
   % (box[1], box[3], SCREEN_H))
ok(box[0] >= 0 and box[2] <= SCREEN_W + 2,
   "horizontally on screen too (%d..%d of %d)"
   % (box[0], box[2], SCREEN_W))

print("== 2. a full repaint must not silently kill it ==")
# This is the exact failure: _draw_frame wipes the canvas mid-sentence.
app._draw_frame()          # no root.update() yet — raw post-wipe state
ok(not items(), "a repaint wipes the overlay's canvas items...")
settle(0.1)
ok(len(items()) >= 4,
   "...and the very next frame rebuilds it (%d items)" % len(items()))
box2 = panel_box()
ok(box2 is not None and (box2[2] - box2[0]) > 400,
   "the rebuilt panel is a full panel again")

# repeated repaints — what the QR flapping was doing
for _ in range(10):
    app._draw_frame()
    app.root.update()
    settle(0.05)
ok(len(items()) >= 4,
   "it survives repeated repaints (%d items)" % len(items()))
ok((panel_box()[2] - panel_box()[0]) > 400, "and is still a panel")

print("== 3. a fast reply is still readable ==")
ok(mod.VOICE_OVERLAY_MIN_S >= 1.2,
   "the panel is held for at least %.1f s" % mod.VOICE_OVERLAY_MIN_S)
app._voice_overlay_update("idle")
app.root.update()
app._voice_overlay_update("listening", "hello", "")
settle(0.15)
t0 = time.time()
app._voice_overlay_update("speaking", "", "Hello Ryan.")
app._voice_overlay_update("idle")          # reply finished instantly
settle(0.3)
ok(len(items()) >= 4,
   "the panel is STILL up right after an instant reply (%d items)"
   % len(items()))
# hold it out
while time.time() - t0 < mod.VOICE_OVERLAY_MIN_S + 0.4:
    app._voice_anim_tick()
    app.root.update()
    time.sleep(0.01)
ok(not items(), "and it retires once it has been readable")
ok(not app._voice_items, "cleanly, releasing its canvas handles")
ok(not app._voice_imgs, "and its images")

print("== 4. it shows BOTH what was heard and the answer ==")
app._voice_overlay_update("idle")
settle(0.05)
end = time.time() + mod.VOICE_OVERLAY_MIN_S + 0.5
while time.time() < end and items():
    app._voice_anim_tick()
    app.root.update()
    time.sleep(0.01)

app._voice_overlay_update("listening", "how many sertraline do i have left", "")
settle(0.3)
top = app.canvas.itemcget(app._voice_items["top"], "text")
ok("sertraline" in top.lower(),
   "while listening, the live transcript is shown: %r" % top[:48])

app._voice_overlay_update(
    "speaking", "how many sertraline do i have left",
    "Sertraline: 28 pills remaining.")
settle(0.3)
top = app.canvas.itemcget(app._voice_items["top"], "text")
big = app.canvas.itemcget(app._voice_items["big"], "text")
ok("sertraline" in top.lower(),
   "while answering, what you SAID is still on screen: %r" % top[:48])
ok("28 pills" in big,
   "and the answer is shown underneath it: %r" % big[:48])
ok(top != big, "they are two different lines")

# nothing may overlap the waveform
tb = app.canvas.bbox(app._voice_items["top"])
bb = app.canvas.bbox(app._voice_items["big"])
wb = app.canvas.bbox(app._voice_items["w0"])
ok(tb[3] <= bb[1] + 4, "the two lines do not overlap each other")
ok(bb[3] <= wb[1] + 6,
   "and the answer does not run into the waveform "
   "(text ends %d, wave starts %d)" % (bb[3], wb[1]))
box = panel_box()
ok(box[3] <= SCREEN_H, "the whole panel is still on screen (%d <= %d)"
   % (box[3], SCREEN_H))

# a long reply must not spill out either
app._voice_overlay_update(
    "speaking", "what do i take today",
    "Sertraline at 8:00 AM, Atorvastatin at 9:00 PM, Metformin at "
    "12:00 PM and Levothyroxine at 7:00 AM, Ryan.")
settle(0.3)
bb = app.canvas.bbox(app._voice_items["big"])
wb = app.canvas.bbox(app._voice_items["w0"])
ok(bb[3] <= wb[1] + 6,
   "a long answer is fitted, not spilled over the wave "
   "(ends %d, wave starts %d)" % (bb[3], wb[1]))
ok(panel_box()[3] <= SCREEN_H, "and stays on screen")

print("== 5. the wave animates while it is up ==")
app._voice_overlay_update("listening", "testing", "")
settle(0.1)
a = app.canvas.coords(app._voice_items["w0"])
settle(0.08)
b = app.canvas.coords(app._voice_items["w0"])
ok(a != b, "the wave is moving")
ok(len(a) == mod.DoseApp.WAVE_POINTS * 2,
   "drawn from %d points" % mod.DoseApp.WAVE_POINTS)

print("== 6. it never lingers forever ==")
app._voice_overlay_update("idle")
end = time.time() + mod.VOICE_OVERLAY_MIN_S + 1.0
while time.time() < end and items():
    app._voice_anim_tick()
    app.root.update()
    time.sleep(0.01)
ok(not items(), "idle always ends with the overlay gone")
ok(not app._voice_anim_running, "and the animation loop stopped")

try:
    app.root.destroy()
except Exception:
    pass

print()
print("overlay-live suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== VOICE PANEL: VISIBLE, SURVIVES REPAINTS, READABLE ===")
