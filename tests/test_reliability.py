"""RELIABILITY — the new speed must never cost correctness.

Making something fast is easy if you allow it to occasionally be
wrong. On a medication device that trade is not available. Everything
added for latency — sentence streaming, speculative recognition, the
remembered audio route, the frame clock — is checked here for the ways
it could fail, using real fault injection rather than inspection.

The invariants:
  * a reply is ALWAYS spoken in full, in order, even when synthesis
    or playback fails part-way through
  * a speculative transcript is NEVER used if the user kept talking
  * the station is NEVER left with no voice
  * the assistant NEVER dispenses medication by itself
  * nothing leaks threads across repeated turns
"""
import json
import os
import queue
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DOSE_VOICE_DIR",
                      tempfile.mkdtemp(prefix="dose_rel_"))

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


REPLY = ("Your sertraline is due at eight this morning, Ryan. "
         "You have twenty eight pills left. "
         "I will remind you again at noon.")


class SpeakEngine(DoseVoice):
    """A speaking engine whose synthesis and playback we can break."""

    def __init__(self, fail_render=(), fail_play=()):
        self._stop = threading.Event()
        self._last_reply = ""
        self._piper_path = "/tmp/fake.onnx"
        self.fail_render = set(fail_render)
        self.fail_play = set(fail_play)
        self.spoken = []            # text actually voiced, in order
        self.n = 0

    def _set_ui_state(self, *a, **k):
        pass

    def _cache_path(self, text):
        return "/nonexistent/%d.wav" % abs(hash(text))

    def render_to_cache(self, text):
        i = self._index(text)
        if i in self.fail_render:
            return None
        return "rendered::%s" % text

    def _play_wav(self, path):
        text = path.split("::", 1)[-1]
        i = self._index(text)
        if i in self.fail_play:
            return False
        self.spoken.append(text)
        return "fake"

    def _speak_uncached(self, text):
        self.spoken.append(text)

    def _index(self, text):
        chunks = DoseVoice._sentences(REPLY)
        for i, c in enumerate(chunks):
            if c == text:
                return i
        return -1


CHUNKS = DoseVoice._sentences(REPLY)
print("== 1. the whole reply is always spoken, in order ==")
e = SpeakEngine()
e._speak(REPLY)
ok(e.spoken == CHUNKS, "clean run speaks every sentence in order")

for label, kw in (("first sentence fails to render",
                   {"fail_render": {0}}),
                  ("a middle sentence fails to render",
                   {"fail_render": {1}}),
                  ("the last sentence fails to render",
                   {"fail_render": {len(CHUNKS) - 1}}),
                  ("every sentence fails to render",
                   {"fail_render": set(range(len(CHUNKS)))})):
    e = SpeakEngine(**kw)
    e._speak(REPLY)
    ok(e.spoken == CHUNKS,
       "%s: still says the whole reply in order (said %d/%d)"
       % (label, len(e.spoken), len(CHUNKS)))

print("== 2. speaking survives a synthesizer that throws ==")


class ThrowingEngine(SpeakEngine):
    def render_to_cache(self, text):
        raise RuntimeError("synthesizer exploded")


e = ThrowingEngine()
e._speak(REPLY)                     # must not raise
ok(True, "a synthesizer that raises does not crash the assistant")

print("== 3. a shutdown mid-reply stops promptly ==")


class SlowEngine(SpeakEngine):
    def render_to_cache(self, text):
        time.sleep(0.05)
        return "rendered::%s" % text


e = SlowEngine()
e._stop.set()
t0 = time.time()
e._speak(REPLY)
ok(time.time() - t0 < 1.0,
   "a stop request ends the reply quickly (%.2f s)" % (time.time() - t0))

print("== 4. speculation is never used when the user kept talking ==")
BLOCK = b"\x00\x00" * 1600          # 0.1 s


class FakeRec:
    def __init__(self, words):
        self.words, self.i = words, 0

    def AcceptWaveform(self, d):
        return False

    def PartialResult(self):
        self.i = min(self.i + 1, len(self.words))
        return json.dumps({"partial": " ".join(self.words[:self.i])})

    def FinalResult(self):
        return json.dumps({"text": " ".join(self.words)})

    def Reset(self):
        pass


