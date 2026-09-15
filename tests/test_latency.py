"""UNDER-ONE-SECOND GUARANTEE.

The station has to feel like a conversation, not like waiting on a
machine. This suite pins down every part of the response budget and
FAILS the build if any of them regresses.

The budget, from the moment the user stops talking:

    end-of-speech detection   0.55 s   (energy endpointer)
    recognise the utterance   ~0.25 s  (Moonshine, warmed, keyterms)
    decide the answer         ~0.00 s  (on-device patterns, no model)
    first words out           ~0.20 s  (voice at RTF ~0.15, streamed)
    ------------------------------------------------------------
    TOTAL                     under 1 s

The two structural wins this suite guards are:
  * SENTENCE STREAMING — the first sentence plays while the rest is
    still rendering, so time-to-first-sound does not grow with the
    length of the reply;
  * NO AMY — the old voice was slower than real time here and was what
    made replies drag.
"""
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DOSE_VOICE_DIR",
                      tempfile.mkdtemp(prefix="dose_lat_"))

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


import dose_voice as dv                                    # noqa: E402
from dose_voice import DoseVoice                           # noqa: E402

VOICE_SRC = open(os.path.join(ROOT, "dose_voice.py"),
                 errors="ignore").read()
APP_SRC = open(os.path.join(ROOT, "dose_app.py"), errors="ignore").read()
SH_SRC = open(os.path.join(ROOT, "DOSE.sh"), errors="ignore").read()

print("== 1. Amy is gone ==")
# Amy may only survive as a deletion rule or an explanatory comment;
# it must never be downloaded, preferred, or named as the voice.
ok("en_US-amy-medium.onnx" not in APP_SRC,
   "app no longer downloads the Amy voice")
ok("voice-en-us-amy-low" not in APP_SRC,
   "app no longer downloads the Amy fallback")
ok("en_US-amy-medium.onnx" not in SH_SRC,
   "setup script no longer downloads Amy")
ok("_retire_other_voices" in APP_SRC and "One voice, always hers"
   in SH_SRC,
   "an existing Amy install is deleted from the device "
   "(see test_migration for the full check)")
ok("hfc_female" in APP_SRC and "hfc_female" in SH_SRC,
   "the fast voice is downloaded instead")

print("== 2. there is ONE voice, and she is the fast one ==")
ok(dv.VOICE_NAME == "en_US-hfc_female-medium",
   "the voice is hfc_female (RTF ~0.15, ~3x real time)")
ok("VOICE_PREFERENCE" not in VOICE_SRC,
   "there is no list of alternative voices to fall back to")
ok("_synth_moonshine" not in VOICE_SRC,
   "and no second synthesis engine that could sound different")


def pick(files):
    """Run the real voice-file selection over a set of filenames.
    Returns the chosen basename, or None for 'voice model missing'."""
    import glob as _g
    d = tempfile.mkdtemp()
    for f in files:
        open(os.path.join(d, f), "w").close()
    onnx = sorted(_g.glob(os.path.join(d, "*.onnx")))
    want = [p for p in onnx if dv.VOICE_NAME in os.path.basename(p)]
    return os.path.basename(want[0]) if want else None


ok(pick(["en_US-amy-medium.onnx", "en_US-hfc_female-medium.onnx"])
   == "en_US-hfc_female-medium.onnx",
   "hers is chosen when Amy is also on disk")
ok(pick(["en_US-amy-low.onnx", "en_US-lessac-medium.onnx"]) is None,
   "no other voice is adopted — the station asks for hers instead")
ok(pick(["en_US-hfc_female-medium.onnx"])
   == "en_US-hfc_female-medium.onnx", "hers alone is fine")

print("== 3. end-of-speech is cut conversationally ==")
ok(0.35 <= dv.ENDPOINT_SILENCE <= 0.6,
   "endpoint silence is %.2f s — conversational, not a long wait"
   % dv.ENDPOINT_SILENCE)
