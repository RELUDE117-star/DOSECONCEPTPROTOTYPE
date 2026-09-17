"""SILERO VAD AND BARGE-IN.

Energy cannot tell a voice from a tap. It only knows loud from quiet,
which is why running water defeated the station: the noise cleared the
gate, so it counted as speech, the turn never ended, and the recogniser
was handed water.

Silero is a 1.3 MB ONNX model that answers "is this 32 ms of audio a
human voice?" — the same component the modular speech-to-speech
pipelines use for turn-taking. It decides three things here: when a
turn starts, when it ends, and whether you have begun talking over her.

The most important property tested below is that it FAILS OPEN. A
voice detector that silently vetoes everything would make the station
deaf, and a deaf medication device is far worse than a noisy one.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DOSE_VOICE_DIR", tempfile.mkdtemp(prefix="dose_vad_"))

import dose_voice as dv                                    # noqa: E402
from dose_voice import DoseVoice                           # noqa: E402

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


def fresh(trusted=True):
    v = object.__new__(DoseVoice)
    v._vad = "unset"
    v._vad_state = None
    v._vad_trusted = trusted
    v._vad_seen = 0
    v._vad_agreed = 0
    v._nfloor = 40.0
    v._barge = False
    v._barge_frames = 0
    v._play_proc = None
    return v


print("== 1. the model loads and runs fast enough ==")
v = fresh()
sess = v._load_vad()
have = sess is not None
if not have:
    print("    (Silero not installed here: %s)"
          % getattr(v, "_vad_reason", ""))
ok(True, "load attempted without raising")

if have:
    import time
    import numpy as np
    silence = np.zeros(512, dtype=np.int16).tobytes()
    v.reset_vad()
    t0 = time.perf_counter()
    N = 300
    for _ in range(N):
        v.vad_speech_prob(silence)
    per = (time.perf_counter() - t0) / N * 1000
    print("    %.3f ms per frame (each frame is 32 ms of audio)" % per)
    ok(per < 4.0,
       "a frame costs %.3f ms — even 8x slower on a Pi that is under "
       "10%% of one core, and it runs on the audio thread" % per)

    print("== 2. it rejects what energy cannot ==")
    sr = 16000
    t = np.arange(sr) / sr
    cases = [
        ("silence", np.zeros(sr)),
        ("a pure tone", np.sin(2 * np.pi * 200 * t) * 9000),
        ("white noise (a fan)", np.random.randn(sr) * 3000),
        ("running water", np.random.randn(sr) * 3000
         * (0.5 + 0.5 * np.sin(2 * np.pi * 0.7 * t))),
    ]
    for label, sig in cases:
        pcm = sig.astype(np.int16)
        v.reset_vad()
        probs = []
        for i in range(0, len(pcm) - 512, 512):
            p = v.vad_speech_prob(pcm[i:i + 512].tobytes())
            if p is not None:
                probs.append(p)
        peak = max(probs) if probs else 0.0
        ok(peak < dv.VAD_THRESHOLD,
           "%-22s is not called speech (peak %.3f < %.2f) — and all of "
           "these are LOUD, which is the whole point"
           % (label, peak, dv.VAD_THRESHOLD))

print("== 3. IT FAILS OPEN — it can never make the station deaf ==")
# No detector at all: energy decides alone.
v = fresh()
v._vad = None
ok(v.is_speech(b"\x00\x00" * 512, True),
   "with no detector installed, loud audio is still heard")
ok(not v.is_speech(b"\x00\x00" * 512, False),
   "and quiet audio still is not")

# A detector that says no to everything must be dropped, not obeyed.
v = fresh()
v._vad = object()                      # present but useless
v.vad_speech_prob = lambda _f: 0.0     # vetoes every single frame
heard = 0
for _ in range(400):
    if v.is_speech(b"\x01\x01" * 512, True):
        heard += 1
ok(not v._vad_trusted,
   "a detector that disagrees with 400 frames of clear audio is "
   "STOPPED being trusted")
ok(heard > 0,
   "and the station goes back to hearing (%d frames) instead of "
   "staying deaf" % heard)
ok("ignored" in (v._vad_reason or ""),
   "with the reason on the Settings page: %r" % v._vad_reason)

# A working detector is kept.
v = fresh()
v._vad = object()
v.vad_speech_prob = lambda _f: 0.9
for _ in range(400):
    v.is_speech(b"\x01\x01" * 512, True)
ok(v._vad_trusted, "a detector that agrees is kept")

print("== 4. talking over her stops her ==")
ok(dv.BARGE_IN, "barge-in is on by default")
ok(dv.BARGE_THRESHOLD > dv.VAD_THRESHOLD,
   "it needs MORE certainty (%.2f) than normal listening (%.2f) — her "
   "own voice is leaking back from a speaker inches away"
   % (dv.BARGE_THRESHOLD, dv.VAD_THRESHOLD))
ok(dv.BARGE_FRAMES >= 3,
   "and %d frames (~%d ms) of it, so a cough or a door does not cut "
   "her off" % (dv.BARGE_FRAMES, dv.BARGE_FRAMES * 32))


class FakeProc:
    """Stands in for the playback process. poll() returns None while
    it is still running, like subprocess.Popen."""

    def __init__(self):
        self.killed = False

    def poll(self):
        return None if not self.killed else 0

    def terminate(self):
        self.killed = True


class FinishedProc(FakeProc):
    def poll(self):
        return 0                    # already ended on its own


loud = (b"\x00\x40" * 512)          # comfortably above the floor
v = fresh()
v._play_proc = FakeProc()
v.vad_speech_prob = lambda _f: 0.95
for i in range(dv.BARGE_FRAMES):
    ok(not v._barge or i == dv.BARGE_FRAMES - 1,
       "it does not fire before %d frames" % dv.BARGE_FRAMES)
    v._detect_barge_in(loud)
ok(v._barge, "sustained speech over her sets the interrupt")
ok(v._play_proc.killed, "and playback is actually stopped")

# a single loud frame is NOT an interruption
v = fresh()
v._play_proc = FakeProc()
v.vad_speech_prob = lambda _f: 0.95
v._detect_barge_in(loud)
ok(not v._barge, "one frame is not an interruption")
ok(not v._play_proc.killed, "and nothing is stopped")

# her own voice leaking back (loud, but not called speech) must not
v = fresh()
v._play_proc = FakeProc()
v.vad_speech_prob = lambda _f: 0.2
for _ in range(20):
    v._detect_barge_in(loud)
ok(not v._barge,
   "loud audio that is not a voice never interrupts her")

# a clip that already finished must not be "terminated" — the handle
# may since have been reused by the next one
v = fresh()
v._play_proc = FinishedProc()
v._stop_playback()
ok(not v._play_proc.killed,
   "a clip that has already ended is not terminated again")

# quiet audio never does either
v = fresh()
v._play_proc = FakeProc()
v.vad_speech_prob = lambda _f: 0.99
for _ in range(20):
    v._detect_barge_in(b"\x00\x00" * 512)
ok(not v._barge, "and neither does silence")

print("== 5. playback is genuinely interruptible ==")
VSRC = open(os.path.join(ROOT, "dose_voice.py"), errors="ignore").read()
play = VSRC.split("def _play_wav")[1].split("\n    def ")[0]
ok("subprocess.Popen" in play,
   "playback runs under Popen — subprocess.run() gives no handle to "
   "stop it, so barge-in could not have worked at all")
ok("self._play_proc = proc" in play,
   "and the handle is kept so it can be terminated")
ok("if self._barge:" in play,
   "a deliberate stop is not treated as a playback failure")
spk = VSRC.split("def _speak")[1].split("\n    def ")[0]
ok("self._stop.is_set() or self._barge" in spk,
   "and the rest of the reply is abandoned — you interrupted it "
   "because you did not want it")

ex = VSRC.split("def _handle_exchange")[1].split("\n    def ")[0]
ok("self._barge" in ex and "reset_vad" in ex,
   "the conversation takes your turn immediately instead of waiting "
   "out a pause you already filled")

print("== 6. it is visible in Settings ==")
ok("Voice detector" in VSRC, "the detector's state is shown")
ok("Talk over me" in VSRC, "so is barge-in")

print()
print("vad suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== SILERO VAD + BARGE-IN: ALL PASSED (and fails open) ===")