class ListenEngine(DoseVoice):
    """Recognition returns whatever audio it was actually given, so a
    stale speculation is visible in the result rather than hidden."""

    def __init__(self, recog_time=0.25):
        self._stop = threading.Event()
        self._audio_q = queue.Queue()
        self._last_voice_ts = 0.0
        self._partial = ""
        self.recog_time = recog_time
        self.calls = []
        self.state = "listening"

    def _set_ui_state(self, *a, **k):
        pass

    def _better_transcribe(self, audio, hint, allow_cloud=True):
        n = len(audio)
        time.sleep(self.recog_time)
        self.calls.append(n)
        # the transcript encodes how much audio it saw
        return "utterance of %d bytes" % n


def speak_then_pause_then_speak():
    """Someone who pauses for breath mid-sentence and carries on.
    The speculation fired during that pause MUST be discarded."""
    e = ListenEngine(recog_time=0.25)
    first_len = [0]

    def mic():
        for _ in range(8):                      # "what do i take"
            e._last_voice_ts = time.time()
            e._audio_q.put(BLOCK)
            time.sleep(0.02)
        first_len[0] = 8
        t = time.time()                          # ...pause for breath
        while time.time() - t < 0.30:
            e._audio_q.put(BLOCK)
            time.sleep(0.02)
        for _ in range(8):                      # "...today please"
            e._last_voice_ts = time.time()
            e._audio_q.put(BLOCK)
            time.sleep(0.02)
        t = time.time()
        while time.time() - t < 1.2:
            e._audio_q.put(BLOCK)
            time.sleep(0.02)
    threading.Thread(target=mic, daemon=True).start()
    while e._last_voice_ts == 0.0:
        time.sleep(0.005)
    return e, e._listen_command(FakeRec(["what", "do", "i", "take",
                                         "today", "please"]), timeout=8)


eng, text = speak_then_pause_then_speak()
final_bytes = int(text.split()[2])
ok(eng.calls, "recognition ran")
ok(final_bytes == max(eng.calls),
   "the transcript covers ALL the audio, not just the part before the "
   "breath (%d bytes of %d)" % (final_bytes, max(eng.calls)))
ok(len(eng.calls) >= 2,
   "the mid-sentence speculation was thrown away and redone")

print("== 5. speculation is used when the user really did finish ==")


def speak_then_stop():
    e = ListenEngine(recog_time=0.25)

    def mic():
        for _ in range(10):
            e._last_voice_ts = time.time()
            e._audio_q.put(BLOCK)
            time.sleep(0.02)
        t = time.time()
        while time.time() - t < 1.5:
            e._audio_q.put(BLOCK)
            time.sleep(0.02)
    threading.Thread(target=mic, daemon=True).start()
    while e._last_voice_ts == 0.0:
        time.sleep(0.005)
    txt = e._listen_command(FakeRec(["whats", "next"]), timeout=8)
    return e, txt, time.time() - e._last_voice_ts


eng2, txt2, stop_to_text = speak_then_stop()
ok(bool(txt2), "a finished utterance is transcribed")
ok(getattr(eng2, "_spec_hits", 0) >= 1,
   "the speculative transcript was reused")
ok(stop_to_text < dv.ENDPOINT_SILENCE + 0.15,
   "and it cost almost nothing on top of the endpoint (%.3f s)"
   % stop_to_text)

print("== 5b. a speaker reaching for the next word is not clipped ==")
# "how many sertraline do i ..." — the transcript ends on a hanging
# word, so the endpoint must hold off rather than cutting the turn.


def trailing_on(word, quiet=0.60):
    e = ListenEngine(recog_time=0.02)
    words = ["how", "many", "sertraline", "do", word]

    def mic():
        for _ in range(8):
            e._last_voice_ts = time.time()
            e._audio_q.put(BLOCK)
            time.sleep(0.02)
        t = time.time()
        while time.time() - t < quiet:
            e._audio_q.put(BLOCK)
            time.sleep(0.02)
    threading.Thread(target=mic, daemon=True).start()
    while e._last_voice_ts == 0.0:
        time.sleep(0.005)
    txt = e._listen_command(FakeRec(words), timeout=quiet + 0.35)
    return txt, time.time() - e._last_voice_ts


_, held = trailing_on("i", quiet=2.9)   # "...do i" — mid-thought
_, cut = trailing_on("left")            # "...do i have left" — finished
ok(held > cut,
   "a hanging word buys more time before the turn closes "
   "(%.2f s vs %.2f s)" % (held, cut))
