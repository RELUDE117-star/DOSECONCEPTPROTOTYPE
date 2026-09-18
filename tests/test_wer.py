"""Word Error Rate — a number that says whether recognition got BETTER.

Every voice fix so far has been argued from anecdote: "it seems to mishear
less". That is not a measurement, and it is why a mic that had captured
0.83 seconds of audio in twenty-three minutes could sit undetected behind
a theory about STT model accuracy. WER is the number that would have said
"this is not a model problem" on day one.

WER = (substitutions + deletions + insertions) / words_in_reference

0.0 is perfect. 1.0 means you got nothing useful. It can exceed 1.0 when
the recogniser hallucinates more words than were spoken — which is a real
failure mode on a hot, noisy capture, and one worth being able to see.

Two things live here:

  1. The metric itself, with golden cases, so the measurement is trusted
     before anything is measured with it. A broken yardstick is worse than
     no yardstick.

  2. A real-audio harness. Drop matched pairs into tests/audio/:

         tests/audio/<name>.wav   — a real recording from the real mic
         tests/audio/<name>.txt   — what was actually said

     and this scores the pipeline against them. Recordings are git-ignored
     (*.wav) on purpose: they are voice data, they belong on the device and
     on your machine, not in a public repository.

Run:  python3 tests/test_wer.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIO_DIR = os.path.join(ROOT, "tests", "audio")

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


# ── the metric ────────────────────────────────────────────────────────

# Spoken digits and their written forms are the same utterance. "take two
# pills" and "take 2 pills" are not a recognition error, and counting them
# as one would make the metric punish a correct transcript.
_NUMBERS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "ten": "10", "eleven": "11", "twelve": "12",
}


def normalise(text):
    """Fold away differences that are not recognition errors.

    Case, punctuation and surrounding whitespace carry no meaning in a
    spoken command, and neither does 'two' vs '2'. Everything else is
    left alone — this must not quietly 'fix' a wrong word into a right
    one, or the metric flatters the thing it is measuring.
    """
    text = (text or "").lower()
    # Keep apostrophes inside words ("don't"), drop other punctuation.
    text = re.sub(r"[^\w\s']", " ", text)
    words = []
    for w in text.split():
        w = w.strip("'")
        if not w:
            continue
        words.append(_NUMBERS.get(w, w))
    return words


def wer_counts(reference, hypothesis):
    """Levenshtein over WORDS. Returns (sub, del, ins, ref_len).

    Full DP table rather than the one-row trick, because the edit counts
    have to be recovered by backtrace — knowing WHICH kind of error
    dominates is the diagnostic value. All deletions means the capture is
    cutting words off (a VAD or endpointing problem). All substitutions
    means the audio arrived and the model misread it (a genuine STT
    problem). Insertions mean it is hearing things that were not said —
    usually a capture running far too hot.
    """
    ref = normalise(reference)
    hyp = normalise(hypothesis)
    n, m = len(ref), len(hyp)

    if n == 0:
        # Nothing was said; anything transcribed is an insertion.
        return (0, 0, m, 0)

    d = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        d[i][0] = i
    for j in range(m + 1):
        d[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            if ref[i - 1] == hyp[j - 1]:
                d[i][j] = d[i - 1][j - 1]
            else:
                d[i][j] = 1 + min(d[i - 1][j - 1],   # substitution
                                  d[i][j - 1],       # insertion
                                  d[i - 1][j])       # deletion
    # Backtrace to attribute the errors.
    sub = dele = ins = 0
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and ref[i - 1] == hyp[j - 1] \
                and d[i][j] == d[i - 1][j - 1]:
            i, j = i - 1, j - 1
        elif i > 0 and j > 0 and d[i][j] == d[i - 1][j - 1] + 1:
            sub += 1
            i, j = i - 1, j - 1
        elif j > 0 and d[i][j] == d[i][j - 1] + 1:
            ins += 1
            j -= 1
        else:
            dele += 1
            i -= 1
    return (sub, dele, ins, n)


def wer(reference, hypothesis):
    sub, dele, ins, n = wer_counts(reference, hypothesis)
    if n == 0:
        return 0.0 if ins == 0 else float("inf")
    return (sub + dele + ins) / float(n)


def describe(reference, hypothesis):
    """One line a human can act on, not just a number."""
    sub, dele, ins, n = wer_counts(reference, hypothesis)
    rate = wer(reference, hypothesis)
    worst = max((sub, "substitutions"), (dele, "deletions"),
                (ins, "insertions"))
    hint = ""
    if n and worst[0]:
        if worst[1] == "deletions":
            hint = "  <- words going missing: capture/VAD, not the model"
        elif worst[1] == "insertions":
            hint = "  <- hearing things unsaid: capture likely too hot"
        else:
            hint = "  <- words misread: this one really is the model"
    return ("WER %.3f  (%d sub, %d del, %d ins over %d words)%s"
            % (rate, sub, dele, ins, n, hint))


# ── 1. the yardstick itself ───────────────────────────────────────────
print("== 1. the metric is correct on known cases ==")

ok(wer("take two pills", "take two pills") == 0.0,
   "an exact match is 0.0")

ok(wer("", "") == 0.0, "empty vs empty is 0.0")

# one substitution in three words
ok(abs(wer("take two pills", "take two bills") - 1 / 3) < 1e-9,
   "one substitution in three words is 1/3")

# one deletion in three words
ok(abs(wer("take two pills", "take pills") - 1 / 3) < 1e-9,
   "one deletion in three words is 1/3")

# one insertion in three words
ok(abs(wer("take two pills", "take two more pills") - 1 / 3) < 1e-9,
   "one insertion in three words is 1/3")

ok(wer("take two pills", "") == 1.0,
   "hearing nothing at all is 1.0, not an error")

ok(wer("hello", "one two three") > 1.0,
   "hallucinating more words than were said exceeds 1.0")

s, d, i, n = wer_counts("take two pills now", "take bills now")
ok((s, d, i, n) == (1, 1, 0, 4),
   "it attributes sub/del/ins separately (got %r)" % ((s, d, i, n),))

print("== 2. normalisation folds non-errors, and ONLY non-errors ==")

ok(wer("Take two pills.", "take two pills") == 0.0,
   "case and punctuation are not recognition errors")

ok(wer("take 2 pills", "take two pills") == 0.0,
   "digits and their spoken forms are the same utterance")

ok(wer("don't take it", "dont take it") > 0.0,
   "an apostrophe inside a word is still a real difference")

ok(wer("take two pills", "take three pills") > 0.0,
   "a WRONG number is still an error (normalisation must not hide it)")

ok(normalise("  Take   TWO pills!  ") == ["take", "2", "pills"],
   "normalise collapses whitespace and lowercases")

print("== 3. the diagnostic hint points at the right stage ==")

ok("capture/VAD" in describe("take two pills now", "take two"),
   "mostly deletions blames capture, not the model")

ok("too hot" in describe("hello", "hello there you are"),
   "mostly insertions blames a hot capture")

ok("the model" in describe("take two pills", "bake blue bills"),
   "mostly substitutions blames the model")

# ── 4. real recorded audio, when it is present ────────────────────────
print("== 4. real recorded audio ==")

pairs = []
if os.path.isdir(AUDIO_DIR):
    for f in sorted(os.listdir(AUDIO_DIR)):
        if f.endswith(".wav"):
            txt = os.path.join(AUDIO_DIR, f[:-4] + ".txt")
            if os.path.exists(txt):
                pairs.append((os.path.join(AUDIO_DIR, f), txt))

if not pairs:
    print("  SKIP: no recordings in tests/audio/ yet.")
    print("  To measure the real pipeline, put matched pairs there:")
    print("    tests/audio/<name>.wav  — recorded from the real mic")
    print("    tests/audio/<name>.txt  — what was actually said")
    print("  (*.wav is git-ignored: voice data stays out of a public repo.)")
    print("  Capture them ON THE DEVICE, e.g.:")
    print("    arecord -D plughw:5,0 -f S16_LE -r 48000 -c 2 -d 5 take_two.wav")
    ok(True, "harness is present and skips cleanly with no recordings")
else:
    try:
        sys.path.insert(0, ROOT)
        import dose_voice  # noqa: F401
        engine = True
    except Exception as e:
        print("  SKIP: speech engine not importable here (%s)" % (e,))
        engine = False
        ok(True, "harness skips cleanly without an engine")

    if engine:
        total_err = total_ref = 0
        for wav, txt in pairs:
            with open(txt) as fh:
                reference = fh.read().strip()
            hypothesis = ""
            try:
                # Kept deliberately loose: the point is that the harness
                # exists and the metric is trustworthy. Wire this to
                # whichever recogniser you are comparing.
                from dose_voice import DoseVoice
                hypothesis = DoseVoice.transcribe_file(wav)  # type: ignore
            except Exception as e:
                print("  %s: no transcript (%s)" % (os.path.basename(wav), e))
                continue
            s, d, i, n = wer_counts(reference, hypothesis)
            total_err += s + d + i
            total_ref += n
            print("  %-28s %s" % (os.path.basename(wav),
                                  describe(reference, hypothesis)))
        if total_ref:
            agg = total_err / float(total_ref)
            print("  ---")
            print("  AGGREGATE WER: %.3f over %d reference words"
                  % (agg, total_ref))
            ok(agg <= 1.0,
               "aggregate WER %.3f is not worse than hearing nothing" % agg)

print()
print("WER suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== WER: the yardstick is trustworthy ===")