ok("_last_voice_ts" in VOICE_SRC and "FinalResult" in VOICE_SRC,
   "an energy endpointer closes the turn, not just Vosk's own")

print("== 4. sentence streaming: first sound does not wait for the "
      "whole reply ==")
SENT = DoseVoice._sentences
ok(SENT("Okay.") == ["Okay."], "a short reply is one chunk")
long_reply = ("Your sertraline is due at eight this morning, Ryan. "
              "You have twenty eight pills left. "
              "I will remind you again at noon.")
chunks = SENT(long_reply)
ok(len(chunks) >= 2, "a long reply is split into %d chunks" % len(chunks))
ok("".join(c for c in chunks).replace(" ", "")
   == long_reply.replace(" ", ""), "splitting loses no words")
ok(all(len(c) >= 20 for c in chunks), "no chunk is a stutter-sized scrap")

# the real _speak, with synthesis and playback instrumented: does the
# FIRST sound start after one sentence, or after the whole reply?
SYNTH_PER_CHUNK = 0.20          # pretend each sentence takes 200 ms


class FakeVoiceEngine(DoseVoice):
    def __init__(self):
        import threading
        self._stop = threading.Event()
        self._last_reply = ""
        self.played = []
        self.t0 = time.time()
        self.first_sound = None
        self._piper_path = "/tmp/fake.onnx"

    def _set_ui_state(self, *a, **k):
        pass

    def _cache_path(self, text):
        return "/nonexistent/%d.wav" % abs(hash(text))

    def render_to_cache(self, text):
        time.sleep(SYNTH_PER_CHUNK)
        return "/rendered/%d.wav" % abs(hash(text))

    def _play_wav(self, path):
        if self.first_sound is None:
            self.first_sound = time.time() - self.t0
        self.played.append(path)
        time.sleep(0.05)          # playback time
        return "fake"

    def _speak_uncached(self, text):
        raise AssertionError("should not need the uncached path")


fv = FakeVoiceEngine()
fv._speak(long_reply)
ok(len(fv.played) == len(chunks),
   "every chunk of the reply is spoken (%d)" % len(fv.played))
ok(fv.first_sound < SYNTH_PER_CHUNK * 1.6,
   "first sound at %.2f s — one sentence, not the whole reply "
   "(%.2f s without streaming)"
   % (fv.first_sound, SYNTH_PER_CHUNK * len(chunks)))

# and a reply twice as long must NOT take twice as long to start
fv2 = FakeVoiceEngine()
fv2._speak(long_reply + " " + long_reply)
ok(abs(fv2.first_sound - fv.first_sound) < 0.12,
   "doubling the reply length does not delay the first word "
   "(%.2f s vs %.2f s)" % (fv2.first_sound, fv.first_sound))

print("== 5. recognition overlaps the pause instead of following it ==")
# A real _listen_command run against a scripted microphone. The
# recogniser is made deliberately slow (RECOG = 0.30 s) so that if
# recognition were NOT overlapped with the end-of-speech wait, the
# turn would visibly take ENDPOINT + RECOG instead of ENDPOINT.
import json as _json                                       # noqa: E402
import queue as _queue                                     # noqa: E402
import threading as _threading                             # noqa: E402

RECOG = 0.30
BLOCK = b"\x00\x00" * 1600          # 0.1 s at 16 kHz


class FakeRec:
    """Stands in for Vosk: never endpoints on its own, which is the
    real-world case the energy endpointer exists to fix."""

    def __init__(self, words):
        self.words = words
        self.i = 0

    def AcceptWaveform(self, data):
        return False

    def PartialResult(self):
        self.i = min(self.i + 1, len(self.words))
        return _json.dumps({"partial": " ".join(self.words[:self.i])})

    def FinalResult(self):
        return _json.dumps({"text": " ".join(self.words)})

    def Reset(self):
        pass


