"""HARDWARE-IN-THE-LOOP: the real DoseVoice engine runs against a
simulated Raspberry Pi audio system built at runtime. The system
default route (pw-record) is DEAD — perfect digital zeros, the
exact silent-mic failure seen on the real Pi — while parec is the
live USB mic with a real noise floor. Proves that route cycling
rejects the dead route by its digital silence, keeps the live one,
then hears and answers a genuinely SPOKEN "hey dose" command
through it (Piper renders the speech; Vosk must recognize it).

Needs the voice models: set DOSE_TEST_MODELDIR to a dir holding
vosk-model-small-en-us-0.15/ and an en-us-amy*.onnx voice."""
import sys, os, time, wave, tempfile, shutil, stat

MODELDIR = os.environ.get("DOSE_TEST_MODELDIR")
if not MODELDIR or not os.path.isdir(MODELDIR):
    print("SKIP: set DOSE_TEST_MODELDIR to the voice model directory")
    sys.exit(0)

WORK = tempfile.mkdtemp(prefix="dose_hw_")
BIN = os.path.join(WORK, "bin")
os.makedirs(BIN)
os.environ["DOSE_VOICE_DIR"] = MODELDIR
os.environ["PATH"] = BIN + ":" + os.environ["PATH"]
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

# ── render the spoken command with the real TTS voice ────────────
from piper import PiperVoice
import glob as _glob
onnx = sorted(_glob.glob(os.path.join(MODELDIR, "*.onnx")))[0]
pv = PiperVoice.load(onnx)
fd, wpath = tempfile.mkstemp(suffix=".wav"); os.close(fd)
with wave.open(wpath, "wb") as w:
    pv.synthesize_wav("hey dose how many pills do i have left", w)
import audioop
with wave.open(wpath) as w:
    rate, data = w.getframerate(), w.readframes(w.getnframes())
if rate != 16000:
    data, _ = audioop.ratecv(data, 2, 1, rate, 16000, None)
open(os.path.join(WORK, "cmd.raw"), "wb").write(data)
os.unlink(wpath)


def script(name, body):
    p = os.path.join(BIN, name)
    with open(p, "w") as f:
        f.write(body)
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)


script("pw-record", """#!/usr/bin/env python3
import sys, time
chunk = b"\\x00" * 8000
while True:
    sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush()
    time.sleep(0.25)
""")
script("parec", """#!/usr/bin/env python3
import sys, os, time, random
D = %r
cmd = open(os.path.join(D, "cmd.raw"), "rb").read()
trigger = os.path.join(D, "speak.now")
sent = False
def floor_chunk():
    return b"".join(int(random.gauss(0, 8)).to_bytes(2, "little",
                    signed=True) for _ in range(4000))
while True:
    if not sent and os.path.exists(trigger):
        sent = True
        for i in range(0, len(cmd), 8000):
            sys.stdout.buffer.write(cmd[i:i+8000])
            sys.stdout.buffer.flush(); time.sleep(0.24)
    sys.stdout.buffer.write(floor_chunk())
    sys.stdout.buffer.flush(); time.sleep(0.25)
""" % WORK)
script("pactl", """#!/usr/bin/env python3
import sys
a = sys.argv[1:]
if a[:3] == ["list", "short", "sources"]:
    print("42\\talsa_input.usb-C-Media_USB_PnP_Sound_Device-00.mono"
          "\\tPipeWire\\ts16le 1ch 48000Hz\\tRUNNING")
elif a[:3] == ["list", "short", "sinks"]:
    print("55\\talsa_output.usb-Speaker_Co-01.analog-stereo"
          "\\tPipeWire\\ts16le 2ch 48000Hz\\tRUNNING")
""")
for n in ("pw-play", "paplay", "amixer", "systemctl"):
    script(n, "#!/bin/sh\nexit 0\n")

# ── run the real engine against it ───────────────────────────────
from dose_voice import DoseVoice


class FakeRoot:
    def after(self, _ms, fn, *a):
        fn(*a)


class FakeApp:
    def __init__(self):
        self.root = FakeRoot()
        self.settings = {}
        self.med_data = {
            "blue": {"name": "Sertraline", "loaded": True,
                     "count": 28, "dose_times": ["8:00 AM"],
                     "schedule_days": ["Mon", "Tue", "Wed", "Thu",
                                       "Fri", "Sat", "Sun"]},
            "demo": {"name": "Demo", "loaded": False, "count": 0}}

    def _dose_due_map(self): return {}
    def _get_today_schedule(self): return []
    def _dose_status(self, k, t): return None
    def _adherence_stats(self):
        return {"score": 90, "on_time": 9, "late": 1, "missed": 0}
    def _start_dispense(self, key):
        raise AssertionError("VOICE MUST NEVER DISPENSE")
    def _nav(self, m): pass
    def _save_med(self): pass
    def _draw_frame(self): pass
    def _med_info_for(self, n): return ["Take with water"]
    def _voice_ui_state(self, *a, **k): pass


app = FakeApp()
v = DoseVoice(app)
assert v.available, "engine not available: %s" % v.reason
v.start()

t0 = time.time()
while time.time() - t0 < 60:
    if getattr(v, "mic_trail", None) and "·" in (v.mic_name or ""):
        break
    time.sleep(0.5)
print("mic_name:", v.mic_name)
for ln in getattr(v, "mic_trail", []):
    print("trail:", ln)
assert any("noise floor 0" in ln for ln in v.mic_trail), \
    "dead route not detected"
assert "parec" in v.mic_name and "hearing OK" in v.mic_name, v.mic_name

lvl = v.mic_level(2.0)
print("idle mic level:", lvl)
assert 1 <= lvl <= 60, lvl

open(os.path.join(WORK, "speak.now"), "w").write("go")
t0 = time.time()
while time.time() - t0 < 90:
    if v._last_exchange:
        break
    time.sleep(0.5)
ex = v._last_exchange
print("exchange:", ex)
assert ex, "spoken wake command never triggered an exchange"
utext = str(ex).lower()
reply = (v._last_reply or "").lower()
print("reply:", reply)
assert "pill" in utext or "left" in utext or "many" in utext, utext
assert "sertraline" in reply or "28" in reply, reply
v.stop()
shutil.rmtree(WORK, ignore_errors=True)
print("=== HARDWARE-IN-THE-LOOP: ALL PASSED ===")
