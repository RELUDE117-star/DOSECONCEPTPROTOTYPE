#!/usr/bin/env python3
"""What to say when nobody wrote a rule for it.

WHY THIS EXISTS
---------------
Ryan, watching the station answer four scripted questions perfectly:

    "That's it's super fast even for like normal questions. What if it
     just wants to talk and say how are you. It should be able to
     handle any conversation. And respond like a human if you want to
     continue talking"

He is describing the real gap. The station matches a fixed list of
intents, and everything off that list gets

    "I didn't catch that, Ryan."

which is not true. It caught him exactly. It heard every word, on a
Mac, in under a second, and then had nothing to say — so it blamed
its own hearing. A machine that says "pardon?" when it understood
you perfectly well is worse than one that is slow.

THE ONE RULE THIS MODULE OBEYS
------------------------------
**It never states a fact.** No number, no medication name, no time,
no date, ever — not from a model, not from a template, not from the
context it is given. Facts about what Ryan takes and when belong to
the Pi, which owns the data, and they stay there.

This module handles the other half: hello, how are you, thank you,
are you there, what are you, never mind. Conversation, and nothing
else. When a sentence is even slightly about medication it hands the
turn back (`defer`) rather than improvising, and it prefers handing
back a turn it could have taken over taking one it should not have.

`tests/test_reply.py` asserts that property over the entire corpus
rather than trusting this paragraph — every line this module can say
is checked for digits and for anything that reads as a claim.

WHY THE REPLIES ARE A CLOSED LIST AND NOT A MODEL
-------------------------------------------------
Because of the reply cache, which is the single reason this station
answers in under a second at all:

    speak 0.00   <- the first words were rendered minutes ago

A reply invented fresh every time cannot be in that cache, so it
costs a Piper render on the critical path — measured at **2.2-3.4
seconds** inside the app. A more human-sounding station that takes
three seconds to start talking is not more human, it is worse at the
one thing it is for.

So variety comes from a CLOSED, DECLARED set: several ways to say
each thing, cycled deterministically so the same words do not come
back twice in a row, and every one of them pre-rendered at startup.
`corpus()` is what the prewarm renders, and it is generated from the
same tables the composer picks from — two lists would drift, and the
failure would be silent and slow rather than loud.

A local model on the Mac can be dropped in later for the long tail;
`compose()` returns `kind="none"` exactly where one would go. It
will pay the render cost, so it belongs on the rare path, never on
"how are you".

WHERE IT RUNS, AND WHY BOTH
---------------------------
On the MAC, which composes the reply and returns it attached to the
transcript in the same HTTP response — so it is free. A second round
trip to ask what to say would cost more than the whole language
layer currently does (`think: 0.00`).

And on the PI, as the same module, as a fallback. A closed laptop
already costs the station its good recogniser; it must not also cost
it the ability to be spoken to like a person. This is regex over a
short string — it costs nothing to carry in both places, and one
module in one file is the only way the two ends cannot disagree.
"""
import re

# ── what this module must NEVER answer ──────────────────────────────
#
# Checked FIRST, before anything else, and deliberately greedy. A
# false "defer" costs a scripted reply the Pi was going to give
# anyway; a false "chat" means this module improvised over a
# medication question. Those are not comparable mistakes.
#
# "how are my pills" contains "how are". "what's up with my dose"
# contains "what's up". Ordering is the whole safety argument.
# STRONG: these words are about medication, full stop. No context
# makes "how many pills" a chat line.
MED_WORDS = re.compile(
    r"\b("
    r"pill|pills|dose|doses|dosage|med|meds|medication|medications|"
    r"medicine|prescription|tablet|tablets|capsule|capsules|"
    r"milligram|milligrams|mg|"
    r"take|taken|taking|took|swallow|"
    r"refill|refills|remaining|remain|supply|inventory|stock|"
    r"adherence|missed|skip|skipped|dispense|cabinet|bottle|"
    r"aspirin|ibuprofen|tylenol|advil|insulin|metformin|"
    r"atorvastatin|lisinopril|amlodipine|omeprazole|levothyroxine"
    r")\b", re.I)

# WEAK: these mean medication IN CONTEXT. "what do I take in the
# morning" is a medication question; "good morning" is a greeting.
# Keeping them in the same list as the strong words is what made the
# station answer "good morning" with silence — see PURE_GREETING.
CONTEXT_WORDS = re.compile(
    r"\b(schedule|scheduled|due|next|today|tonight|morning|evening|"
    r"afternoon|time|clock|when|left|hours?|minutes?)\b", re.I)

# Kept as the union, for anything that just wants "is this his
# business or mine".
MED_TELLS = re.compile(
    "(%s)|(%s)" % (MED_WORDS.pattern, CONTEXT_WORDS.pattern), re.I)

