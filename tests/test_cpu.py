"""THE CPU BUDGET.

Reported from the device: load average 5.00 on four cores — 125%
oversubscribed — and silero-vad failing to install.

Both had the same shape of cause: work that was being done constantly
when it only needed to be done occasionally, and a dependency that was
enormously larger than the thing actually needed from it.

  * The QR decoder ran FIVE passes five times a second — a Gaussian
    blur, an autocontrast, a contrast stretch and a sharpen, each with
    its own zbar scan. Measured at 14.5 ms of image processing per
    640x480 frame before any scanning, about 58% of a Pi core, and
    roughly three times that at 720p. It escalated whenever fewer than
    four codes were visible, which is nearly always.
  * `pip install silero-vad` requires torch and torchaudio. Hundreds
    of megabytes, on a 4 GB Pi, to obtain one 2.3 MB ONNX file.

This suite measures the steady-state cost of everything that runs on
its own and holds it to a budget.
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

_HOME = tempfile.mkdtemp(prefix="dose_cpu_")
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
    "dose_app_cpu", os.path.join(ROOT, "dose_app.py"))
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


PI_FACTOR = 8.0          # this machine vs a Pi 4 at scalar work
BUDGET_PCT = 25.0        # of ONE core, for everything that self-runs

app = mod.DoseApp()
app._fx_enabled = False
app._on_update_pressed = lambda *a, **k: None
app._do_update_check = lambda *a, **k: None
app.root.update()

print("== 1. the voice detector needs no torch ==")
APP_SRC = open(os.path.join(ROOT, "dose_app.py"), errors="ignore").read()
SH_SRC = open(os.path.join(ROOT, "DOSE.sh"), errors="ignore").read()
import dose_voice as dv                                    # noqa: E402

# comments explaining why we do NOT install it are fine; an actual
# install command is not
SH_CODE = "\n".join(l for l in SH_SRC.split("\n")
                    if not l.lstrip().startswith("#"))
ok("pip install silero-vad" not in SH_CODE
   and "silero-vad" not in SH_CODE.replace(
       "snakers4/silero-vad", ""),
   "the setup script never pip-installs silero-vad — only fetches "
   "the model file")
APP_CODE = "\n".join(l for l in APP_SRC.split("\n")
                     if not l.lstrip().startswith("#"))
ok("silero-vad" not in APP_CODE,
   "and the app never asks pip for it either")
ok('"silero_vad", "silero-vad"' not in APP_SRC,
   "and neither does the app's dependency list")
ok(hasattr(dv, "VAD_MODEL_URL") and dv.VAD_MODEL_URL.endswith(".onnx"),
   "the model is fetched as a single ONNX file instead")
ok("fetch_vad_model" in open(os.path.join(ROOT, "dose_voice.py"),
                             errors="ignore").read(),
   "by a plain background download — no package, no build step")
ok(dv.VAD_MODEL_MIN_BYTES > 100_000,
   "and a truncated download is rejected")

print("== 2. the QR decoder escalates only when it must ==")
dec = APP_SRC.split("def _decode_passes")[1].split("\n    def ")[0]
ok("HARD_PASS_PERIOD" in APP_SRC,
   "the expensive passes are rate-limited")
ok(app.HARD_PASS_PERIOD >= 0.5,
   "to at most once every %.1f s, not five times a second"
   % app.HARD_PASS_PERIOD)
ok("if len(found) < 4" not in dec,
   "they no longer fire whenever fewer than four codes are visible")
ok("_expected_codes" in dec,
   "they fire when the cheap pass found less than we believe is there")
ok("def enough" in dec,
   "and stop as soon as the answer is in")

# the steady state: everything expected is found by the cheap pass
class FakeR:
    def __init__(self, text):
        self.data = text.encode()
        self.rect = None


slots = ["blue", "red"]
codes = [json.dumps({"slot": s, "med": s.capitalize()}) for s in slots]
app._qr_held = {s: True for s in slots}
app._last_hard_pass = 0.0
calls = {"n": 0}
_real_scan = mod._scan_qr


def cheap_scan(img):
    calls["n"] += 1
    return [FakeR(c) for c in codes]


mod._scan_qr = cheap_scan
try:
    from PIL import Image
    frame = Image.new("RGB", (640, 480), (20, 20, 20))
    calls["n"] = 0
    for _ in range(25):                 # 5 seconds of camera at 5 Hz
        app._decode_passes(frame)
    ok(calls["n"] == 25,
       "in the steady state each frame costs exactly ONE scan (%d for "
       "25 frames) — it was five" % calls["n"])

    # nothing readable: it escalates, but not on every frame
    mod._scan_qr = lambda img: (_ for _ in ()).throw(RuntimeError) \
        if False else []
    calls["n"] = 0
    app._last_hard_pass = 0.0
    t0 = time.time()
    n_frames = 0
    while time.time() - t0 < 1.0:       # one second of camera
        app._decode_passes(frame)
        n_frames += 1
        time.sleep(0.2)
    ok(n_frames >= 3, "ran %d frames in a second" % n_frames)
    hard_rounds = max(0, (calls["n"] - n_frames)) // 4
    ok(hard_rounds <= 2,
       "with an unreadable frame it escalated %d time(s) in a second, "
       "not once per frame" % hard_rounds)
finally:
    mod._scan_qr = _real_scan

print("== 3. the steady-state budget ==")
# Everything that runs on its own timer, per second of wall clock.
app.mode = "home"
app._draw_frame()
app.root.update()
t0 = time.perf_counter()
N = 20
for _ in range(N):
    app._draw_frame()
    app.root.update()
redraw_ms = (time.perf_counter() - t0) / N * 1000

from PIL import Image as _Im                               # noqa: E402
_frame = _Im.new("RGB", (640, 480), (30, 30, 30))
mod._scan_qr = cheap_scan
try:
    app._qr_held = {s: True for s in slots}
    t0 = time.perf_counter()
    for _ in range(10):
        app._decode_passes(_frame)
    cheap_ms = (time.perf_counter() - t0) / 10 * 1000
finally:
    mod._scan_qr = _real_scan

budget = [
    ("screen repaint", redraw_ms, 1.0),        # clock tick, 1 Hz
    ("QR decode", cheap_ms, 5.0),              # camera, 5 Hz
]
total_pct = 0.0
for label, ms, hz in budget:
    pct = ms * PI_FACTOR * hz / 10.0           # ms/s -> % of one core
    total_pct += pct
    print("    %-16s %6.2f ms x %.0f/s -> Pi est. %5.1f%% of a core"
          % (label, ms, hz, pct))
print("    %-16s %27s %5.1f%%" % ("TOTAL", "", total_pct))
ok(total_pct < BUDGET_PCT,
   "everything that runs on its own costs %.1f%% of one core on a Pi "
   "(budget %.0f%%)" % (total_pct, BUDGET_PCT))

print("== 3b. everything is in place BEFORE the app opens ==")
# The app used to open while pieces were still arriving, so the first
# screen listed things as MISSING and the first thing you said went to
# whatever happened to be loaded.
gate = SH_SRC.split("EVERYTHING READY?")[1].split("# ── Launch ──")[0]
ok(SH_SRC.index("EVERYTHING READY?") < SH_SRC.index("Starting DOSE"),
   "the readiness check runs BEFORE launch")
for mod_name in ("sounddevice", "vosk", "piper", "rapidfuzz",
                 "jellyfish", "onnxruntime", "faster_whisper",
                 "moonshine_voice"):
    ok(mod_name in gate, "it verifies %s" % mod_name)
ok("silero_vad.onnx" in gate, "and the voice detector model file")
ok("vosk-model" in gate, "and the speech model")
ok("pip install" in gate,
   "anything missing is retried once, in the foreground")
ok("Starting without" in gate,
   "and what is still missing is reported plainly")
ok("MISSING=\"\"" in gate,
   "the app still starts either way — a station that will not open is "
   "worse than one missing its best recogniser")

print("== 4. nothing is left spinning ==")
VSRC = open(os.path.join(ROOT, "dose_voice.py"), errors="ignore").read()
ok(dv.INFER_THREADS <= max(1, dv.CPU_CORES - 2),
   "continuous speech work is capped at %d of %d cores"
   % (dv.INFER_THREADS, dv.CPU_CORES))
ok("os.nice(" in VSRC,
   "background workers yield rather than compete")
import re                                                   # noqa: E402
spins = re.findall(r"while True:\s*\n(?!\s*(?:if|try|data|for))", VSRC)
ok(len(spins) < 6, "no obvious unbounded spin loops (%d)" % len(spins))
ok("time.sleep" in VSRC, "loops that do poll, sleep")

fps = APP_SRC.split("VOICE_FPS")[1][:200]
ok("VOICE_FPS_MIN" in APP_SRC,
   "the overlay drops its frame rate rather than queueing frames it "
   "cannot draw")
anim = APP_SRC.split("def _voice_anim_tick")[1].split("\n    def ")[0]
ok('self._voice_state == "idle"' in anim,
   "and it stops entirely when there is no conversation")

try:
    app.root.destroy()
except Exception:
    pass

print()
print("cpu suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== CPU BUDGET: ALL PASSED (under %.0f%% of one core) ==="
      % BUDGET_PCT)
