"""DOSE NLU — transcript → intent (+ medication), deterministic and fast.

Ported from the DOSE Voice Assistant build guide (offline-first design).

Why rules instead of an LLM here: a medication device has a small set of
things people ask, and every one must be understood the same way every
time. The phonetic medication matcher handles mishears ("liz and oprah"
-> Lisinopril, "metaprologue" -> Metoprolol) and REFUSES TO GUESS when
two names are close — it asks "Did you mean…?" instead, which is the
safe behaviour for a device that gates real pills.

Pure Python + rapidfuzz + jellyfish; ~1 ms per utterance on a Pi 4.
Degrades gracefully: if rapidfuzz/jellyfish are missing, the matcher
falls back to difflib and still works.
"""
import os
import re
from dataclasses import dataclass, field

try:                                    # preferred: fast + phonetic
    import jellyfish
    from rapidfuzz import fuzz
    _HAVE_FUZZ = True
except Exception:                       # graceful fallback
    import difflib
    jellyfish = None
    _HAVE_FUZZ = False

    class _Fuzz:
        @staticmethod
        def ratio(a, b):
            return difflib.SequenceMatcher(None, a, b).ratio() * 100

    fuzz = _Fuzz()


def _env(name, default, cast=float):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    try:
        return cast(v)
    except Exception:
        return default


# Matching thresholds (measured defaults from the build guide).
MED_MATCH_THRESHOLD = _env("MED_MATCH_THRESHOLD", 72.0)   # accept outright
MED_MATCH_MARGIN = _env("MED_MATCH_MARGIN", 12.0)         # ...and beat runner-up by this
MED_SUGGEST_THRESHOLD = _env("MED_SUGGEST_THRESHOLD", 65.0)   # "Did you mean X?"
MED_SUGGEST_MARGIN = _env("MED_SUGGEST_MARGIN", 10.0)

