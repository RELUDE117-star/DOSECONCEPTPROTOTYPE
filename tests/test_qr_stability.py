"""THE SCANNER MUST NOT INVENT REMOVALS.

Symptom: "medicine placed / medicine removed" over and over while
nobody had touched a bottle.

Cause: presence was decided purely on ELAPSED TIME — a slot counted as
present if its code had been decoded within the last 2.5 s. That
cannot tell "the bottle is gone" apart from "the camera thread didn't
get to run". The camera decodes in its own thread every ~0.2 s, and
when the Pi got busy those passes were starved, the window lapsed, and
the station announced a removal followed by a replacement. Speech
recognition during a conversation was the obvious way to get busy.

Fix, checked here against the real methods:
  * presence is counted in COMPLETED SCAN PASSES, not seconds
  * if the camera is stalled or stopped, the last known answer is
    held — a busy CPU is never reported as someone taking their
    medication out
  * speech is confined to fewer cores than the machine has, so the
    camera always has one
"""
import os
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

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


sys.modules.setdefault("tkinter", types.ModuleType("tkinter"))
sys.modules["tkinter"].font = types.ModuleType("tkinter.font")
sys.modules.setdefault("tkinter.font", sys.modules["tkinter"].font)
import importlib.util                                      # noqa: E402

spec = importlib.util.spec_from_file_location(
    "dose_app_qr", os.path.join(ROOT, "dose_app.py"))
da = importlib.util.module_from_spec(spec)
spec.loader.exec_module(da)
import dose_voice as dv                                    # noqa: E402

SLOTS = da.SLOT_KEYS


class FakeStation:
    """The real presence methods, driven by a scripted camera."""

    def __init__(self):
        self.camera_running = True
        self.qr_last_seen = {k: 0.0 for k in SLOTS}
        self.qr_last_seen["demo"] = 0.0
        self.qr_x_pos = {k: 0 for k in SLOTS}
        self.qr_x_pos["demo"] = 0
        self._qr_miss = {k: da.QR_MISS_LIMIT for k in SLOTS}
        self._qr_miss["demo"] = da.QR_MISS_LIMIT
        self._qr_held = {k: False for k in SLOTS}
        self._qr_held["demo"] = False
        self._qr_last_scan = 0.0
        self._prev_qr_present = {k: False for k in SLOTS}
        self._prev_qr_present["demo"] = False
        self.dispense_state = 0
        self.med_data = {k: {"name": k, "loaded": True, "count": 30}
                         for k in SLOTS}
        self.med_data["demo"] = {"name": "demo", "loaded": False,
                                 "count": 0}
        self.events = []
        self.mode = "home"
        self._demo_registered = True
        self._qty_cancel_time = 0
        self._addmed_cancel_time = 0

    # real methods under test
    _is_qr_present = da.DoseApp._is_qr_present
    _check_presence_changes = da.DoseApp._check_presence_changes
    _handle_qr_results = da.DoseApp._handle_qr_results
    _parse_qr_payload = da.DoseApp._parse_qr_payload

    def _is_loaded(self, k):
        return self.med_data.get(k, {}).get("loaded", False)

    def _animate_pill_bottle(self, key, direction):
        self.events.append((key, direction))

    def _save_med(self):
        pass

    def _show_qty_confirm(self, *a):
        pass

    def _draw_frame(self):
        pass


import json as _json                                        # noqa: E402


def payload(slot):
    return _json.dumps({"slot": slot, "med": slot.capitalize()})


def scan(st, present_slots):
    """One completed camera pass."""
    st._handle_qr_results([(0, payload(s)) for s in present_slots])
    st._check_presence_changes()


print("== 1. a bottle sitting still is never announced ==")
st = FakeStation()
for _ in range(60):          # 12 s of scanning at 0.2 s
    scan(st, ["blue"])
    time.sleep(0.002)
placed = [e for e in st.events if e == ("blue", "down")]
removed = [e for e in st.events if e == ("blue", "up")]
ok(len(placed) == 1, "announced placed exactly once (got %d)" % len(placed))
ok(len(removed) == 0, "never announced removed (got %d)" % len(removed))
ok(st._is_qr_present("blue"), "and it reads as present")

print("== 2. an occasional missed decode is not a removal ==")
# a glossy sticker / a hand passing over: a few passes miss
st = FakeStation()
for i in range(80):
    seen = [] if i % 7 == 0 else ["blue"]        # ~1 in 7 passes misses
    scan(st, seen)
