#!/usr/bin/env python3
"""Time-to-first-sound is the whole of perceived response time.

WHY THIS TEST EXISTS
--------------------
The owner's words: "I want the visual overlay to be fast and quick
especially tts."

_speak() renders the FIRST chunk, plays it, and renders the rest on a
worker while that audio is in the air. Piper runs about three times
real time on this board, so every later chunk is ready long before the
previous one finishes and the reply comes out continuous. That design
was already right. The problem was what counted as a chunk.

_sentences() splits only on sentence ENDS. So:

    "You have two doses left today, Ryan, and the next one is at six."

is ONE chunk — sixty-four characters, about four seconds of speech,
and nothing at all is audible until the whole of it has been
synthesized. The person is looking at a screen that says nothing is
happening, which is exactly the complaint.

Splitting that at the comma costs nothing: Piper puts a small pause at
a comma anyway, so the seam is inaudible, and the remainder renders
while the opening plays.

These tests are mostly about the ways that can go WRONG — a split that
loses words, a split that opens with a stutter, a split that walks
straight over a full stop to break at "and".

Run:  python3 tests/test_tts_first_sound.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dose_voice                                            # noqa: E402

V = dose_voice.DoseVoice
MAXC = dose_voice.TTS_FIRST_CHUNK_MAX
MINC = dose_voice.TTS_FIRST_CHUNK_MIN

FAILURES = []
CHECKS = [0]


def check(label, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


def pipeline(text):
    return V._split_first(V._sentences(text))


def words(s):
    return re.findall(r"[A-Za-z0-9']+", s)


print("\n── nothing may be lost or invented ──────────────────────────")

CORPUS = [
    "You have two doses left today, Ryan, and the next one is at six.",
    "I didn't catch that, Ryan. Tap the logo and try again.",
    "Your next dose is metformin at six in the evening, and you have "
    "taken two of three today.",
    "Aspirin is a blood thinner which reduces the risk of clots.",
    "Good morning, Ryan. You have three medications scheduled today: "
    "metformin, lisinopril and aspirin.",
    "Yes, Ryan - that one is due at eight.",
    "That medication is not in my database, Ryan.",
    "Sure.",
    "Okay",
    "",
    "   ",
    "No.",
    "A" * 300,
    "one, two, three, four, five, six, seven, eight, nine, ten, eleven",
    "Wait. What? Really! Yes... no.",
    "supercalifragilisticexpialidociousandmorewordswithoutanybreakhere",
    "Take one tablet with food; the second is due at eight, but only "
    "if you have eaten.",
]

lost = []
for t in CORPUS:
    out = pipeline(t)
    if words(" ".join(out)) != words(t):
        lost.append(t[:40])
check("every word survives the split, in order", not lost, lost)

empties = [t for t in CORPUS if any(not c.strip() for c in pipeline(t))]
check("no empty fragment is ever produced", not empties, empties)

stutters = []
for t in CORPUS:
    out = pipeline(t)
    if len(out) > 1 and any(len(c) < MINC for c in out[:2]):
        stutters.append((t[:35], out[:2]))
check("a split never opens with a stutter", not stutters, stutters)

print("\n── it actually gets the station talking sooner ──────────────")

t = "You have two doses left today, Ryan, and the next one is at six."
out = pipeline(t)
check("the long single-sentence reply is now split", len(out) == 2, out)
check("...at the comma, where a speaker pauses anyway",
      out[0].endswith(","), out)
check("...and the opening fits the first-sound budget",
      len(out[0]) <= MAXC, len(out[0]))
check("...which is most of the sentence, not a fragment of it",
      len(out[0]) >= MAXC * 0.6, len(out[0]))

before = V._sentences(t)
check("before the split, that reply was ONE chunk — nothing audible "
      "until all of it had rendered", len(before) == 1)

print("\n── the strongest boundary wins, not the latest ──────────────")

t = "I didn't catch that, Ryan. Tap the logo and try again."
out = pipeline(t)
check("a full stop beats a comma and a conjunction",
      out[0] == "I didn't catch that, Ryan.", out)
check("...so it does not break across a sentence end at 'and'",
      not out[0].endswith("logo"), out)

t = "Good morning, Ryan. You have three medications scheduled today: " \
    "metformin, lisinopril and aspirin."
out = pipeline(t)
check("a greeting is split off whole", out[0] == "Good morning, Ryan.", out)

t = "Aspirin is a blood thinner which reduces the risk of clots."
out = pipeline(t)
check("with no punctuation at all, a conjunction will do",
      len(out) == 2 and out[1].startswith("which"), out)

print("\n── one long clause overshoots rather than giving up ─────────")

t = "Your next dose is metformin at six in the evening, and you have " \
    "taken two of three today."
out = pipeline(t)
check("a sentence whose only break is past the window still splits",
      len(out) == 2, out)
check("...just past it, not at the far end",
      MAXC < len(out[0]) <= MAXC * 2, len(out[0]))
check("...and a 50-character opening beats the 89-character original",
      len(out[0]) < len(t) * 0.7, len(out[0]))

check("a word with no break anywhere is left alone rather than "
      "chopped mid-phrase",
      pipeline("A" * 300) == ["A" * 300])

print("\n── short and cached replies are untouched ───────────────────")

for t in ("Sure.", "Okay", "No.", "Yes, Ryan."):
    check("%r is spoken whole" % t, pipeline(t) == [t.strip()])
check("an empty reply produces nothing to say", pipeline("") == [])
check("whitespace produces nothing to say", pipeline("   ") == [])
check("_split_first on an empty list is safe", V._split_first([]) == [])

print("\n── later chunks are left exactly as they were ───────────────")

t = ("One sentence here that is quite long, yes. And a second one. "
     "And a third one that also runs on for a while.")
sents = V._sentences(t)
out = pipeline(t)
check("only the first chunk is ever touched",
      out[len(out) - len(sents) + 1:] == sents[1:], (out, sents))

print("\n── it is wired in, and bounded ──────────────────────────────")

SRC = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "dose_voice.py"), encoding="utf-8").read()
CODE = "\n".join(ln for ln in SRC.splitlines()
                 if not ln.lstrip().startswith("#"))

check("_speak splits the first chunk before rendering anything",
      "chunks = self._split_first(chunks)" in CODE)
check("...after _sentences, so sentence structure is respected first",
      CODE.index("chunks = self._sentences(text)")
      < CODE.index("chunks = self._split_first(chunks)"))
check("...and before the first chunk is rendered",
      CODE.index("chunks = self._split_first(chunks)")
      < CODE.index("first = self.render_to_cache(chunks[0])"))
check("a fully cached reply still skips all of this",
      CODE.index("if os.path.exists(whole):")
      < CODE.index("chunks = self._sentences(text)"))
check("the limits are environment overrides",
      "DOSE_TTS_FIRST_MAX" in SRC and "DOSE_TTS_FIRST_MIN" in SRC)
check("the opening is short enough to be quick (<= 60 chars)",
      MAXC <= 60, MAXC)
check("...and long enough not to sound clipped (>= 25 chars)",
      MAXC >= 25, MAXC)
check("the minimum leaves room for a real phrase", 6 <= MINC < MAXC)
check("the remainder still renders on a worker while the opening "
      "plays — that is what makes this free",
      "name=\"tts-stream\"" in CODE)

print("\n── the background render must not race the first chunk ─────")
# The intent was always to overlap the remaining chunks with PLAYBACK
# of the first one. The thread was started before the first chunk was
# rendered, so it overlapped the RENDER instead — competing, on four
# cores running ONNX, with the single piece of work the person is
# waiting for.
#
# The device measured it. `speak` in the turn row is the first chunk's
# render time and nothing else, so it should be roughly constant
# whatever the reply length. It was not:
#
#   "The time is 1:18 PM."                  speak 2.00
#   "I could not find aspirin, Ryan."       speak 2.31
#   "No medications are in view today..."   speak 3.14
#   "Current inventory: New Medication..."  speak 4.55
#
# A first-chunk render cannot scale with the length of the whole reply
# on its own. The extra is the other chunks, rendering underneath it.
_i_render = SRC.index("first = self.render_to_cache(chunks[0])")
_i_thread = SRC.index('name="tts-stream"')
_i_play = SRC.index("self._play_wav(first)")
_i_stamp = SRC.index("self._t_first_sound = time.time() - _t_speak0")

check("the first chunk is rendered before the worker starts",
      _i_render < _i_thread,
      "otherwise the worker competes for the critical path")
check("time-to-first-sound is stamped before the worker starts too",
      _i_stamp < _i_thread,
      "or the measurement includes the contention it caused")
check("the worker still starts before playback, so it overlaps the "
      "AUDIO — which is what makes streaming free",
      _i_thread < _i_play)
check("there is still exactly one worker",
      SRC.count('name="tts-stream"') == 1)

print("\n── nothing decodes speech while the station is answering ───")
# render_to_cache timing it own parts settled where the remaining
# second went, and it was none of my four guesses:
#
#   tts: {'cache': 0.0002, 'hit': 0, 'synth': 1.22, 'write': 0.0002}
#   tts: {'cache': 0.001,  'hit': 0, 'synth': 4.37, 'write': 0.0002}
#
# The cache lookup and the write are microseconds. It is all
# synthesis — and the same sentence measured 0.72 s standalone with
# the app running but IDLE, which is the flaw in that comparison.
#
# During a turn the listening loop decodes every 21 ms block through
# Vosk, right through the transcription and the reply, on the same
# four cores Piper renders on. In this room 54% of blocks now carry
# signal, so it is decoding hard. Vosk's output after endpointing is
# thrown away.
check("Vosk is not fed while the station is thinking or speaking",
      'if self.state in ("thinking", "speaking"):' in SRC,
      "its output is discarded then, and Piper needs the cores")
check("...and that guard sits before the decode, not after",
      SRC.index('if self.state in ("thinking", "speaking"):')
      < SRC.index("got_final = rec.AcceptWaveform(data)"))
check("the idle guard is still there too",
      'if self.state == "idle" and not WAKE_WORD:' in SRC)
check("the voice detector does not run while the station is thinking",
      'if self.state == "thinking":' in SRC
      and "is_voice = False" in SRC,
      "Silero is an ONNX inference per block, on Piper's cores, in "
      "exactly the window the person is waiting in")
check("...and the noise floor still learns from those blocks",
      SRC.index('if self.state == "thinking":')
      < SRC.index("self._nfloor = max(1.0, min(nf, NOISE_FLOOR_MAX))"),
      "that part is arithmetic, not a model — only the inference "
      "is worth skipping")
check("speaking is still handled earlier, by barge-in",
      SRC.index('if self.state == "speaking" and not measuring:')
      < SRC.index('if self.state == "thinking":'))
check("barge-in does not depend on that queue, so nothing is lost",
      "_detect_barge_in(data)" in SRC
      and SRC.index("_detect_barge_in(data)")
      < SRC.index("got_final = rec.AcceptWaveform(data)"),
      "barge-in runs in ingest() and returns before the queue")

print("\n── the prewarm caches what _speak ASKS for ─────────────────")
# 32 clips sat in voice/cache all evening while every render logged
# `'hit': 0`, including replies identical across three separate runs.
#
# prewarm_replies() rendered each fixed line WHOLE. _speak() never
# renders a whole line — it splits it and renders chunks[0], so the
# key it looks up is the OPENING FRAGMENT. For every line long enough
# to be split, the prewarmed entry could not be found. A cache whose
# keys are not the keys anybody looks up is a directory of files.
check("the prewarm chunks each line the way _speak does",
      "self._split_first(self._sentences(line))" in SRC,
      "whole lines are not what render_to_cache is asked for")
check("...and renders every chunk, not just the first",
      "for c in chunks:" in SRC and "self.render_to_cache(c)" in SRC,
      "the later ones are played seconds after and cost nothing to "
      "have ready")
check("_speak still looks up the first chunk",
      "first = self.render_to_cache(chunks[0])" in SRC)

# The invariant OPENINGS of replies that are otherwise assembled fresh
# every time. "Current inventory: New Medication, 26; ..." can never be
# cached whole, but its first 18 characters never change — and the
# opening is the only part on the critical path.
check("the invariant openings of variable replies are prewarmed",
      '"Current inventory:",' in SRC
      and '"No medications are in view today, Ryan.",' in SRC,
      "the slowest reply the station has, made to start instantly")


def _first(text):
    return pipeline(text)[0]


check("the inventory reply opens with exactly that cached fragment",
      _first("Current inventory: New Medication, 26; Metformin, 12.")
      == "Current inventory:",
      _first("Current inventory: New Medication, 26; Metformin, 12."))
# NOT "however the list changes" — that claim was too strong and the
# code is right to refuse it. A SHORT inventory is spoken whole,
# because it is short; only a long one splits, and then it splits at
# the invariant prefix. Both are the good outcome, for different
# reasons, and the test should say so rather than demand one shape.
check("a LONG inventory opens with the cached invariant fragment",
      _first("Current inventory: New Medication, 26; Metformin, 12.")
      == "Current inventory:",
      _first("Current inventory: New Medication, 26; Metformin, 12."))
check("...and a short one is spoken whole, being short enough not to "
      "need the trick",
      _first("Current inventory: Aspirin, 4.")
      == "Current inventory: Aspirin, 4.")
check("the slowest case is the one that gets the cached opening",
      len("Current inventory: New Medication, 26; Metformin, 12.")
      > len("Current inventory: Aspirin, 4."))

print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("TTS first sound OK")