# Order matters: first match wins. Safety intents are checked FIRST.
_PATTERNS = [
    ("crisis", r"\b(kill(ing)? myself|end (it all|my life)|want(ed)? to die|don'?t want to (live|be here)|"
               r"suicid\w*|hurt(ing)? myself|harm(ing)? myself|take (them|it) all|take all (of )?(my|the|them)|"
               r"overdose|no reason to live|better off (dead|without me))\b"),
    ("emergency", r"\b(chest pain|can'?t breathe|cannot breathe|trouble breathing|i (fell|have fallen)|"
                  r"stroke|passing out|pass out|call (911|an ambulance|for help))\b"),
    ("cancel", r"^(no|nope|stop|cancel|never ?mind|forget it|quiet|be quiet|that'?s all)\b"
               r"|\b(cancel (that|it)|stop talking)\b"),
    ("repeat", r"\b(say (that|it) again|repeat (that|it)?|what did you say|come again|pardon)\b|^what\??$"),
    ("medical_question", r"\b(side effects?|interact\w*|alcohol|drink(ing)? with|with food|empty stomach|"
                         r"is it (ok|okay|safe|bad|harmful|dangerous)|"
                         r"is (this|that|it|my \w+) (medication |medicine |drug )?(bad|safe|harmful|dangerous|ok|okay)|"
                         r"should i (stop|skip|double|not take|keep taking|wait)|"
                         r"should i take (more|less|another|two|it with|them with|extra)|"
                         r"double (up|dose)|"
                         r"can i (take|have) (two|more|another|an extra|extra|a double|double)|"
                         r"can i (skip|stop|miss|halve|split|crush)|"
                         r"what happens if i (take|miss|skip|stop|double)|"
                         r"(is|are) (it|they|these|those|this) (bad|safe|harmful|addictive)|"
                         r"missed (a|my) dose|what (is|does) \w+ (for|do)|how much should|"
                         r"can i take .* with|pregnan\w*|dosage|overdos\w*|too much)\b"),
    ("unwell", r"\b(don'?t feel (well|good)|feel(ing)? (sick|dizzy|bad|awful)|dizzy|nause\w*|throwing up)\b"),
    ("taken_today", r"\b(what (have|did) i (already )?(taken|take|had)|what'?s been taken|taken today|"
                    r"(did|have) i (already )?(take|taken|had) (my |the |all (of )?my )?"
                    r"(medicines?|medications?|meds?|pills?|tablets?|doses?|everything)|"
                    r"am i (up to date|caught up)|did i miss anything|"
                    r"my history)\b"),
    ("did_take", r"\b(did i (already )?take|have i (already )?taken|did i have|have i had)\b"),
    ("next_dose", r"\b(what'?s next|what is next|next (dose|pill|one|medication|med)|"
                  r"when (do|should) i take|when is my|what time (do|should) i take)\b"),
    ("adherence", r"\b(how (am|have) i (been )?doing|how'?s it going with my|"
                  r"my (adherence|score|streak|stats|record)|adherence|"
                  r"am i (doing (ok|okay|well|good|alright)|on track|keeping up)|"
                  r"have i been (good|consistent|taking them))\b"),
    ("pills_left", r"\b(how many (\w+ )*(left|remaining)|running (low|out)|refill|left in)\b"),
    ("dispense", r"\b(dispense|release|give me|i need my|can i (have|get)|time for my|ready for my|"
                 r"take my (pills?|meds?|medicine|medication|dose)|"
                 r"my (morning|evening|night|bedtime) (pills?|meds?|dose))\b"),
    ("schedule", r"\b(what ((medications?|medicines?|meds?|pills?|drugs?|doses?|tablets?) )?(do|should) i "
                 r"(need to )?(take|have)( today| this morning| tonight| now)?|"
                 r"what are my (medications?|medicines?|meds?|pills?)|what do i take|"
                 r"what am i (taking|on|due for)|what'?s due|what is due|"
                 r"anything (to take|due)|"
                 r"my schedule|today'?s (doses?|medications?|pills?))\b"),
    ("time", r"\b(what time is it|what'?s the time|what is the time|tell me the time)\b"),
    ("help", r"\b(what can you do|help( me)?$|how do(es)? (this|you) work)\b"),
    ("thanks", r"\b(thanks?|thank you|appreciate it)\b"),
    ("greeting", r"^(hi|hello|hey|good (morning|afternoon|evening))\b"),
]
_COMPILED = [(name, re.compile(p)) for name, p in _PATTERNS]

YES = re.compile(
    r"^(yes|yeah|yea|yah|yep|yup|correct|right|ok|okay|okey|k|"
    r"that'?s (it|right|correct)|sure|please do|go ahead|do it|"
    r"affirmative|absolutely|definitely|of course|indeed|"
    r"uh huh|mm hmm|mhm)\b")
NO = re.compile(
    r"^(no|nope|nah|naw|not that|not quite|not right|wrong|incorrect|"
    r"neither|negative|don'?t|do not|never ?mind|"
    r"that'?s (wrong|not (it|right))|cancel)\b")

NEEDS_MED = {"did_take"}                       # can't answer without a med
OPTIONAL_MED = {"dispense", "pills_left", "next_dose"}


@dataclass
class Intent:
    name: str
    text: str
    med: str = None                 # confident match
    suggestion: str = None          # "did you mean ...?"
    scores: list = field(default_factory=list)

    @property
    def complete(self):
        """True when acting on this now can't be premature (early-commit)."""
        if self.name == "unknown":
            return False
        if self.name in NEEDS_MED:
            return self.med is not None
        if self.name == "dispense":
            return self.med is not None or bool(
                re.search(r"(pills?|meds?|medicine|medication|dose)\b", self.text))
        return True


def normalize(text):
    t = (text or "").lower().replace("’", "'")
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _metaphone(s):
    if jellyfish is None:
        return s
    try:
        return jellyfish.metaphone(s).lower()
    except Exception:
        return s


