#!/usr/bin/env python3
"""The conversational layer may never state a fact.

Ryan asked for a station that can be talked to:

    "What if it just wants to talk and say how are you. It should be
     able to handle any conversation. And respond like a human if you
     want to continue talking"

and, in the same breath, for the thing that makes that safe:

    "don't let it trick you. Because if it says nothing and the
     response is nothing it might seem like it answered but really it
     was a null value"

So this file asserts two properties and they pull against each other:

  1. It ANSWERS. "how are you" must not fall through to "I didn't
     catch that" — the station heard him perfectly and saying
     otherwise is a lie.
  2. It NEVER ANSWERS ABOUT MEDICATION. Not a number, not a drug
     name, not a time. Those come from the Pi, which owns the data.

The second is checked over the whole corpus, not over the examples I
happened to think of, because the failure mode is a line added months
from now that quietly mentions a dose.

Run:  python3 tests/test_reply.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import dose_reply as R                                       # noqa: E402

FAILURES = []
CHECKS = [0]


def check(label, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


print("\n── NOT ONE FACT, ANYWHERE IN THE CORPUS ────────────────────")
CORP = R.corpus()
check("the corpus is not empty", len(CORP) > 20, len(CORP))
bad_digits = [c for c in CORP if re.search(r"\d", c)]
check("no reply contains a digit", not bad_digits, bad_digits)

# Every medication word the module itself knows about. If the guard
# learns a new drug name, this check learns it too — which is the
# point of reading it off the same pattern.
# The STRONG list only. Temporal words like "morning" live in
# CONTEXT_WORDS, and "Good morning, Ryan." is a greeting, not a
# medication claim — banning them from the corpus banned hello.
MEDWORDS = re.findall(r"[a-z]{3,}", R.MED_WORDS.pattern)
leaks = []
for line in CORP:
    for w in MEDWORDS:
        if re.search(r"\b%s\b" % w, line, re.I):
            leaks.append((line, w))
check("no reply uses a medication word",
      not leaks,
      leaks[:4])

# A reply that sounds like a measurement, even without a digit.
CLAIMY = re.compile(
    r"\b(none\s+left|all\s+gone|you\s+(have|took|need|should\s+take)|"
    r"your\s+(dose|next)|it'?s\s+(due|time)|remaining|milligram)\b", re.I)
claims = [c for c in CORP if CLAIMY.search(c)]
check("no reply reads as a claim about his medication", not claims, claims)

print("\n── AND IT DOES NOT IMPROVISE OVER A MEDICATION QUESTION ────")
MEDICAL = [
    "what do I take today",
    "how many pills do I have left",
    "did I take my aspirin today",
    "what time is my next dose",
    "how are my pills doing",          # contains "how are"
    "hey what about my medication",    # contains "hey"
    "thanks but when is my next dose",  # contains "thanks"
    "good morning what do I take",     # contains "good morning"
    "are you there did I take my pills",
    "is it 8 o'clock yet",             # a digit
    "should I take two of them",
    "what's my dosage",
    "am I due for anything",
    "how much metformin is left",
]
for phrase in MEDICAL:
    say, kind, why = R.compose(phrase)
    check("defers: %r" % phrase[:38],
          kind == "defer" and say == "",
          "got kind=%s say=%r (%s)" % (kind, say, why))

print("\n── IT ANSWERS THE THINGS HE ACTUALLY ASKED ABOUT ───────────")
CONVERSATION = [
    ("how are you", "how_are_you"),
    ("how are you doing", "how_are_you"),
    ("how's it going", "how_are_you"),
    ("hello", "greeting"),
    ("hey", "greeting"),
    ("good morning", "greeting_morning"),
    ("good evening", "greeting_evening"),
    ("thank you", "thanks"),
    ("thanks", "thanks"),
    ("who are you", "who_are_you"),
    ("what's your name", "who_are_you"),
    ("are you there", "are_you_there"),
    ("can you hear me", "are_you_there"),
    ("never mind", "never_mind"),
    ("goodbye", "goodbye"),
    ("good job", "praise"),
    ("sorry", "sorry"),
    ("I'm good", "im_fine"),
    ("what's the weather", "out_of_scope"),
    ("tell me a joke", "out_of_scope"),
]
for phrase, expect in CONVERSATION:
    say, kind, why = R.compose(phrase)
    check("answers: %-24r -> %s" % (phrase[:24], expect),
          kind == "chat" and why == expect and say.strip(),
          "got kind=%s why=%s say=%r" % (kind, why, say))

print("\n── A NULL ANSWER IS NEVER A CHAT ANSWER ────────────────────")
# His words: "if it says nothing and the response is nothing it might
# seem like it answered but really it was a null value".
for empty in ("", "   ", None):
    say, kind, why = R.compose(empty)
    check("empty input is 'none', not a reply: %r" % (empty,),
          kind == "none" and say == "")
check("kind is never 'chat' with an empty line",
      all(R.compose(p)[0].strip() or R.compose(p)[1] != "chat"
          for p, _ in CONVERSATION))

print("\n── VARIETY THAT THE CACHE CAN STILL HOLD ───────────────────")
# Variety is worthless if it costs a Piper render. Every line the
# composer can emit must be in corpus(), which is what the station
# pre-renders. A line outside it is 2.2-3.4s on the critical path.
seen = set()
for name, pat, replies in R.CHAT:
    for r in replies:
        seen.add(r)
missing = [r for r in seen if r not in CORP]
check("every possible reply is in corpus()", not missing, missing)
check("...and corpus() invents nothing extra",
      all(c in seen for c in CORP))
check("there is more than one way to say each thing",
      all(len(r) >= 2 for _n, _p, r in R.CHAT),
      [n for n, _p, r in R.CHAT if len(r) < 2])

# Cycled, not random: two identical turns must be reproducible.
R._SEQ.clear()
a = [R.compose("hello")[0] for _ in range(6)]
R._SEQ.clear()
b = [R.compose("hello")[0] for _ in range(6)]
check("the sequence is deterministic", a == b, (a, b))
check("...and does not repeat back to back",
      all(a[i] != a[i + 1] for i in range(len(a) - 1)), a)

print("\n── FIRST-CHUNK LENGTH: THESE ARE ON THE CRITICAL PATH ──────")
# TTS_FIRST_CHUNK_MAX is 42. A reply whose first chunk is longer than
# that gets split, and a split fragment is a cache key of its own —
# one more thing to get wrong. Keeping chat lines short sidesteps it.
longest = max(CORP, key=len)
check("no chat line is long enough to need splitting badly",
      len(longest) <= 60, "%d chars: %r" % (len(longest), longest))

print("\n── THE GUARD IS FIRST, AND THAT IS THE SAFETY ARGUMENT ─────")
SRC = open(os.path.join(ROOT, "tools", "dose_reply.py"),
           encoding="utf-8").read()
body = SRC.split("def compose(")[1]
check("compose() checks is_medical before matching any reply",
      body.index("is_medical(") < body.index("for name, pat, replies"),
      "one reordering and the module starts improvising over doses")
check("a digit alone is enough to defer",
      "HAS_DIGIT" in SRC and "HAS_DIGIT.search" in
      SRC.split("def is_medical(")[1][:700])
check("a strong medication word is never exempt",
      "MED_WORDS.search(t) or HAS_DIGIT.search(t)" in SRC,
      "the greeting exemption must not reach the words that matter")
check("only a BARE greeting escapes the temporal words",
      "PURE_GREETING.match(t)" in SRC and "^[" in R.PURE_GREETING.pattern
      and R.PURE_GREETING.pattern.rstrip().endswith("$"),
      "unanchored, this would exempt any sentence containing hello")
check("the module reaches no network and runs no command",
      not any(w in SRC for w in ("urllib", "socket", "requests",
                                 "subprocess", "http")))
check("'defer' and 'none' are distinguished on purpose",
      '"defer"' in SRC and '"none"' in SRC
      and "A model added later goes on" in SRC)

print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("REPLY: it can talk, and it cannot make anything up")
