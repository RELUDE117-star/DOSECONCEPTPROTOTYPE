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
ok(bt.index("_moonshine_transcribe") < bt.index("_whisper_transcribe"),
   "the fast model goes first, so the common case stays fast")
ok("self._usable(ms)" in bt,
   "its answer is only accepted if it actually means something")
ok("_whisper_transcribe" in bt,
   "otherwise the stronger model gets a turn on the SAME audio")
ok("if ms and not" in bt and "if wh:" in bt,
   "and if neither parses, whichever heard something is still "
   "returned so it can be matched or asked about")

pr = VSRC.split("def _probe_moonshine")[1].split("\n    def ")[0]
ok("small.en" in pr,
   "the stronger model is whisper small.en, an accuracy step up")
ok('compute_type="int8"' in pr, "in int8 so it fits a Pi 4")
ok("_whisper_prompt" in VSRC,
   "and it is told this cabinet's medication names, the same way "
   "the fast model is given key terms")

ms = VSRC.split("def _moonshine_v2")[1].split("\n    def ")[0]
ok(ms.index("MEDIUM_STREAMING") < ms.index("TINY_STREAMING"),
   "speech models are tried MOST ACCURATE first — the old order fell "
   "back to tiny, which is why 'storage' came back as noise")
ok("ACCURACY_ORDER" in ms, "deliberately, not by accident")
ok("tiny — the only one available" in ms,
   "and if tiny really is all there is, it says so on screen")

print()
print("accuracy suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== ACCURACY: ALL PASSED (right drug, or asks — never wrong) ===")