# Anything with a digit in it is a question about a quantity or a
# time, whatever else it looks like.
HAS_DIGIT = re.compile(r"\d")

# ── THE ONE EXEMPTION, AND IT IS NARROW ─────────────────────────────
#
# "morning" and "evening" are in CONTEXT_WORDS because "what do I
# take in the morning" is a medication question. That also made
# **"good morning"** one, and the station answered a greeting with
# silence. The test caught it, which is the whole reason the corpus
# is checked by machine rather than by me reading it.
#
# The exemption is NOT "ignore the guard for greetings". It is: an
# utterance that is NOTHING BUT a greeting, anchored at both ends,
# with nothing else in it. "Good morning" passes. "Good morning, what
# do I take?" does not — it carries a strong word anyway, and even
# without one it is not only a greeting.
#
# Strong words and digits are never exempt. This buys back exactly
# the eight or nine phrases a person opens with and nothing else.
PURE_GREETING = re.compile(
    r"^[\s,.!?-]*"
    r"(good\s*(morning|afternoon|evening|night)|mornin[g']?|"
    r"evenin[g']?|night\s*night|hello|hi|hey|howdy|yo)"
    r"([\s,.!?-]*(ryan|there|again|everyone))?"
    r"[\s,.!?-]*$", re.I)

# ── the conversation ────────────────────────────────────────────────
#
# (name, pattern, replies). ORDER MATTERS: the first match wins, so
# the specific sits above the general.
#
# Every reply here is short on purpose. The first chunk is the only
# part on the critical path, and a short line is one chunk.
#
# None of these states a fact. That is not a style choice, it is the
# rule at the top of this file, and test_reply.py enforces it.
CHAT = [
    ("greeting_morning",
     r"\b(good\s*morning|mornin[g']?)\b",
     ["Good morning, Ryan.",
      "Morning, Ryan.",
      "Good morning. I'm here."]),

    ("greeting_evening",
     r"\b(good\s*(evening|night)|evenin[g']?|night\s*night)\b",
     ["Good evening, Ryan.",
      "Evening, Ryan.",
      "Good evening. I'm right here."]),

    ("greeting",
     r"\b(hello|hi|hey|howdy|yo)\b",
     ["Hello, Ryan.",
      "Hi, Ryan.",
      "Hey. I'm listening."]),

    # "how are you" and its cousins. Kept above the general question
    # patterns and below the medication guard.
    ("how_are_you",
     r"\b(how(\s+are|'?re)\s+(you|ya|u)|how\s+you\s+doing|"
     r"how'?s\s+it\s+going|how\s+have\s+you\s+been|you\s+(doing\s+)?o?k(ay)?)\b",
     ["I'm doing well, thank you. How are you?",
      "I'm good, Ryan. How about you?",
      "All well here. How are you doing?"]),

    ("im_fine",
     r"\b(i'?m\s+(good|fine|ok|okay|well|alright|great)|"
     r"(pretty\s+)?good\s+thanks|not\s+bad|can'?t\s+complain)\b",
     ["Glad to hear it.",
      "Good. I'm glad.",
      "That's good to hear."]),

    ("im_not_fine",
     r"\b(i'?m\s+(not\s+(good|well|ok|okay)|tired|exhausted|sad|"
     r"lonely|bored)|rough\s+day|bad\s+day|long\s+day)\b",
     # Careful here. This is sympathy, not advice, and not a question
     # that demands anything back.
     ["I'm sorry to hear that. I'm right here.",
      "That sounds like a lot. I'm here.",
      "Sorry, Ryan. I'm right here with you."]),

    ("thanks",
     r"\b(thank\s*(you|s)|thanks|thx|cheers|appreciate\s+it)\b",
     ["You're welcome, Ryan.",
      "Any time.",
      "Of course."]),

    ("sorry",
     r"\b(sorry|my\s+(bad|mistake)|apolog)",
     ["No need to apologise.",
      "That's alright.",
      "Nothing to be sorry for."]),

    ("praise",
     r"\b(good\s+(job|work|girl|one)|well\s+done|nice\s+(job|work|one)|"
     r"you'?re\s+(great|awesome|amazing|the\s+best)|love\s+(it|you))\b",
     ["Thank you, Ryan.",
      "That's kind of you.",
      "Glad it's working for you."]),

    ("frustration",
     r"\b(stupid|useless|dumb|annoying|hate\s+this|shut\s+up|"
     r"come\s+on|seriously)\b",
     # No defensiveness and no apology spiral. Acknowledge, offer,
     # stop talking.
     ["Understood. I'll keep it brief.",
      "Fair enough. I'm listening.",
      "Alright. Go ahead."]),

    ("who_are_you",
     r"\b(who\s+are\s+you|what\s+are\s+you|what'?s\s+your\s+name|"
     r"your\s+name)\b",
     ["I'm your station, Ryan.",
      "I'm the station here on your counter.",
      "Just your station, Ryan. Nothing clever."]),

    ("what_can_you_do",
     r"\b(what\s+can\s+you\s+do|how\s+do(es)?\s+(you|this)\s+work|"
     r"what\s+do\s+you\s+do|help\s+me\s+out|can\s+you\s+help)\b",
     # Deliberately vague about specifics: naming capabilities here
     # would be stating facts, and the Pi is the one that knows what
     # it can actually do today.
     ["Just ask me out loud. I'm listening.",
      "Talk to me normally and I'll do what I can.",
      "Ask me anything. I'll tell you what I know."]),

    ("are_you_there",
     r"\b(are\s+you\s+there|you\s+there|can\s+you\s+hear\s+me|"
     r"hello\s+hello|are\s+you\s+awake|are\s+you\s+on|you\s+up)\b",
     ["I'm here, Ryan.",
      "Right here. Go ahead.",
      "I'm listening."]),

    ("never_mind",
     r"\b(never\s*mind|nevermind|forget\s+it|no\s+thanks|"
     r"nothing|that'?s\s+all|we'?re\s+done|all\s+good)\b",
     ["Alright.",
      "No problem.",
      "Okay, Ryan."]),

    ("goodbye",
     r"\b(bye|goodbye|see\s+you|talk\s+(to\s+you\s+)?later|"
     r"i'?m\s+off|heading\s+out|going\s+to\s+bed)\b",
     ["Goodbye, Ryan.",
      "See you later.",
      "Talk to you soon, Ryan."]),

    ("yes",
     r"^\s*(yes|yeah|yep|yup|sure|of\s+course|correct|right)\b",
     ["Alright.",
      "Understood.",
      "Okay."]),

    ("no",
     r"^\s*(no|nope|nah|not\s+really)\b",
     ["Understood.",
      "Alright then.",
      "Okay, Ryan."]),

    # The honest ones. A station that says "I don't know" about the
    # weather is more trustworthy about a dose than one that guesses.
    ("out_of_scope",
     r"\b(weather|forecast|rain|temperature\s+outside|news|headlines|"
     r"sports|score|game|stock|joke|sing|music|play\s+something|"
     r"traffic|recipe|movie|tv)\b",
     ["That's outside what I know, Ryan.",
      "I can't help with that one.",
      "I don't know about that, Ryan."]),
]

