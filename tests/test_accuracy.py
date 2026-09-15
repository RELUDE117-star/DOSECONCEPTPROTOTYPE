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

print()
print("accuracy suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== ACCURACY: ALL PASSED (right drug, or asks — never wrong) ===")