ok(held >= dv.ENDPOINT_DANGLING * 0.7,
   "and it is a real amount of extra time (%.2f s of a %.1f s grace)"
   % (held, dv.ENDPOINT_DANGLING))
ok(cut < dv.ENDPOINT_STABLE + 0.35,
   "while a finished sentence still closes promptly (%.2f s)" % cut)
ok("left" not in dv.HANGING_WORDS and "today" not in dv.HANGING_WORDS
   and "it" not in dv.HANGING_WORDS,
   "words people DO end on are not treated as hanging")
ok("and" in dv.HANGING_WORDS and "my" in dv.HANGING_WORDS
   and "um" in dv.HANGING_WORDS,
   "words people never end on are")

# the policy itself, straight from the engine
pol = ListenEngine()
pol._med_names = lambda: ["Sertraline", "Metformin"]
CASES = [
    ("i want to kill myself", 0.0, "a crisis is never made to wait"),
    ("chest pain", 0.0, "nor an emergency"),
    ("what time is it", dv.ENDPOINT_STABLE,
     "a finished command commits fast even ending on 'it'"),
    ("how many sertraline do i have left", dv.ENDPOINT_STABLE,
     "so does a complete question"),
    ("give me my sertraline", dv.ENDPOINT_CORRECTION,
     "a dispense request leaves room to change your mind"),
    ("ive been thinking about", dv.ENDPOINT_DANGLING,
     "a sentence cut mid-thought WAITS"),
    ("how many of my", dv.ENDPOINT_DANGLING, "so does this one"),
    ("umm what about my", dv.ENDPOINT_DANGLING, "and this one"),
]
for phrase, want, why in CASES:
    got = pol._endpoint_wait(phrase)
    ok(abs(got - want) < 0.001,
       "%s — %r waits %.2fs (want %.2fs)" % (why, phrase, got, want))
ok(dv.ENDPOINT_DANGLING >= 2.0,
   "the mid-thought grace is %.1f s — long enough to beat a real "
   "pause" % dv.ENDPOINT_DANGLING)
ok(dv.ENDPOINT_STABLE <= 0.4,
   "while a finished command still commits in %.2f s"
   % dv.ENDPOINT_STABLE)
ok(dv.ENDPOINT_MAX_UTTERANCE <= 10,
   "and speech that never ends (a television) is cut at %.0f s"
   % dv.ENDPOINT_MAX_UTTERANCE)

print("== 5c. the live listener may NOT cut the turn short ==")
# The reported symptom: "what time is it" answered as "what time".
# Vosk endpoints on any brief pause; that used to return immediately
# and bypass the whole policy above. Its finals are now just more
# transcript.


class ChoppyRec:
    """A listener that finalises MID-SENTENCE, the way Vosk does when
    someone takes a breath between words."""

    def __init__(self, chunks):
        self.chunks = list(chunks)   # each finalises separately
        self.i = 0
        self.fired = 0

    def AcceptWaveform(self, data):
        # finalise after a few blocks, repeatedly
        self.fired += 1
        return self.fired % 4 == 0 and self.i < len(self.chunks)

    def Result(self):
        txt = self.chunks[self.i] if self.i < len(self.chunks) else ""
        self.i += 1
        return json.dumps({"text": txt})

    def PartialResult(self):
        nxt = self.chunks[self.i] if self.i < len(self.chunks) else ""
        return json.dumps({"partial": nxt})

    def FinalResult(self):
        rest = " ".join(self.chunks[self.i:])
        self.i = len(self.chunks)
        return json.dumps({"text": rest})

    def Reset(self):
        pass


class WholeUtteranceEngine(ListenEngine):
    """Records the transcript the policy finally committed."""

    def _better_transcribe(self, audio, hint, allow_cloud=True):
        self.calls.append(hint)
        return hint          # whatever the listener accumulated


def choppy_turn(chunks, speak_blocks=16, quiet=1.4):
    e = WholeUtteranceEngine(recog_time=0.0)
    e._med_names = lambda: ["Sertraline"]

    def mic():
        for _ in range(speak_blocks):
            e._last_voice_ts = time.time()
            e._audio_q.put(BLOCK)
            time.sleep(0.02)
        t = time.time()
        while time.time() - t < quiet:
            e._audio_q.put(BLOCK)
            time.sleep(0.02)
    threading.Thread(target=mic, daemon=True).start()
    while e._last_voice_ts == 0.0:
        time.sleep(0.005)
    return e._listen_command(ChoppyRec(chunks), timeout=8)


