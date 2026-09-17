"""ZERO-COST / OFFLINE GUARANTEE.

The Home Station must never cost money to run: no paid APIs, no API
keys, no per-use billing, and no medication data leaving the device.
This suite FAILS the build if anything paid or cloud-dependent creeps
into the voice path, so the guarantee can't be broken by accident.

Every model the station uses is free and runs on-device. There are
now exactly two, doing different jobs:
  Moonshine Base (MIT) — hearing
  Piper hfc_female (MIT) — her voice
plus Vosk (Apache-2.0) as the live on-screen listener and deaf-proof
fallback, and openWakeWord (Apache-2.0) as an optional wake word.
"""
import os
import re
import sys

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


def read(name):
    with open(os.path.join(ROOT, name), "r", errors="ignore") as f:
        return f.read()


VOICE = read("dose_voice.py")
NLU = read("dose_nlu.py")
APP = read("dose_app.py")
SETUP = read("DOSE.sh")

# ── 1. No paid providers anywhere in the voice path ──────────────────
PAID = [
    "api.groq.com", "groq_api_key", "openai.com/v1", "api.openai.com",
    "elevenlabs", "api.anthropic.com", "deepgram", "assemblyai",
    "azure.cognitiveservices", "speech.googleapis", "aws.amazon.com/polly",
]
for blob, who in ((VOICE, "dose_voice.py"), (NLU, "dose_nlu.py")):
    low = blob.lower()
    for term in PAID:
        ok(term not in low, "no paid provider %r in %s" % (term, who))

# ── 2. No API-key plumbing at all ────────────────────────────────────
for blob, who in ((VOICE, "dose_voice.py"), (NLU, "dose_nlu.py")):
    ok(not re.search(r"api[_-]?key", blob, re.I),
       "no api key handling in %s" % who)
    ok(not re.search(r"(?i)authorization\s*[:=]|bearer\s+\$?\{?token",
                     blob),
       "no auth headers in %s" % who)

# ── 3. The voice engine makes NO outbound web requests ───────────────
ok("requests.post" not in VOICE and "requests.get" not in VOICE,
   "voice engine makes no HTTP requests")
ok("socket." not in VOICE, "voice engine opens no sockets")

# The engine opens exactly ONE url, and only to fetch the 2.3 MB voice
# detector. Everything else it needs is downloaded by the app or the
# setup script. This is checked precisely rather than by banning
# urlopen outright, because the ban is what the property is FOR: no
# telemetry, no paid API, nothing metered.
import re as _re                                            # noqa: E402
_urls = _re.findall(r"urlopen\(([^)]*)", VOICE)
ok(len(_urls) <= 1,
   "voice engine opens at most one URL (found %d)" % len(_urls))
for _u in _urls:
    ok("VAD_MODEL_URL" in _u,
       "and it is the voice-detector model, nothing else: %r"
       % _u.strip()[:40])
# read the constant itself rather than scraping source lines — the
# URL is split across two of them
import dose_voice as _dvz                                   # noqa: E402
_vad_url = getattr(_dvz, "VAD_MODEL_URL", "")
ok("githubusercontent.com" in _vad_url or "github.com" in _vad_url,
   "from GitHub — free, open, no account: %s" % _vad_url[:48])
ok("silero" in _vad_url.lower(), "the Silero VAD model (MIT)")
ok(_vad_url.endswith(".onnx"), "a single model file, not a package")

# ── 4. Network use is limited to FREE hosts: the GitHub update and
#      free, open model downloads. No paid or metered service.
FREE_HOSTS = ("github.com", "githubusercontent.com", "api.github.com",
              "raw.githubusercontent.com",   # Silero VAD model (MIT)
              "alphacephei.com",        # Vosk models (Apache-2.0)
              "huggingface.co",         # Piper voices (MIT)
              "moonshine.ai")           # Moonshine models (MIT)
urls = re.findall(r"https?://[^\s\"')]+", APP)
for u in urls:
    host = u.split("//", 1)[-1].split("/")[0].lower()
    ok(any(host.endswith(h) for h in FREE_HOSTS),
       "network target is a free host (found %s)" % host)

# ── 5. Installers pull only free, open packages ──────────────────────
for pkg in ("vosk", "piper-tts", "moonshine-voice",
            "rapidfuzz", "jellyfish", "openwakeword"):
    ok(pkg in SETUP or pkg in APP, "installs free package %s" % pkg)
ok("--api-key" not in SETUP and "GROQ" not in SETUP,
   "setup script needs no API key")

# ── 6. The engine imports cleanly and answers with NO network ────────
import tempfile
os.environ["DOSE_VOICE_DIR"] = tempfile.mkdtemp(prefix="dose_zero_")
import socket as _socket

_real_socket = _socket.socket


class _NoNet(_real_socket):
    def connect(self, *a, **k):
        raise AssertionError("voice tried to reach the network!")

    def connect_ex(self, *a, **k):
        raise AssertionError("voice tried to reach the network!")


from dose_voice import DoseVoice  # noqa: E402


class FakeRoot:
    def after(self, _ms, fn, *a):
        fn(*a)


class FakeApp:
    def __init__(self):
        self.root = FakeRoot()
        self.settings = {}
        self.med_data = {
            "blue": {"name": "Sertraline", "loaded": True, "count": 28,
                     "dose_times": ["8:00 AM"], "schedule_days": []},
            "demo": {"name": "Demo", "loaded": False, "count": 0}}

    def _dose_due_map(self):
        return {}

    def _get_today_schedule(self):
        return []

    def _dose_status(self, k, t):
        return None

    def _adherence_stats(self):
        return {"score": 90, "on_time": 9, "late": 1, "missed": 0}

    def _start_dispense(self, key):
        raise AssertionError("voice must never dispense")

    def _nav(self, m):
        pass

    def _save_med(self):
        pass

    def _draw_frame(self):
        pass

    def _med_info_for(self, n):
        return ["Take with water"]

    def _voice_ui_state(self, *a, **k):
        pass


v = object.__new__(DoseVoice)
v.app = FakeApp()
v._flow = None
v.state = "idle"
v._last_reply = ""
v._last_exchange = None
v._learn = v._learn_load()

_socket.socket = _NoNet          # any network use now raises
try:
    for phrase in ("what do i take today",
                   "how many pills do i have left",
                   "what's next",
                   "did i take my sertraline",
                   "should i take a double dose",
                   "i want to kill myself",
                   "hello"):
        reply, _ = v.respond(phrase)
        ok(isinstance(reply, str) and reply,
           "answered offline with no network: %r" % phrase[:32])
finally:
    _socket.socket = _real_socket

print()
print("zero-cost suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== ZERO-COST / OFFLINE GUARANTEE: ALL PASSED ($0 to run) ===")