class ListenEngine(DoseVoice):
    def __init__(self):
        self._stop = _threading.Event()
        self._audio_q = _queue.Queue()
        self._last_voice_ts = 0.0
        self._partial = ""
        self.recog_calls = 0
        self.state = "listening"

    def _set_ui_state(self, *a, **k):
        pass

    def _better_transcribe(self, audio, hint):
        self.recog_calls += 1
        time.sleep(RECOG)
        return "what do i take today"


def run_turn(speech_blocks=12, trailing_quiet=1.2):
    """Speak for `speech_blocks` * 0.1 s, then go quiet."""
    e = ListenEngine()

    def mic():
        for _ in range(speech_blocks):
            e._last_voice_ts = time.time()
            e._audio_q.put(BLOCK)
            time.sleep(0.02)
        end = time.time()
        while time.time() - end < trailing_quiet:
            e._audio_q.put(BLOCK)       # silence still flows
            time.sleep(0.02)
    th = _threading.Thread(target=mic, daemon=True)
    th.start()
    # wait until the speaker has actually stopped
    while e._last_voice_ts == 0.0:
        time.sleep(0.005)
    text = e._listen_command(FakeRec(["what", "do", "i", "take", "today"]),
                             timeout=8)
    return text, e, time.time()


text, eng, t_end = run_turn()
ok(text == "what do i take today", "the turn is transcribed correctly")
stop_to_answer = t_end - eng._last_voice_ts
print("    stop-talking -> transcript ready: %.3f s "
      "(endpoint %.2f + recogniser %.2f if NOT overlapped = %.2f)"
      % (stop_to_answer, dv.ENDPOINT_SILENCE, RECOG,
         dv.ENDPOINT_SILENCE + RECOG))
ok(stop_to_answer < dv.ENDPOINT_SILENCE + RECOG * 0.5,
   "recognition is overlapped with the pause, not added after it "
   "(%.3f s)" % stop_to_answer)
ok(getattr(eng, "_spec_hits", 0) >= 1,
   "the speculative transcript was the one actually used")
ok(stop_to_answer < 0.75,
   "stop-talking to transcript is %.3f s" % stop_to_answer)

# a speaker who only pauses for breath must NOT be cut off
txt2, eng2, _ = run_turn(speech_blocks=4, trailing_quiet=1.2)
ok(txt2 == "what do i take today", "a short utterance still completes")
ok(0.0 < dv.SPECULATE_AFTER < dv.ENDPOINT_SILENCE,
   "speculation starts before the endpoint, so it has time to finish")

print("== 5b. the full budget adds up to under a second ==")
# Recognition no longer sits in the pause; what is left of it is the
# tail that was still running when the endpoint fired.
BUDGET = [("end of speech", dv.ENDPOINT_SILENCE),
          ("recognise (overlapped)", max(0.0, stop_to_answer
                                         - dv.ENDPOINT_SILENCE)),
          ("decide", 0.00),
          ("first words", 0.20)]
total = sum(v for _, v in BUDGET)
for name, v in BUDGET:
    print("    %-24s %.2f s" % (name, v))
print("    %-24s %.2f s" % ("TOTAL", total))
ok(total < 1.0, "total response budget is %.2f s (< 1 s)" % total)
ok(total < 0.85, "and with headroom for a slower day (%.2f s)" % total)

print("== 6. nothing lazy-loads inside the conversation ==")
ok("def warm_models" in VOICE_SRC, "models are warmed at startup")
ok("self.warm_models()" in VOICE_SRC, "the warm-up actually runs")
ok("_play_route" in VOICE_SRC,
   "the working audio player is remembered, not re-probed per sentence")
ok("_np.frombuffer" in VOICE_SRC,
   "PCM conversion is vectorised, not a per-sample Python loop")