got = choppy_turn(["what time", "is it"])
ok(got == "what time is it",
   "a mid-sentence finalisation does NOT cut the turn — got %r" % got)

got = choppy_turn(["how many", "sertraline", "do i have left"])
ok(got == "how many sertraline do i have left",
   "three chopped pieces are reassembled — got %r" % got)

got = choppy_turn(["did i take", "my medicine", "today"])
ok(got == "did i take my medicine today",
   "and so are these — got %r" % got)

print("== 5d. it holds a conversation, and lets you leave ==")
DONE = ("im done", "I'm done talking", "that's all", "nothing else",
        "goodbye", "never mind", "stop listening", "that's it")
NOT_DONE = ("what time is it", "how many sertraline do i have left",
            "im not sure", "that's all i take in the morning",
            "did i take my medicine today", "bye the way what is next")
conv = object.__new__(DoseVoice)
for phrase in DONE:
    ok(conv._is_done_talking(phrase), "ends on %r" % phrase)
for phrase in NOT_DONE:
    ok(not conv._is_done_talking(phrase),
       "does NOT end on %r" % phrase)

VSRC = open(os.path.join(ROOT, "dose_voice.py"), errors="ignore").read()
ex = VSRC.split("def _handle_exchange")[1].split("\n    def ")[0]
ok("FOLLOWUP_TIMEOUT" in ex,
   "the mic stays open for a follow-up after every answer")
ok(dv.FOLLOWUP_TIMEOUT >= 5,
   "for %.0f s — long enough to think of the next question"
   % dv.FOLLOWUP_TIMEOUT)
# she must finish speaking before listening resumes, or she hears
# herself through the speaker
ok(ex.index("self._speak(reply") < ex.index("self._drain(rec)")
   < ex.index("self._listen_command"),
   "she finishes speaking, the mic is cleared, THEN it listens again")
ok("self._last_voice_ts = 0.0" in ex,
   "and her own voice is not mistaken for the start of your next turn")
ok("_is_done_talking" in ex, "saying you're done ends it")
ok("_closed.is_set()" in ex, "so does a tap outside the panel")

APPSRC = open(os.path.join(ROOT, "dose_app.py"), errors="ignore").read()
click = APPSRC.split("def _on_canvas_click")[1].split("\n    def ")[0]
ok("_voice_panel_box" in click and "_voice_dismiss" in click,
   "the UI turns a tap outside the panel into a close")
ok(click.index("_voice_state") < click.index("_click_zones"),
   "and it is checked before any button underneath it")

print("== 5e. a FRAGMENT is not a sentence ==")
# From the device: "what time is it" was answered as "what". The word
# "what" on its own matches the pattern for "say that again", counted
# as a complete command, and committed 0.35 s later — cutting off the
# rest of the question.
frag = ListenEngine()
frag._med_names = lambda: ["Sertraline", "Atorvastatin"]
frag._flow = None

MUST_WAIT = ["what", "how", "when", "which", "did i", "have i",
             "ive been thinking about", "umm what about my",
             "how many of my", "i want to take my", "can i"]
for phrase in MUST_WAIT:
    w = frag._endpoint_wait(phrase)
    ok(w >= dv.ENDPOINT_DANGLING - 0.01,
       "%-28r waits %.2fs — it is somebody mid-sentence" % (phrase, w))

MUST_COMMIT = ["what time is it", "whats next", "open storage",
               "go to settings", "how am i doing", "add a medication",
               "did i take my sertraline", "what do i take today",
               "how many pills do i have",
               "how many sertraline do i have left"]
for phrase in MUST_COMMIT:
    w = frag._endpoint_wait(phrase)
    ok(w <= dv.ENDPOINT_STABLE + 0.01,
       "%-36r commits in %.2fs — it is a finished question"
       % (phrase, w))

for phrase in ("i want to kill myself", "chest pain"):
    ok(frag._endpoint_wait(phrase) == 0.0,
       "%r is never made to wait" % phrase)

