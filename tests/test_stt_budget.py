#!/usr/bin/env python3
"""A better answer that arrives at 25 seconds is not a better answer.

WHY THIS TEST EXISTS
--------------------
From the device's own turns.jsonl:

    worst   "fast": 17.26   "speak": 1.41   "total": 25.19
    best    "fast":  0.12   "speak": 1.90   "total":  2.52

Endpointing accounts for 0.49-0.57 s of that. All of the variance is
transcription — and the worst turn is the fast model taking seventeen
seconds and then the base.en escalation being started ON TOP of it,
because no stage in the chain asked what time it was before beginning.

Two things made a slow turn catastrophic rather than merely slow:

1. Escalation was unconditional. Whenever the fast answer did not
   parse, the stronger model ran, however long the fast one had
   already taken and however little chance there was of anyone still
   standing there.

2. finish() waited six seconds for the in-flight speculation and then,
   if it had not landed, transcribed the WHOLE BUFFER AGAIN — six
   seconds of waiting followed by the full cost, for audio a worker
   was already most of the way through. The worst of both paths.

Both now answer to one number: STT_TURN_BUDGET.

The escalation's cost is estimated from THIS DEVICE'S OWN measurement
of the fast pass on exactly this audio, so a throttled Pi and a cool
one get different answers without anyone tuning a constant.

Run:  python3 tests/test_stt_budget.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dose_voice                                            # noqa: E402

FAILURES = []
CHECKS = [0]


def check(label, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


class Fake(object):
    """A DoseVoice with the two recognisers replaced by stopwatches.

    Everything about this decision is time arithmetic, so the models
    are simulated by sleeping and the whole ladder is judged in
    milliseconds instead of on a Raspberry Pi."""

    def __init__(self, fast_secs, fast_text, escal_secs=None,
                 escal_text="tell me the time", usable=False):
        self.e = object.__new__(dose_voice.DoseVoice)
        e = self.e
        e.FAST_CONF_FLOOR = dose_voice.DoseVoice.FAST_CONF_FLOOR
        e.CLOUD_BUDGET_S = dose_voice.DoseVoice.CLOUD_BUDGET_S
        self.fast_secs = fast_secs
        self.escalated = []
        e._trim_silence = lambda b: b
        e._cloud_enabled = lambda: False
        e._is_online = lambda: False
        e._usable = lambda t: bool(t) and usable
        e._fw_conf = 0.0

        def fast(_b):
            time.sleep(min(fast_secs, 0.05))
            # Report the time the REAL model would have taken, which is
            # what the estimate is built from.
            e._fw_conf = 0.0
            return fast_text, "whisper tiny.en"
        e._fast_transcribe = fast

        def slow(_b):
            self.escalated.append(True)
            time.sleep(min(escal_secs or 0.0, 0.05))
            return escal_text
        e._whisper_transcribe = slow

    def run(self, vosk=""):
        """One whole turn through the real _better_transcribe."""
        return {"text": self.e._better_transcribe(b"\0" * 32000, vosk)}


print("\n── the estimate ─────────────────────────────────────────────")

check("a turn budget exists and is a ceiling, not a target",
      dose_voice.STT_TURN_BUDGET > 0)
check("it is short enough that nobody repeats themselves (<= 10s)",
      dose_voice.STT_TURN_BUDGET <= 10, dose_voice.STT_TURN_BUDGET)
check("it is long enough for a normal escalation (>= 3s)",
      dose_voice.STT_TURN_BUDGET >= 3, dose_voice.STT_TURN_BUDGET)
check("the escalation is more expensive than the fast model",
      dose_voice.ESCALATION_COST_RATIO > 1)
check("...and the ratio is the measured one (base.en 3.98s against "
      "tiny.en 2.12s on this board), not a round guess",
      1.5 <= dose_voice.ESCALATION_COST_RATIO <= 2.5,
      dose_voice.ESCALATION_COST_RATIO)
check("the budget is an environment override",
      "DOSE_STT_BUDGET" in open(dose_voice.__file__.replace(".pyc", ".py"),
                                encoding="utf-8").read())

SRC = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "dose_voice.py"), encoding="utf-8").read()
CODE = "\n".join(ln for ln in SRC.splitlines()
                 if not ln.lstrip().startswith("#"))

print("\n── the escalation is gated on the clock ─────────────────────")

check("the escalation asks what time it is first",
      "STT_TURN_BUDGET - elapsed" in CODE)
check("the estimate is built from THIS device's measured fast pass, "
      "not a constant",
      "self._t_fast * ESCALATION_COST_RATIO" in CODE)
check("a skip is counted, so accuracy cannot fall silently",
      "_escalations_skipped" in CODE)
check("a skip says why, in the turn log",
      "escalation skipped" in SRC)
check("the estimate has a floor, so a zero measurement cannot make "
      "the escalation look free",
      "max(0.2," in CODE)

print("\n── the decision itself ──────────────────────────────────────")


def decide(t_fast, elapsed=None):
    """The gate, in isolation: given a fast pass that took t_fast and
    a turn that has used `elapsed` so far, does the escalation fit?"""
    elapsed = t_fast if elapsed is None else elapsed
    left = dose_voice.STT_TURN_BUDGET - elapsed
    est = max(0.2, t_fast * dose_voice.ESCALATION_COST_RATIO)
    return left >= est


check("the fast turn from the log (0.12s) still escalates",
      decide(0.12))
check("the worst turn from the log (17.26s) does NOT",
      not decide(17.26))
check("a 1s fast pass escalates", decide(1.0))
# The ratio is MEASURED on this board, not assumed: same recording,
# three passes, median, tiny.en 2.12s against base.en 3.98s. It started
# at 3x, which made the station skip escalations it had time for and
# traded accuracy away for latency it was not short of.
check("the typical 2.1s pass on this board still escalates — the "
      "measured ratio bought that back", decide(2.1))
check("a 3s fast pass does not", not decide(3.0))
_edge = dose_voice.STT_TURN_BUDGET / (1 + dose_voice.ESCALATION_COST_RATIO)
check("the boundary is where the estimate stops fitting",
      decide(_edge - 0.1) and not decide(_edge + 0.1), _edge)

print("\n── what a skipped escalation hands back ─────────────────────")

f = Fake(fast_secs=0.0, fast_text="", usable=False)
out = f.run(vosk="what time is it")
check("with nothing from either model, the live Vosk text is not lost",
      out["text"] in ("what time is it", "tell me the time"), out)

f = Fake(fast_secs=0.0, fast_text="what time is it", usable=True)
out = f.run()
check("a usable, confident fast answer never escalates at all",
      out["text"] == "what time is it" and not f.escalated)
check("...and records no skip note, because nothing was skipped",
      not getattr(f.e, "_stt_note", ""))

f = Fake(fast_secs=0.0, fast_text="mumble", escal_secs=0.0,
         escal_text="what time is it", usable=False)
f.run()
check("a cheap fast pass DOES escalate — the budget is not a ban",
      f.escalated == [True])

print("\n── the speculation is no longer paid for twice ──────────────")

check("finish() waits the turn budget, not a hardcoded six",
      "spec[\"done\"].wait(timeout=STT_TURN_BUDGET)" in CODE)
check("the old six-second wait is gone",
      "wait(timeout=6)" not in CODE)
check("a speculation that lands is counted", "_spec_hits" in CODE)
check("a speculation that does NOT land is counted too — a station "
      "doing every turn twice is invisible otherwise",
      "_spec_misses" in CODE)
check("how long the turn waited on it is recorded",
      "_t_spec_wait" in CODE)
check("...and reset per turn, so it cannot describe the previous one",
      "self._t_spec_wait = 0.0" in CODE)

print("\n── the turn log explains its own cost ───────────────────────")

check("the turn record carries the decision, not just the total",
      "\"stt_note\":" in CODE)
check("...and the budget it was judged against",
      "\"budget\": STT_TURN_BUDGET" in CODE)
check("the note is set per turn, not per branch",
      'self._stt_note = getattr(self, "_cap_note", "") or ""' in CODE)
check("a cap applied on the way IN survives that reset — it happened "
      "to this turn and is the first thing worth knowing about it",
      'self._cap_note = ""' in CODE and "_cap_note" in CODE)

print("\n── the audio itself is bounded ──────────────────────────────")
# A budget downstream cannot rescue a 25-second transcription: by the
# time it is consulted the 25 seconds are already spent.
check("there is a ceiling on how much audio a turn may hand over",
      "STT_MAX_AUDIO_S" in CODE)
check("it is an environment override", "DOSE_STT_MAX_AUDIO" in SRC)
check("twelve seconds is longer than anything said to a cabinet, "
      "and short enough to bound the worst turn",
      6 <= dose_voice.STT_MAX_AUDIO_S <= 20, dose_voice.STT_MAX_AUDIO_S)
check("the cap is applied before the recogniser is called",
      "finish(self._cap_audio(buf), text)" in CODE)

_e = object.__new__(dose_voice.DoseVoice)
_sr = dose_voice.SAMPLE_RATE
_short = bytes(b"\x01\x00" * int(_sr * 3))
_long = bytes(b"\x02\x00" * int(_sr * 40))
check("a normal turn is handed over untouched",
      _e._cap_audio(_short) == _short)
_out = _e._cap_audio(_long)
check("a runaway turn is cut to the ceiling",
      abs(len(_out) / 2.0 / _sr - dose_voice.STT_MAX_AUDIO_S) < 0.05,
      len(_out) / 2.0 / _sr)
check("the LAST seconds are kept — a turn ends when somebody stops "
      "speaking, so the words that matter are at the end",
      _out == _long[-len(_out):])
check("the cut is recorded, with both durations",
      "audio capped" in getattr(_e, "_cap_note", ""), _e._cap_note)
check("a cut is counted", getattr(_e, "_audio_capped", 0) == 1)
check("nonsense input cannot raise", _e._cap_audio(b"") == b"")

print("\n── a turn's duration is measured, not inferred ──────────────")
# turns.jsonl reported a 70.9-second utterance on a turn whose listen
# timeout is twelve. blocks * BLOCK_SIZE / SAMPLE_RATE counts blocks
# the DEVICE delivered (1024 samples at 48 kHz) against the 16 kHz
# rate, so every figure was three times too long — and I read one of
# them as evidence that endpointing had run away.
check("seconds come from the buffer's bytes",
      "self._turn_secs = round(" in CODE and "len(buf) / 2.0" in CODE)
check("...and the block-count estimate is only a fallback",
      CODE.count("else round(blocks * BLOCK_SIZE") >= 2)
check("it is reset per turn", "self._turn_secs = None" in CODE)

print("\n── the thread the turn waits on is not deprioritised ───────")
# It used to call os.nice(5), on the reasoning that a missed QR decode
# was worse than "a few milliseconds of extra speech latency" and that
# the work was speculative anyway. Both halves stopped being true:
# finish() WAITS for the speculation and uses its answer (the device
# logs three hits to two misses), and the cost is seconds, not
# milliseconds —
#
#     fast 4.31s  of which decode 4.08s  on audio 1.84s
#
# against roughly 0.7x real time for the same model measured
# standalone on the same board. The camera argument is gone too:
# pyzbar bursts eight frames every five minutes.

_spec = CODE[CODE.index("def speculate("):CODE.index("def finish(")]
check("the speculative worker no longer nices itself",
      "os.nice" not in _spec, _spec[:200])
check("...and finish() still waits for it, which is why that matters",
      'spec["done"].wait(' in CODE)
check("the reasoning is written down where the nice used to be",
      "DO NOT DEPRIORITISE THE THREAD WE THEN WAIT ON" in SRC)
# Background work that nothing waits on SHOULD still yield.
check("the reply prewarm still yields, because nothing blocks on it",
      "os.nice(10)" in CODE)

print("\n── nothing here can make a turn SLOWER ──────────────────────")

check("the gate only ever skips work; it starts none",
      "_whisper_transcribe" in CODE)
t0 = time.time()
for _ in range(20000):
    decide(0.12)
check("the decision costs nothing measurable (20k in < 0.5s)",
      time.time() - t0 < 0.5, "%.3fs" % (time.time() - t0))

print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("STT budget OK")