ok([e for e in st.events].count(("blue", "up")) == 0,
   "scattered misses never announce a removal")
ok(st._is_qr_present("blue"), "still present throughout")

print("== 3. a REAL removal is still reported, promptly ==")
st = FakeStation()
for _ in range(20):
    scan(st, ["blue"])
ok(st._is_qr_present("blue"), "present to begin with")
passes = 0
while st._is_qr_present("blue") and passes < 40:
    scan(st, [])                 # bottle physically gone
    passes += 1
ok(not st._is_qr_present("blue"),
   "a genuine removal is detected (after %d passes)" % passes)
ok(passes <= da.QR_MISS_LIMIT + 2,
   "and it is prompt — %d passes ~= %.1f s at the camera's cadence"
   % (passes, passes * 0.2))
ok(("blue", "up") in st.events, "the removal is announced once")
ok(len([e for e in st.events if e == ("blue", "up")]) == 1,
   "exactly once")

print("== 4. THE BUG: a starved camera is not a removal ==")
# The CPU gets busy, the camera thread stops completing passes. The
# old code would time out after 2.5 s and announce a removal.
st = FakeStation()
for _ in range(20):
    scan(st, ["blue"])
ok(st._is_qr_present("blue"), "present before the machine gets busy")
st._qr_last_scan = time.time() - 10.0        # camera starved for 10 s
ok(st._is_qr_present("blue"),
   "STILL present after 10 s with no completed scan — held, not guessed")
st._check_presence_changes()
ok(("blue", "up") not in st.events,
   "and nothing was announced while the camera was starved")
# when it recovers and the bottle is there, nothing happened at all
for _ in range(10):
    scan(st, ["blue"])
ok(len([e for e in st.events if e[0] == "blue"]) == 1,
   "after recovery the only event is the original placement")

print("== 5. a stopped camera holds state too ==")
st = FakeStation()
for _ in range(20):
    scan(st, ["blue"])
st.camera_running = False
ok(st._is_qr_present("blue"),
   "stopping the camera does not empty the unit")
st._check_presence_changes()
ok(("blue", "up") not in st.events, "and announces nothing")

print("== 6. multiple bottles are tracked independently ==")
st = FakeStation()
for _ in range(20):
    scan(st, ["blue", "red"])
ok(st._is_qr_present("blue") and st._is_qr_present("red"),
   "both present")
for _ in range(da.QR_MISS_LIMIT + 2):
    scan(st, ["blue"])           # only red removed
ok(st._is_qr_present("blue"), "the one left behind stays present")
ok(not st._is_qr_present("red"), "the one taken out is removed")
ok(("red", "up") in st.events and ("blue", "up") not in st.events,
   "only the removed one is announced")

print("== 7. bookkeeping cannot be skipped by a UI flow ==")
# _handle_qr_results can return early (it opens screens). The scan
# accounting must happen before any of that.
src = open(os.path.join(ROOT, "dose_app.py"), errors="ignore").read()
body = src.split("def _handle_qr_results")[1].split("\n    def ")[0]
first_return = body.index("\n                        return")
ok(body.index("_qr_last_scan") < first_return,
   "the pass is recorded before any early return")
ok(body.index("self._qr_miss[_k] += 1") < first_return,
   "and so are the miss counters")
cam = src.split("def _camera_loop")[1].split("\n    def ")[0]
ok("if results:" not in cam,
   "the camera reports EVERY pass, including empty ones")
ok("_handle_qr_results" in cam, "on every pass")

print("== 8. speech is not allowed to starve the camera ==")
ok(dv.INFER_THREADS <= max(1, dv.CPU_CORES - 2),
   "speech gets %d of %d cores, leaving one for the screen AND one "
   "for the camera" % (dv.INFER_THREADS, dv.CPU_CORES))
vsrc = open(os.path.join(ROOT, "dose_voice.py"), errors="ignore").read()
spec_body = vsrc.split("def speculate")[1].split("return box")[0]
ok("os.nice(5)" in spec_body,
   "speculative recognition yields to the camera")
tts_body = vsrc.split("def render_rest")[1].split("if len(chunks)")[0]
ok("os.nice(5)" in tts_body, "so does background speech synthesis")

print()
print("qr-stability suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== QR SCANNER: NO PHANTOM REMOVALS ===")