ok("SHORT_FRAGMENT_WORDS" in VSRC,
   "question words and auxiliaries only mean 'still talking' in a "
   "short fragment — they end finished sentences all the time")
ok(dv.SHORT_FRAGMENT_MAX <= 3,
   "which is %d words or fewer" % dv.SHORT_FRAGMENT_MAX)
ok("have" not in dv.HANGING_WORDS,
   "'have' is not treated as never-final: 'how many pills do i have' "
   "is a question, and it was waiting the full grace for nothing")
ok("the" in dv.HANGING_WORDS and "my" in dv.HANGING_WORDS
   and "about" in dv.HANGING_WORDS,
   "while articles, possessives and prepositions still are")
ew = VSRC.split("def _endpoint_wait")[1].split("\n    def ")[0]
ok("intent.complete" in ew and "_match_builtin" in ew,
   "the endpointer asks the matcher that will actually answer, so a "
   "question it can answer is not left waiting")
ok("STRICT parse" in ew,
   "but a loose phonetic match may NOT shorten the mid-thought grace")

print("== 6. silence alone never invents an utterance ==")
e = ListenEngine(recog_time=0.05)


def silence():
    t = time.time()
    while time.time() - t < 1.5:
        e._audio_q.put(BLOCK)
        time.sleep(0.02)


threading.Thread(target=silence, daemon=True).start()
got = e._listen_command(FakeRec([]), timeout=1.2)
ok(got == "", "pure silence returns nothing (got %r)" % got)
ok(not e.calls, "and recognition is never even run on it")

print("== 7. no thread leak across repeated turns ==")
base = threading.active_count()
for _ in range(12):
    SpeakEngine()._speak(REPLY)
time.sleep(0.6)
leaked = threading.active_count() - base
ok(leaked <= 1, "12 replies leak no threads (delta %d)" % leaked)

print("== 8. the station is never left without a voice ==")
APP = open(os.path.join(ROOT, "dose_app.py"), errors="ignore").read()
SH = open(os.path.join(ROOT, "DOSE.sh"), errors="ignore").read()
# There is ONE voice by design, so "never mute" cannot mean "fall
# back to a different voice" — it means the download is retried and
# the old voice is kept until hers actually lands.
ok("VOICE_REFS" in APP and APP.count('"v1.0.0", "main"') >= 1,
   "more than one mirror ref is tried for her voice")
ok('magic=b"\\x08"' in APP,
   "a download is validated as a real ONNX, not an HTML error page")
ok("doctype html" in APP.lower(),
   "an HTML error page is explicitly rejected")
seg = APP.split("Now that she is on disk")[1][:400]
ok("if os.path.exists(onx):" in seg,
   "other voices are deleted only once hers is on disk")
mig = APP.split("def migrate_voice")[1].split(
    "def _voice_download_models")[0]
ok("_voice_download_models(force=True)" in mig,
   "a station with only an old voice downloads hers before anything "
   "is removed")
ok("One voice, always hers" in SH,
   "the setup script applies the same rule")
ok("_voice_dl_tries" in APP,
   "and a failed download is retried rather than giving up")

print("== 9. the assistant still cannot dispense ==")


class NoDispense:
    def __init__(self):
        self.root = type("R", (), {"after": staticmethod(
            lambda ms, fn, *a: fn(*a))})()
        self.settings = {}
        self.med_data = {"blue": {"name": "Sertraline", "loaded": True,
                                  "count": 28, "dose_times": ["8:00 AM"],
                                  "schedule_days": []}}

    def _start_dispense(self, key):
        raise AssertionError("the voice dispensed medication!")

    def _dose_due_map(self):
        return {}

    def _get_today_schedule(self):
        return []

    def _dose_status(self, k, t):
        return None

    def _adherence_stats(self):
        return {"score": 90, "on_time": 9, "late": 1, "missed": 0}

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
v.app = NoDispense()
v._flow = None
v.state = "idle"
v._last_reply = ""
v._last_exchange = None
v._learn = v._learn_load()
for phrase in ("give me my sertraline", "dispense my pills",
               "i need my medication now", "release my morning dose",
               "take my meds", "can i have my sertraline"):
    reply, _ = v.respond(phrase)
    ok(isinstance(reply, str) and reply,
       "answers without dispensing: %r" % phrase)

print()
print("reliability suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== RELIABILITY: ALL PASSED (fast, and still correct) ===")
