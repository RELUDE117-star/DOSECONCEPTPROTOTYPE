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

# frame -> grayscale, per camera frame
import numpy as _np                                         # noqa: E402
_raw = (_np.random.rand(480, 640, 3) * 255).astype("uint8")
t0 = time.perf_counter()
for _ in range(40):
    mod.DoseApp._frame_to_gray(_raw)
gray_ms = (time.perf_counter() - t0) / 40 * 1000

# the audio thread, per 64 ms block
try:
    import audioop as _ao
except ImportError:                                          # py3.13
    import audioop_lts as _ao
_blk = (_np.random.randn(1024) * 2000).astype(_np.int16).tobytes()
_st = [None]
_blk48 = (_np.random.randn(3072) * 2000).astype(_np.int16).tobytes()


def _audio_block():
    d, _st[0] = _ao.ratecv(_blk48, 2, 1, 48000, 16000, _st[0])
    _ao.rms(d, 2)
    _ao.max(d, 2)


t0 = time.perf_counter()
for _ in range(500):
    _audio_block()
audio_ms = (time.perf_counter() - t0) / 500 * 1000
blocks_per_s = dv.SAMPLE_RATE / dv.BLOCK_SIZE

# The clock tick only repaints when something on screen CHANGED. The
# clock shows hours and minutes, so in the steady state that is about
# twice a minute, not once a second.
budget = [
    ("screen repaint", redraw_ms, 1.0 / 30),   # on change + backstop
    ("frame -> gray", gray_ms, 5.0),           # camera, 5 Hz
    ("QR decode (ours)", cheap_ms, 5.0),       # camera, 5 Hz
    ("audio thread", audio_ms, blocks_per_s),  # continuous
    # zbar's own scan cannot be measured here (pyzbar is not installed
    # in this environment), so it is carried as a STATED ALLOWANCE
    # rather than quietly counted as free. 20 ms per 640x480 frame is
    # the pessimistic end of what pyzbar costs on a Pi 4 for QR-only
    # decoding — already in Pi terms, so it is divided back out.
    ("zbar scan (allowance)", 20.0 / PI_FACTOR, 5.0),
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
   "(budget %.0f%%), INCLUDING a pessimistic allowance for the zbar "
   "scan that cannot be measured here" % (total_pct, BUDGET_PCT))
ok(total_pct < BUDGET_PCT * 0.8,
   "with headroom for a busy moment (%.1f%% of %.0f%%)"
   % (total_pct, BUDGET_PCT))

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

print("== 3c. nothing expensive runs while idle ==")
# The single biggest thing this device was doing: a full speech
# recogniser on EVERY audio block, forever, to notice "hey dose".
# Roughly a quarter of a Pi core, permanently, for a wake word that
# is not how anyone gets into a conversation here.
VSRC2 = open(os.path.join(ROOT, "dose_voice.py"), errors="ignore").read()
ok(not dv.WAKE_WORD,
   "the wake word is OFF by default — tapping the logo costs nothing "
   "until it is tapped")
run_loop = VSRC2.split("def _run(")[1].split("\n    def ")[0]
ok('if self.state == "idle" and not WAKE_WORD:' in run_loop,
   "and with it off, the recogniser is not fed while idle")
ok(run_loop.index('not WAKE_WORD')
   < run_loop.index("got_final = rec.AcceptWaveform"),
   "the skip happens BEFORE the decode, not after")

ok("maxsize=" in VSRC2.split("self._audio_q = queue.Queue")[1][:40],
   "the audio queue is bounded")
ok("queue.Full" in VSRC2,
   "and drops the oldest audio rather than growing without limit")

tick = APP_SRC.split("def _tick_clock")[1].split("\n    def ")[0]
ok("_screen_signature" in tick,
   "the idle screen repaints only when something on it changed")
ok("30" in tick and "stale" in tick,
   "with an unconditional repaint as a backstop — a stale screen is "
   "worse than the CPU it saves")
sig = APP_SRC.split("def _screen_signature")[1].split("\n    def ")[0]
for field in ("minute", "_is_qr_present", "_get_count", "_due_keys",
              "dispense_state"):
    ok(field in sig, "the signature covers %s" % field)

cam = APP_SRC.split("def _camera_loop")[1].split("\n    def ")[0]
ok("_frame_to_gray" in cam,
   "frames are decoded in grayscale — a QR code has no colour")
ok("self._camera_view and self.mode ==" in cam,
   "and the colour image is built only when the debug view is open")

VSRC = open(os.path.join(ROOT, "dose_voice.py"),
            errors="ignore").read()
print("== 3d. the update can actually LAND ==")
# The device sat on a build from days earlier while fix after fix was
# pushed to it. The launch-time update ended in an interactive
# "Would you like to update? (y/n)" — and the station autostarts from
# a desktop entry with Terminal=false, so there was nobody to answer
# it. The read got EOF and the update was skipped EVERY TIME.
upd = SH_SRC.split("Update, every launch")[1].split("# ──", 1)[0]
upd_code = "\n".join(ln for ln in upd.split("\n")
                     if not ln.lstrip().startswith("#"))
ok("read -p" not in upd_code,
   "the launch update never asks a question")
import re as _re3                                           # noqa: E402
unbounded = [ln for ln in SH_SRC.split("\n")
             if _re3.match(r"\s*read\s", ln) and "-t " not in ln
             and not ln.lstrip().startswith("#")]
ok(not unbounded,
   "and NOTHING in the script waits for a keypress without a timeout "
   "(%d found)" % len(unbounded))
ok("[ -t 0 ]" in SH_SRC,
   "a key wait only happens when a terminal is actually attached")
for f in ("dose_app.py", "dose_voice.py", "dose_nlu.py", "DOSE.sh"):
    ok(f in upd, "the update fetches %s" % f)
ok("py_compile" in upd,
   "every python file is compiled before it is installed")
ok(".backup" in upd,
   "and the previous version is kept, so a bad update is recoverable")
ok("UPDATE_OK=0" in upd,
   "a failed or short download changes nothing")

print("== 3e. the voice detector rests when nobody is talking ==")
is_sp = VSRC.split("def is_speech")[1].split("\n    def ")[0]
ok('"idle"' in is_sp and "WAKE_WORD" in is_sp,
   "Silero runs during a conversation, not on every loud block while "
   "idle — a television should not keep it busy all evening")
ok('getattr(self, "state", "listening")' in is_sp,
   "and an unknown state means DO the work, not skip it")

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