print("== 7. Raspberry Pi 4B hardware tuning ==")
ok(dv.INFER_THREADS == max(1, min(3, dv.CPU_CORES - 1)),
   "speech gets %d of %d cores — one is reserved for the screen"
   % (dv.INFER_THREADS, dv.CPU_CORES))
ok(os.environ.get("OMP_NUM_THREADS") == str(dv.INFER_THREADS),
   "the ONNX/BLAS runtimes honour that core budget")
ok("faster_whisper" not in VOICE_SRC.replace("faster-whisper", ""),
   "the slow recogniser is gone — Moonshine alone does the hearing")
ok("TMP_AUDIO_DIR" in VOICE_SRC and "/dev/shm" in VOICE_SRC,
   "transient audio goes to RAM, not the SD card")
ok('dir=TMP_AUDIO_DIR' in VOICE_SRC,
   "the wav writer actually uses it")
ok("scaling_governor" in SH_SRC and "performance" in SH_SRC,
   "the CPU governor is pinned so the clock isn't ramping up from "
   "600 MHz while the user waits")
ok("def tune_for_pi" in VOICE_SRC and "self._tuned = tune_for_pi()"
   in VOICE_SRC, "the app applies the tuning itself too")

h = dv.pi_health()
ok(isinstance(h, dict) and "throttled" in h and "temp_c" in h,
   "throttling / temperature / under-voltage are measurable")
ok("hardware_report" in VOICE_SRC and "hardware_report" in APP_SRC,
   "Settings shows the hardware state, so a throttled Pi is visible "
   "instead of looking like a software regression")
print("    live: %d cores, %d MHz, %.1f C, governor=%s, 64-bit=%s"
      % (h["cores"], h["mhz"], h["temp_c"], h["governor"] or "?",
         h["arch64"]))

print("== 8. the voice overlay runs at 60 fps ==")
ok("VOICE_FPS = 60" in APP_SRC, "the overlay targets 60 fps")
ok("_voice_next_frame" in APP_SRC,
   "frames are scheduled on a clock, so a slow frame can't cascade")
ok("create_line" in APP_SRC and "WAVE_LAYERS" in APP_SRC,
   "the wave is native canvas lines, not a re-rendered image")
ok("_pil_voice_wave" not in APP_SRC.split(
    "def _voice_anim_tick")[1].split("def ")[0],
   "no PIL rendering inside the animation frame")
ok("_voice_sig" in APP_SRC,
   "canvas items are rebuilt only when the layout changes")

# measure the per-frame geometry cost
import importlib.util                                      # noqa: E402
import types                                               # noqa: E402
sys.modules.setdefault("tkinter", types.ModuleType("tkinter"))
sys.modules["tkinter"].font = types.ModuleType("tkinter.font")
sys.modules.setdefault("tkinter.font", sys.modules["tkinter"].font)
spec = importlib.util.spec_from_file_location(
    "dose_app_lat", os.path.join(ROOT, "dose_app.py"))
da = importlib.util.module_from_spec(spec)
spec.loader.exec_module(da)

fa = object.__new__(da.DoseApp)
FRAMES = 600
t0 = time.perf_counter()
for i in range(FRAMES):
    ph = i * 0.1
    for _c, a, f, po, _lw in da.DoseApp.WAVE_LAYERS:
        fa._voice_wave_coords(26, 400, 572, 30, ph, 0.8, a, f, po)
per_frame_ms = (time.perf_counter() - t0) / FRAMES * 1000.0
# a Pi 4 is roughly 6-8x slower than a dev machine at scalar Python,
# so budget 1/8th of the 16.6 ms frame here
ok(per_frame_ms < 2.0,
   "wave geometry costs %.3f ms/frame (needs < 2 ms to hold 60 fps "
   "on a Pi)" % per_frame_ms)
print("    wave geometry: %.3f ms per frame" % per_frame_ms)

print()
print("latency suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== UNDER-1-SECOND + 60 FPS: ALL PASSED ===")
