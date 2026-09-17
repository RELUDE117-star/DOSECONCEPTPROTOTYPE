"""ACCURACY BATTERY — understanding the user, not just hearing them.

Speech recognition on a $10 USB mic will never be letter-perfect, so
the station is built so that IT DOESN'T HAVE TO BE. Two layers do the
work:

  * PHONETIC matching — a medication is matched on how it SOUNDS
    (metaphone) as well as how it is spelled, over every 1-3 word
    window. "sir tra leen", "sertra lean" and "certain lean" all land
    on Sertraline even though none of them is spelled like it.
  * REFUSING TO GUESS — when two medications are close, or nothing is
    close enough, the matcher returns a SUGGESTION ("did you mean
    Lisinopril?") or nothing at all. It never silently picks the wrong
    drug. For a medication device that is the property that matters:
    being unsure out loud is safe, being confidently wrong is not.

This suite is the regression net for both. Every case below is a
realistic mishear of the kind these recognisers actually produce.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dose_nlu as nlu                                     # noqa: E402

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


# a realistic cabinet: common drugs, including deliberately
# confusable pairs (Lisinopril/Lisdexamfetamine, Metformin/Metoprolol)
MEDS = ["Sertraline", "Atorvastatin", "Lisinopril", "Metformin",
        "Metoprolol", "Levothyroxine", "Amlodipine", "Omeprazole",
        "Gabapentin", "Hydrochlorothiazide", "Vitamin D"]

print("== 1. mishears still land on the right medication ==")
# (what the recogniser produced, what the user meant)
MISHEARS = [
    ("sir tra leen", "Sertraline"),
    ("sertra lean", "Sertraline"),
    ("certain lean", "Sertraline"),
    ("ay tor va statin", "Atorvastatin"),
    ("a torva statin", "Atorvastatin"),
    ("atorva stat in", "Atorvastatin"),
    ("met foreman", "Metformin"),
    ("met four min", "Metformin"),
    ("metfor min", "Metformin"),
    ("meto pro lol", "Metoprolol"),
    ("levo thy rox ine", "Levothyroxine"),
    ("levothy roxine", "Levothyroxine"),
    ("am lodi pine", "Amlodipine"),
    ("amlo dip een", "Amlodipine"),
    ("o mep ra zole", "Omeprazole"),
    ("oh mepra zole", "Omeprazole"),
    ("gaba pentin", "Gabapentin"),
    ("gabba pent in", "Gabapentin"),
    ("hydro chloro thigh a zide", "Hydrochlorothiazide"),
    ("vitamin dee", "Vitamin D"),
]
hits = 0
for heard, want in MISHEARS:
    med, sug, _ = nlu.match_med(heard, MEDS)
    got = med or sug
    if got == want:
        hits += 1
    ok(got == want,
       "%-28r -> %s (got %r)" % (heard, want, got))
print("    %d / %d mishears resolved" % (hits, len(MISHEARS)))

print("== 2. mishears inside a full sentence ==")
SENTENCES = [
    ("did i take my sir tra leen this morning", "Sertraline", "did_take"),
    ("how many met foreman do i have left", "Metformin", "pills_left"),
    ("when is my levo thy rox ine due", "Levothyroxine", "next_dose"),
    ("i need my gaba pentin", "Gabapentin", None),
]
for text, want_med, want_intent in SENTENCES:
    it = nlu.parse(text, MEDS)
    got = it.med or it.suggestion
    ok(got == want_med,
       "sentence %-42r -> %s (got %r)" % (text[:42], want_med, got))
    if want_intent:
        ok(it.name == want_intent,
           "sentence %-42r -> intent %s (got %s)"
           % (text[:42], want_intent, it.name))

print("== 3. NEVER confidently wrong: confusable pairs ==")
# These SOUND close to two different drugs. The only safe answers are
# the right one, or a suggestion the user confirms — never the other.
CONFUSABLE = [
    ("met o pro lol", "Metoprolol", "Metformin"),
    ("met for min", "Metformin", "Metoprolol"),
]
for heard, right, wrong in CONFUSABLE:
    med, sug, _ = nlu.match_med(heard, MEDS)
    ok(med != wrong, "%r is never silently read as %s" % (heard, wrong))
    ok((med or sug) == right, "%r resolves to %s" % (heard, right))

print("== 4. NEVER guesses at a drug that isn't in the cabinet ==")
# Nonsense, or a drug the user doesn't have, must produce no
# confident match. Answering about the wrong medication is the one
# failure mode this device cannot have.
NOT_MINE = ["ibuprofen", "warfarin", "insulin", "banana bread",
            "the weather tomorrow", "turn on the lights",
            "hydroxychloroquine", "prednisone"]
for phrase in NOT_MINE:
    med, sug, scores = nlu.match_med(phrase, MEDS)
    ok(med is None,
       "%-24r makes no confident match (got %r)" % (phrase, med))

print("== 5. asks instead of assuming, when it is unsure ==")
# A near-miss should come back as a SUGGESTION, so the station can say
# "did you mean X?" rather than acting on a coin flip.
NEAR = [("liz and oprah", "Lisinopril"),
        ("lie sin o pril", "Lisinopril")]
for heard, want in NEAR:
    med, sug, _ = nlu.match_med(heard, MEDS)
    ok((med or sug) == want,
       "%r reaches %s (confident=%r, suggested=%r)"
       % (heard, want, med, sug))

print("== 6. intents survive sloppy phrasing ==")
INTENTS = [
    ("what do i take today", "schedule"),
    ("what should i take today", "schedule"),
    ("whats on my schedule", "schedule"),
    ("what am i taking today", "schedule"),
    ("whats next", "next_dose"),
    ("when is my next dose", "next_dose"),
    ("when do i take the next one", "next_dose"),
    ("how many pills do i have left", "pills_left"),
    ("how many are left", "pills_left"),
    ("how am i doing", "adherence"),
    ("hows my adherence", "adherence"),
    ("did i take my sertraline", "did_take"),
    ("have i taken my metformin", "did_take"),
]
for text, want in INTENTS:
    got = nlu.parse(text, MEDS).name
    ok(got == want, "%-34r -> %s (got %s)" % (text, want, got))

print("== 7. medical questions are always gated, never answered ==")
# The station must never give medical advice. These MUST route to the
# medical gate no matter how they are phrased or mis-transcribed.
MEDICAL = [
    "should i take a double dose",
    "can i take two of these",
    "is it safe to take this with alcohol",
    "what are the side effects",
    "should i stop taking my sertraline",
    "can i skip my dose today",
    "is this medication bad for me",
    "what happens if i take too much",
    "can i drink with metformin",
    "should i take more",
]
for text in MEDICAL:
    got = nlu.parse(text, MEDS).name
    ok(got == "medical_question",
       "MEDICAL GATE: %-40r -> %s" % (text, got))

print("== 8. decoder loops are caught, not spoken back ==")
# Key-term biasing occasionally makes a recogniser stutter. A looped
# transcript must be rejected so we fall back to a clean model.
LOOPS = ["sertraline sertraline sertraline sertraline",
         "take take take take take take",
         "aaaaaaaaa",
         "the the the the the the the"]
for bad in LOOPS:
    ok(nlu.looks_hallucinated(bad), "rejects decoder loop %r" % bad[:36])
for good in ["what do i take today", "how many pills do i have left",
             "did i take my sertraline", "yes", "no",
             "vitamin d vitamin d"]:
    ok(not nlu.looks_hallucinated(good),
       "accepts clean transcript %r" % good)

print("== 9. yes / no are understood however they are said ==")
for y in ["yes", "yeah", "yep", "yup", "sure", "correct", "that's right",
          "ok", "okay", "affirmative"]:
    ok(nlu.is_yes(y), "yes: %r" % y)
for n in ["no", "nope", "nah", "negative", "that's wrong", "incorrect"]:
    ok(nlu.is_no(n), "no: %r" % n)
for y in ["yes", "yeah", "correct"]:
    ok(not nlu.is_no(y), "%r is not read as no" % y)

print("== 10. getting around by voice ==")
# Every screen answers to several names, because nobody knows ours.
import os as _os                                            # noqa: E402
import sys as _sys                                          # noqa: E402
import tempfile as _tf                                      # noqa: E402
_os.environ.setdefault("DOSE_VOICE_DIR", _tf.mkdtemp(prefix="dose_nav_"))
from datetime import datetime as _dt                         # noqa: E402
from dose_voice import DoseVoice                             # noqa: E402


class _Root:
    def after(self, _m, fn, *a):
        fn(*a)


class _App:
    def __init__(self):
        self.root = _Root()
        self.nav_to = None
        self.statuses = {}
        self.med_data = {
            "blue": {"name": "Sertraline", "loaded": True, "count": 28,
                     "dose_times": ["8:00 AM"],
                     "schedule_days": ["Mon", "Tue", "Wed", "Thu", "Fri",
                                       "Sat", "Sun"]},
            "yellow": {"name": "Atorvastatin", "loaded": True,
                       "count": 30, "dose_times": ["9:00 PM"],
                       "schedule_days": ["Mon", "Tue", "Wed", "Thu",
                                         "Fri", "Sat", "Sun"]},
            "demo": {"name": "Demo", "loaded": False, "count": 0}}
        self._demo_registered = False

    def _dose_due_map(self):
        return {}

    def _get_today_schedule(self):
        n = _dt.now()
        return [{"key": "blue", "name": "Sertraline", "time": "8:00 AM",
                 "count": 28, "sort": n.replace(hour=8, minute=0)},
                {"key": "yellow", "name": "Atorvastatin",
                 "time": "9:00 PM", "count": 30,
                 "sort": n.replace(hour=21, minute=0)}]

    def _dose_status(self, k, t):
        return self.statuses.get((k, t))

    def _adherence_stats(self):
        return {"score": 92, "on_time": 11, "late": 1, "missed": 0}

    def _start_dispense(self, k):
        raise AssertionError("voice must never dispense")

    def _nav(self, m):
        self.nav_to = m

    def _save_med(self):
        pass

    def _draw_frame(self):
        pass

    def _med_info_for(self, n):
        return ["Cholesterol (statin)", "Avoid grapefruit juice"]


_app = _App()
_v = object.__new__(DoseVoice)
_v.app = _app
_v._flow = None
_v.state = "idle"
_v._last_reply = ""
_v._last_exchange = None
_v._learn = _v._learn_load()


def says(phrase):
    _app.nav_to = None
    reply, _ = _v.respond(phrase)
    return reply, _app.nav_to


NAV = [
    ("go to settings", "settings"), ("open settings", "settings"),
    ("take me to settings", "settings"), ("show me the settings page",
                                          "settings"),
    ("go to user", "user"), ("go to the user page", "user"),
    ("open my profile", "user"), ("take me to my record", "user"),
    ("go to my stats", "user"), ("show me my progress", "user"),
    ("go to storage", "storage"), ("go to storage apps", "storage"),
    ("open the storage page", "storage"),
    ("go to the medication screen", "storage"),
    ("show me my medicine cabinet", "storage"),
    ("go home", "home"), ("open the home screen", "home"),
    ("take me back to the main screen", "home"),
]
for phrase, want in NAV:
    reply, went = says(phrase)
    ok(went == want, "%-38r -> %s screen (got %s)"
       % (phrase, want, went))

print("== 10b. navigation survives a MISHEARD screen name ==")
# The station must act on what you MEANT, not only on phrases it was
# given. These are what a far-field mic actually produces.
MISHEARD_NAV = [
    ("open storge", "storage"), ("open storidge", "storage"),
    ("open store age", "storage"), ("open sturge", "storage"),
    ("go to storge", "storage"), ("show me the storidge", "storage"),
    ("open settens", "settings"), ("open sittings", "settings"),
    ("go to setings", "settings"), ("open the setting page", "settings"),
    ("go to use er", "user"), ("open my profil", "user"),
    ("take me to my recerd", "user"), ("go to hoam", "home"),
]
for phrase, want in MISHEARD_NAV:
    reply, went = says(phrase)
    ok(went == want, "%-32r -> %s (got %s)" % (phrase, want, went))

# ...but it must not fire on things that merely rhyme
NOT_NAV = ["how many pills do i have left", "what time is it",
           "did i take my sertraline", "i want to kill myself",
           "chest pain", "what do i take today"]
for phrase in NOT_NAV:
    reply, went = says(phrase)
    ok(went in (None, "home"),
       "%-34r does not get read as a screen (got %s)" % (phrase, went))

import dose_nlu as _nlu                                      # noqa: E402
SCR = {"home": ("home", "main"), "storage": ("storage", "medication"),
       "settings": ("settings", "options"), "user": ("user", "profile")}
for noise in ("sink", "storm", "sausage", "garage", "water", "coffee",
              "sertraline", "metformin"):
    ok(_nlu.match_choice(noise, SCR) is None,
       "%-12r is not matched to a screen" % noise)

print("== 11. asking about today, however you phrase it ==")
TODAY = ["what medication do i need to take today",
         "what medicine do i need to take today",
         "what pills do i need to take today",
         "what do i take today",
         "which meds are due today",
         "what medications do i have today"]
for phrase in TODAY:
    reply, _ = says(phrase)
    ok("sertraline" in reply.lower() and "atorvastatin" in reply.lower(),
       "%-42r lists the whole day" % phrase)

print("== 12. 'did i take my medicine today' ==")
# "medicine" is not a drug name — it means everything due today.
_app.statuses.clear()
for phrase in ("did i already take my medicine today",
               "did i take my medication today",
               "have i taken my pills today",
               "am i caught up",
               "did i miss anything"):
    reply, _ = says(phrase)
    ok("not yet" in reply.lower() or "still to take" in reply.lower(),
       "%-40r -> what is outstanding" % phrase)
    ok("could not find" not in reply.lower(),
       "%-40r does not hunt for a drug called 'medicine'" % phrase)

_app.statuses[("blue", "8:00 AM")] = "taken"
reply, _ = says("did i take my medicine today")
ok("sertraline" in reply.lower() and "atorvastatin" in reply.lower(),
   "part-done says what IS logged and what is left: %r" % reply[:70])

_app.statuses[("yellow", "9:00 PM")] = "taken"
reply, _ = says("did i take my medicine today")
ok("yes" in reply.lower() and "outstanding" in reply.lower(),
   "all-done says so plainly: %r" % reply[:70])
_app.statuses.clear()

print("== 13. 'how do i take X' ==")
for phrase in ("how do i take atorvastatin",
               "how do i take my atorvastatin",
               "how should i take atorvastatin",
               "tell me about atorvastatin"):
    reply, _ = says(phrase)
    ok("grapefruit" in reply.lower(),
       "%-38r reads the label back" % phrase)
    ok("cannot give medical advice" in reply.lower()
       or "pharmacist" in reply.lower(),
       "%-38r still defers to the pharmacist" % phrase)

print("== 14. nonsense gets a SECOND OPINION, not a shrug ==")
# Reported from the device: saying "storage" came back as "(urk)" and
# the station answered "insufficient data". A recogniser handed poor
# audio does not return nothing — it returns confident nonsense. The
# fix is to notice the transcript means nothing and re-run the SAME
# audio through a stronger model, not to give up faster.
_v._med_names = lambda: ["Sertraline", "Atorvastatin"]

USABLE = ["open storage", "what time is it", "how many pills do i have "
          "left", "did i take my sertraline", "yes", "no",
          "sertraline", "what do i take today"]
for t in USABLE:
    ok(_v._usable(t), "usable transcript: %r" % t)

JUNK = ["urk", "", "a", "mmm hmm hmm hmm hmm hmm",
        "the the the the the the the", "zzzzzzzzzz"]
for t in JUNK:
    ok(not _v._usable(t),
       "recognised as meaningless, worth re-hearing: %r" % t)

VSRC = open(os.path.join(ROOT, "dose_voice.py"), errors="ignore").read()
bt = VSRC.split("def _better_transcribe")[1].split("\n    def ")[0]
ok("self._usable(ms)" in bt,
   "its answer is only accepted if it actually means something")
ok("_whisper_transcribe" in bt,
   "otherwise the stronger model gets a turn on the SAME audio")
ok("max(cands" in bt,
   "and if neither parses, the answer with MORE IN IT wins — a turn "
   "where the fast model said 'urk' and the slow one said 'open "
   "storage the' was reporting 'urk', throwing away the transcript "
   "the phonetic matcher could have used")

# prove it, on the real method
class _Pick(DoseVoice):
    def __init__(self, fast, slow):
        self._f, self._s = fast, slow
        self._med_names = lambda: ["Sertraline"]

    def _moonshine_transcribe(self, a):
        return self._f

    def _whisper_transcribe(self, a):
        return self._s

    def _write_wav(self, a):
        return None


for fast, slow, want in (("urk", "open storage the", "open storage the"),
                         ("open storage the", "urk", "open storage the"),
                         ("", "hello there friend", "hello there friend"),
                         ("hello there friend", "", "hello there friend")):
    got = _Pick(fast, slow)._better_transcribe(b"x" * 100, "")
    ok(got == want,
       "fast=%r slow=%r -> %r" % (fast, slow, got))

lw = VSRC.split("def _load_whisper")[1].split("\n    def ")[0]
ok("WHISPER_MODELS" in lw,
   "the stronger model comes from the configured chain")
ok('compute_type="int8"' in lw, "in int8 so it fits a Pi 4")
pr = VSRC.split("def _probe_moonshine")[1].split("\n    def ")[0]
ok("_whisper_loaded = False" in pr,
   "and it is NOT loaded at startup — most turns never need it, and "
   "a loaded model costs RAM on every boot")
ok("_load_whisper" in VSRC.split("def _whisper_transcribe")[1][:400],
   "it loads on first need instead")
ok("_whisper_prompt" in VSRC,
   "and it is told this cabinet's medication names, the same way "
   "the fast model is given key terms")

print("== 14b. Whisper does the hearing now ==")
# The HuggingFace audio course builds its assistant's transcription
# stage on Whisper (base.en on CPU, small.en as the upgrade). We had
# the small streaming recogniser running first, and when its package
# only shipped a tiny arch the station was listening with the weakest
# model it had.
import dose_voice as _dvm                                    # noqa: E402
ok(_dvm.WHISPER_MODELS[0] in ("base.en", "distil-small.en",
                              "small.en"),
   "the escalation model is %r — strong enough to be worth calling, "
   "small enough to answer before the person gives up"
   % _dvm.WHISPER_MODELS[0])
ok("tiny.en" in _dvm.WHISPER_MODELS[-1],
   "tiny is the LAST resort, not the first fallback")
ok(len(_dvm.WHISPER_MODELS) >= 3,
   "with a chain to fall back through if one will not download")
ok(bt.index("_moonshine_transcribe") < bt.index("_whisper_transcribe"),
   "the FAST model answers first — I had Whisper leading and on a Pi "
   "that is seconds per utterance, every utterance")
ok("_usable(ms)" in bt,
   "and the stronger model escalates only when that answer is "
   "unusable")
ok(_dvm.STT_THREADS > _dvm.INFER_THREADS,
   "transcription gets a bigger budget (%d) than continuous work "
   "(%d), because it is a short burst"
   % (_dvm.STT_THREADS, _dvm.INFER_THREADS))
ok(_dvm.STT_THREADS == max(1, _dvm.CPU_CORES - 1),
   "a bigger budget than background work, but a core stays free — "
   "the device measured a load average of 6.66 on four cores when "
   "speech held all of them")

ms = VSRC.split("def _moonshine_v2")[1].split("\n    def ")[0]
# The FAST model chooses between base and tiny only. I had it
# ordered biggest-first for accuracy, and the device measured
# "moonshine medium" taking 4.11 SECONDS on one word. Accuracy is
# Whisper's job — this one answers every sentence and must be quick.
ord_txt = ms.split("ACCURACY_ORDER = ")[1][:90]
ok("MEDIUM" not in ord_txt and "SMALL" not in ord_txt,
   "the fast model never falls into medium or small")
ok(ord_txt.index("BASE") < ord_txt.index("TINY"),
   "base preferred over tiny — the largest that stays quick here")
ok("ACCURACY_ORDER" in ms, "deliberately, not by accident")
ok("tiny — the only one available" in ms,
   "and if tiny really is all there is, it says so on screen")

print("== 15. it acts on what you MEANT, without hijacking ==")
# The phonetic command vocabulary runs dead last. An earlier version
# ran it sooner and turned "how many banana pills do I have left" into
# "opening storage" — which answered the wrong thing AND broke the
# teaching flow, where the station must say it does not have that one
# so you can correct it.
MISHEARD_CMD = [
    ("whats nekst", ("next", "nothing further")),
    ("what time izit", ("the time is",)),
    ("how am i doin", ("adherence", "percent")),
    ("pil count", ("sertraline",)),
    ("my adherance", ("adherence", "percent")),
    ("todays medicashun", ("sertraline",)),
    ("am i running lo", ("sertraline",)),
    ("did i take my medisin", ("still to take", "not yet", "logged")),
    ("my recerd", ("record",)),
    ("open storge", ("storage",)),
    ("go to setings", ("settings",)),
]
for phrase, wants in MISHEARD_CMD:
    _v._flow = None
    reply, _ = says(phrase)
    low = reply.lower()
    ok(any(w in low for w in wants)
       and not any(b in low for b in ("unclear", "insufficient",
                                      "did not copy", "didn't catch")),
       "%-24r -> %s" % (phrase, reply[:44]))

print("== 16. ...and never at the expense of naming a drug ==")
# An unknown drug name must still be reported as unknown, because
# that is what lets the user correct it.
for phrase in ("how many banana pills do i have left",
               "how many happy pills do i have left"):
    _v._flow = None
    reply, _ = says(phrase)
    ok("do not have" in reply.lower() or "not have" in reply.lower(),
       "%-40r still says it does not have that one" % phrase)
    ok("storage" not in reply.lower(),
       "%-40r is NOT turned into a screen change" % phrase)

# and nothing dangerous is ever matched phonetically
import dose_voice as _dvv                                    # noqa: E402
NEVER = ["sertraline", "atorvastatin", "metformin",
         "i want to kill myself", "chest pain",
         "should i take a double dose", "can i drink alcohol with this",
         "what are the side effects", "dispense my sertraline"]
for phrase in NEVER:
    ok(_nlu.match_choice(phrase, _dvv.COMMAND_VOCAB,
                         threshold=_dvv.VOCAB_THRESHOLD) is None,
       "%-34r is never matched to a command" % phrase)

ok(all(k.startswith("nav:") or k in {
    "time", "date", "remaining_today", "next_dose", "taken_today",
    "count", "adherence", "addmed"} for k in _dvv.COMMAND_VOCAB),
   "every command in the vocabulary is one the dispatcher can act on")

print("== 17. FUZZED vocabulary: broad, and still safe ==")
# Every phrase in the command vocabulary, damaged the way a recogniser
# damages speech — a dropped letter, a transposition, a missing word —
# three times each. This is how the leak below was found; it is not a
# hypothetical.
import random as _rnd                                        # noqa: E402
import dose_voice as _dv2                                    # noqa: E402
_rnd.seed(7)


def _mangle(phrase):
    w = phrase.split()
    r = _rnd.random()
    if r < 0.35 and len(w) > 2:
        w.pop(_rnd.randrange(len(w)))
    out = " ".join(w)
    i = _rnd.randrange(max(1, len(out) - 1))
    if r < 0.7:
        return out[:i] + out[i + 1:]
    return out[:i] + out[i + 1:i + 2] + out[i:i + 1] + out[i + 2:]


hit = tot = 0
for intent, phrases in _dv2.COMMAND_VOCAB.items():
    for phrase in phrases:
        for _ in range(3):
            tot += 1
            if _nlu.match_choice(_mangle(phrase), _dv2.COMMAND_VOCAB,
                                 threshold=_dv2.VOCAB_THRESHOLD) == intent:
                hit += 1
rate = 100.0 * hit / tot
print("    %d/%d damaged phrases still resolved (%.1f%%)"
      % (hit, tot, rate))
ok(rate >= 90.0,
   "%.1f%% of damaged phrasings still reach the right command" % rate)
ok(tot >= 300, "across %d variations of %d phrases"
   % (tot, sum(len(v) for v in _dv2.COMMAND_VOCAB.values())))

# THE LEAK THIS FOUND: "should i stop taking this" matched the
# SETTINGS screen — a question about stopping a medication, answered
# by opening a menu. Safety intents now block the phonetic matcher
# entirely.
SAFETY_PHRASES = [
    "should i stop taking this", "should i skip my dose",
    "can i take two", "what happens if i take too much",
    "can i drink alcohol with this", "what are the side effects",
    "is it safe to take this", "should i take more",
    "can i stop my metformin", "should i double up",
    "i want to kill myself", "i want to end my life",
    "chest pain", "i cant breathe", "i fell",
    "i dont feel well", "i feel dizzy",
]
for phrase in SAFETY_PHRASES:
    _v._flow = None
    _app.nav_to = None
    reply, went = says(phrase)
    low = reply.lower()
    ok(went in (None, "home"),
       "%-34r never becomes a screen change (got %s)" % (phrase, went))
    ok(any(w in low for w in ("pharmacist", "medical advice", "911",
                              "988", "lifeline", "emergency",
                              "not have to go", "how you are feeling",
                              "cannot give")),
       "%-34r gets its safety answer" % phrase)

src_v = open(os.path.join(ROOT, "dose_voice.py"), errors="ignore").read()
mb = src_v.split("def _match_builtin")[1].split("\n    def ")[0]
ok(mb.index('"medical_question", "crisis", "emergency", "unwell"')
   < mb.index("TRULY LAST"),
   "safety intents are checked BEFORE the phonetic matcher gets a "
   "turn — guessing is fine when the worst case is the wrong screen, "
   "and not fine here")

print("== 18. it answers small talk instead of failing ==")
# From the device: "1 of 8 understood". The conversation stays open
# for several seconds after every answer, so people fill that space
# the way they do with a person — and sixteen of twenty such words
# were coming back as "instruction unclear" and counted as failures.
# They are not failures.
BAD = ("unclear", "did not copy", "didn't catch", "insufficient")
SMALL = ["yeah", "yes", "ok", "okay", "thanks", "thank you", "hello",
         "hi", "no", "nope", "cool", "alright", "got it", "sure",
         "nice", "good", "bye", "never mind", "sorry", "um"]
for phrase in SMALL:
    _v._flow = None
    reply, _ = says(phrase)
    ok(not any(b in reply.lower() for b in BAD),
       "%-12r -> %s" % (phrase, reply[:34]))

# "bye" ends the conversation; "yeah" keeps it open
_v._flow = None
_, keep = _v.respond("bye")
ok(keep is False, "saying goodbye ends the conversation")
_v._flow = None
_, keep = _v.respond("yeah")
ok(keep is True, "an acknowledgement keeps it open")

# WHOLE phrases only — small talk must never swallow a real sentence
for phrase in ("no i meant the blue one",
               "yes i took my sertraline this morning",
               "ok what do i take today",
               "good how many pills do i have left"):
    ok(_v._match_small_talk(phrase) is None,
       "%-38r is NOT small talk" % phrase)

# and it must not outrank a confirmation inside a flow
VS3 = open(os.path.join(ROOT, "dose_voice.py"), errors="ignore").read()
resp = VS3.split("def respond")[1]
ok(resp.index("_match_small_talk")
   > resp.index("self._dispatch(intent_id, arg)"),
   "small talk is checked AFTER every real command has had its "
   "chance, so 'no' in a confirmation still means no")
ok("_match_small_talk" in VS3 and "SMALL_TALK" in VS3,
   "matched from a fixed vocabulary, not guessed")

print()
print("accuracy suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== ACCURACY: ALL PASSED (right drug, or asks — never wrong) ===")