# How close a spoken word has to be to a screen name. Lower than the
# medication threshold on purpose: picking the wrong SCREEN shows you
# the wrong page, which you fix by looking at it. Picking the wrong
# MEDICATION is not recoverable that way, so that matcher stays strict
# and refuses to guess. The cost of being wrong sets the threshold.
SCREEN_MATCH_THRESHOLD = _env("SCREEN_MATCH_THRESHOLD", 76.0)


def match_choice(text, options, threshold=SCREEN_MATCH_THRESHOLD):
    """Phonetic + spelling match of a spoken phrase against a set of
    labels: {key: (alias, alias, ...)}.

    This is what stops the station from only answering to phrases it
    was told about. "Open storage" heard as "open storge", "open
    storidge" or "open sturge" is obviously the same request, and it
    should not need the recogniser to be perfect to act on it.
    Returns the best key, or None.
    """
    toks = normalize(text).replace("'", "").split()
    if not toks:
        return None
    grams = {" ".join(toks[i:i + n]).replace(" ", "")
             for n in (1, 2, 3) for i in range(len(toks) - n + 1)}
    best_key, best_score = None, 0.0
    for key, aliases in options.items():
        for alias in aliases:
            al = alias.lower().replace(" ", "")
            am = _metaphone(al)
            for g in grams:
                if abs(len(g) - len(al)) > max(3, len(al) * 0.5):
                    continue
                sc = 0.5 * fuzz.ratio(g, al) + \
                    0.5 * fuzz.ratio(_metaphone(g), am)
                if sc > best_score:
                    best_key, best_score = key, sc
    return best_key if best_score >= threshold else None


def match_med(text, names):
    """Phonetic + spelling similarity over 1-3 word windows (joined), so
    'lo sartan' and 'liz and oprah' still hit the right drug.
    Returns (confident_name | None, suggestion | None, top3 scores)."""
    if not names:
        return None, None, []
    toks = normalize(text).replace("'", "").split()
    grams = {"".join(toks[i:i + n])
             for n in (1, 2, 3) for i in range(len(toks) - n + 1)}
    scored = []
    for name in names:
        nl = name.lower()
        nm = _metaphone(nl)
        best = 0.0
        for g in grams:
            if len(g) < max(3, len(nl) * 0.4):
                continue
            s = 0.5 * fuzz.ratio(g, nl) + 0.5 * fuzz.ratio(_metaphone(g), nm)
            if s > best:
                best = s
        scored.append((best, name))
    scored.sort(reverse=True)
    top_s, top = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    top3 = [(round(s), n) for s, n in scored[:3]]
    if top_s >= MED_MATCH_THRESHOLD and top_s - second >= MED_MATCH_MARGIN:
        return top, None, top3
    if top_s >= MED_SUGGEST_THRESHOLD and top_s - second >= MED_SUGGEST_MARGIN:
        return None, top, top3
    return None, None, top3


def parse(text, med_names):
    """transcript + this device's medication names -> Intent."""
    t = normalize(text)
    name = "unknown"
    for intent, rx in _COMPILED:
        if rx.search(t):
            name = intent
            break
    med, suggestion, scores = match_med(t, med_names)
    # A bare medication name ("Metformin?") most likely means
    # "did I take it / can I have it" — ask as did_take.
    if name == "unknown" and (med or suggestion) and len(t.split()) <= 3:
        name = "did_take"
    return Intent(name=name, text=t, med=med,
                  suggestion=suggestion, scores=scores)


def is_yes(text):
    return bool(YES.search(normalize(text)))


def is_no(text):
    return bool(NO.search(normalize(text)))


def looks_hallucinated(text):
    """Guard against decoder loops (the same word repeated) — seen when a
    recognizer's key-term boost is set too high."""
    toks = normalize(text).split()
    if any(len(a) >= 5 and a == b for a, b in zip(toks, toks[1:])):
        return True
    if len(toks) >= 4 and len(set(toks)) <= len(toks) / 2.5:
        return True
    return bool(re.search(r"(\w)\1{5,}", (text or "").lower()))