_COMPILED = [(n, re.compile(p, re.I), r) for (n, p, r) in CHAT]

# Cycled, not random. A station whose phrasing depends on a random
# number says different things on two identical turns, which makes
# every transcript of a test run unreproducible for the sake of
# variety nobody asked for.
_SEQ = {}


def corpus():
    """EVERY line this module can say, for the reply cache to render.

    Generated from the tables above rather than written out again.
    Two lists drift, and the way this one would fail is silent: a
    line the composer produces that the cache does not hold is
    rendered from scratch, on the critical path, for the rest of the
    station's life. Nobody would notice except as "it got slower
    sometimes".
    """
    out = []
    for _n, _p, replies in CHAT:
        for r in replies:
            if r not in out:
                out.append(r)
    return out


def is_medical(text):
    """Whether this belongs to the Pi. Deliberately over-inclusive."""
    t = (text or "").strip()
    if not t:
        return False
    # Strong words and digits: always his, never negotiable.
    if MED_WORDS.search(t) or HAS_DIGIT.search(t):
        return True
    if CONTEXT_WORDS.search(t):
        # A bare "good morning" is a greeting. Anything more than a
        # bare greeting that carries a time word is his.
        return not PURE_GREETING.match(t)
    return False


def compose(text, seq=None):
    """What to say, or an honest admission that this is not ours.

    Returns (say, kind, why):

        ("Hello, Ryan.", "chat",  "greeting")    say exactly this
        ("",             "defer", "medical")     the Pi answers
        ("",             "none",  "no match")    nobody has a line

    `kind` is the contract and the Pi acts on it. "defer" and "none"
    are different on purpose: "defer" means this module recognised
    the subject and declined it, "none" means it did not recognise
    anything. A model added later goes on "none" and must never be
    reached by "defer".
    """
    t = (text or "").strip()
    if not t:
        return "", "none", "nothing was said"

    # FIRST, ALWAYS. See MED_TELLS.
    if is_medical(t):
        return "", "defer", "medical"

    for name, pat, replies in _COMPILED:
        if pat.search(t):
            i = _SEQ.get(name, 0) if seq is None else int(seq)
            _SEQ[name] = (i + 1) % len(replies)
            return replies[i % len(replies)], "chat", name

    return "", "none", "no match"
