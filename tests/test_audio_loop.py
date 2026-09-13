"""REAL end-to-end audio test: Piper speaks user commands, Vosk
transcribes them, the brain answers, Piper speaks the answer, and
Vosk transcribes the answer back for verification. This exercises
the exact STT -> brain -> TTS chain that runs on the Pi."""
import sys, os, json, wave, io, time, tempfile
# models live next to this file by default; override with
# DOSE_TEST_MODELDIR (e.g. a scratch dir holding vosk + piper models)
SCRATCH = os.path.dirname(os.path.abspath(__file__))
os.environ["DOSE_VOICE_DIR"] = os.environ.get(
    "DOSE_TEST_MODELDIR", os.path.join(SCRATCH, "voice", "modeldir"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vosk import Model, KaldiRecognizer, SetLogLevel
from piper import PiperVoice
from dose_voice import DoseVoice
from datetime import datetime

SetLogLevel(-1)
import glob as _glob
VD = os.environ["DOSE_VOICE_DIR"]
_onnx = sorted(_glob.glob(os.path.join(VD, "*.onnx")))[0]
_vosk = sorted(_glob.glob(os.path.join(VD, "vosk-model*")))[0]
voice = PiperVoice.load(_onnx)
model = Model(_vosk)


def tts(text):
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with wave.open(path, "wb") as w:
        voice.synthesize_wav(text, w)
    return path


def stt(path):
    rec = KaldiRecognizer(model, 16000)
    with wave.open(path) as w:
        assert w.getframerate() == 16000
        while True:
            data = w.readframes(4000)
            if not data:
                break
            rec.AcceptWaveform(data)
    out = json.loads(rec.FinalResult()).get("text", "")
    os.unlink(path)
    return out


# minimal fake app (same as brain battery)
class FakeRoot:
    def after(self, _ms, fn, *a):
        fn(*a)

class FakeApp:
    def __init__(self):
        self.root = FakeRoot()
        self.med_data = {
            "blue": {"name": "Sertraline", "loaded": True, "count": 28,
                     "dose_times": ["8:00 AM"],
                     "schedule_days": ["Mon", "Tue", "Wed", "Thu",
                                       "Fri", "Sat", "Sun"]},
            "yellow": {"name": "Atorvastatin", "loaded": True,
                       "count": 30, "dose_times": ["9:00 PM"],
                       "schedule_days": ["Mon", "Tue", "Wed", "Thu",
                                         "Fri", "Sat", "Sun"]},
            "demo": {"name": "Demo", "loaded": False, "count": 0},
        }
        self._demo_registered = False
    def _dose_due_map(self):
        return {}
    def _get_today_schedule(self):
        now = datetime.now()
        return [{"key": "yellow", "name": "Atorvastatin",
                 "time": "9:00 PM", "count": 30,
                 "sort": now.replace(hour=23, minute=59)}]
    def _dose_status(self, key, ts):
        return None
    def _adherence_stats(self):
        return {"score": 88, "on_time": 7, "late": 1, "missed": 0}
    def _start_dispense(self, key):
        raise AssertionError("voice must never dispense!")
    def _nav(self, mode):
        self.nav_to = mode
    def _save_med(self):
        pass
    def _draw_frame(self):
        pass
    def _med_info_for(self, name):
        return ["Cholesterol (statin)", "Avoid grapefruit juice"]

app = FakeApp()
v = object.__new__(DoseVoice)
v.app = app
v._flow = None
v.state = "idle"
v._last_reply = ""
v._last_exchange = None
v._learn = v._learn_load()

FAIL = []

def spoken_exchange(user_says, expect_in_reply, wake=True,
                    text_expect=()):
    """Full loop: user's words -> TTS -> STT -> wake strip -> brain ->
    TTS of the reply -> STT -> verify the heard reply."""
    t0 = time.time()
    heard_user = stt(tts(user_says))
    rest = v._match_wake(heard_user) if wake else heard_user
    if wake and rest is None:
        FAIL.append((user_says, "WAKE NOT DETECTED: " + heard_user))
        print(f"FAIL wake: {user_says!r} heard as {heard_user!r}")
        return
    reply, _keep = v.respond(rest if wake else heard_user)
    heard_reply = stt(tts(reply))
    # robust words are verified on the audio round trip; anything in
    # text_expect is verified on the reply text (e.g. drug names that
    # a low-quality test voice garbles)
    ok = all(e.lower() in heard_reply.lower() for e in expect_in_reply)
    ok = ok and all(e.lower() in reply.lower() for e in text_expect)
    tag = "OK " if ok else "FAIL"
    if not ok:
        FAIL.append((user_says, heard_reply))
    print(f"{tag} [{time.time()-t0:4.1f}s] {user_says!r}")
    print(f"      heard:  {heard_user!r}")
    print(f"      reply:  {reply!r}")
    print(f"      heard back: {heard_reply!r}")

spoken_exchange("Hey dose, what do I need to take next?",
                ["nine pm"], text_expect=["Atorvastatin"])
spoken_exchange("Hey dose, how many sertraline do I have left?",
                ["twenty eight"])
spoken_exchange("Hey dose, how am I doing?",
                ["eighty eight percent"])
spoken_exchange("Hey dose, who are you?",
                ["assistant"], text_expect=["Dose"])
spoken_exchange("Hey dose, what are your protocols?",
                ["protocol one", "protect the patient"])
spoken_exchange("Hey dose, dispense my atorvastatin",
                ["never dispense"], text_expect=["Atorvastatin", "hold"])

# multi-turn by voice: add a medication
def flow_line(user_says, expect):
    heard = stt(tts(user_says))
    reply, keep = v.respond(heard)
    heard_reply = stt(tts(reply))
    ok = all(e.lower() in heard_reply.lower() for e in expect)
    tag = "OK " if ok else "FAIL"
    if not ok:
        FAIL.append((user_says, heard_reply))
    print(f"{tag} flow {user_says!r}\n      heard: {heard!r}"
          f"\n      reply heard back: {heard_reply!r}")
    return keep

spoken_exchange("Hey dose, add a new medication",
                ["what is the medication called"])
flow_line("ibuprofen", ["read me the label"])
flow_line("take one tablet at seven thirty pm thirty pills",
          ["seven thirty", "is that correct"])
flow_line("yes that is correct", ["registered"])
assert app.med_data["demo"]["loaded"]
assert app.med_data["demo"]["dose_times"] == ["7:30 PM"], \
    app.med_data["demo"]["dose_times"]
assert app.med_data["demo"]["count"] == 30
print("  spoken add-med saved:", app.med_data["demo"]["name"],
      app.med_data["demo"]["count"], app.med_data["demo"]["dose_times"])

print()
if FAIL:
    print(f"=== {len(FAIL)} FAILURES ===")
    for a, b in FAIL:
        print("-", a, "->", b)
    sys.exit(1)
print("=== REAL AUDIO LOOP: ALL PASSED ===")
