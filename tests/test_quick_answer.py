#!/usr/bin/env python3
"""Answer from the live transcript when it already says enough.

WHY THIS EXISTS
---------------
Ryan, on the device: "from starting a voice dictation its about 7.5-10s
… we need TTS to show a lot faster because that lets the user know it
was heard correctly and they just need to wait a second."

Whisper costs about 4 s on this board and cannot be made much faster —
it pads every utterance to a thirty-second window, so a one-word
question costs what a sentence does, and the smallest sensible model at
four threads is 2.12 s of pure inference. Under two seconds is not
reachable that way.

But the live recogniser has ALREADY produced a transcript by the time
the person stops speaking, at no extra cost, and on this station it is
frequently exact:

    vosk "what time is it"                -> time
    vosk "how many pills to i have left"  -> pills_left
    vosk "did i take my aspirin today"    -> did_take, Aspirin

When that transcript already parses into a complete intent, waiting
four more seconds to be told the same thing is waiting for nothing.

THE GATE is the language layer's own judgement, not a confidence number
invented for the occasion. Intent.complete means "acting on this now
cannot be premature": False for an unknown intent, and False for one
that needs a medication it has not matched. On the live transcripts
this device actually logged, that is exactly the line between the ones
that were right and the ones that were mush.

Run:  python3 tests/test_quick_answer.py
"""
import os
import sys

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


def engine(meds=("Aspirin", "Metformin")):
    e = object.__new__(dose_voice.DoseVoice)
    e._med_names = lambda: list(meds)
    return e


print("\n── transcripts this device really produced ──────────────────")
# Left column is what Vosk gave; these are copied out of turns.jsonl.
REAL = [
    ("what time is it", True),
    ("how many pills to i have left", True),
    ("did i take my aspirin today", True),
    ("what do i take today", True),
    ("what to i take taken", False),
    ("a dose for time is it", False),
    ("for time is it", False),
    ("how many pills two i tablet", False),
    ("did i take my [unk]", False),
]
for text, want in REAL:
    got = engine()._quick_answer(text) is not None
    check("%-32r -> %s" % (text, "answer now" if want else "use Whisper"),
          got == want, got)

print("\n── nothing that acts on the world may take this path ────────")
for text in ("dispense my aspirin", "give me my metformin",
             "i want to add a medication", "add a new medication",
             "i feel unwell", "help me i think i took too many"):
    check("%-40r is never answered from the live text" % text,
          engine()._quick_answer(text) is None)

check("the allowed set is questions only",
      not (dose_voice.DoseVoice.QUICK_INTENTS
           & {"dispense", "addmed", "crisis", "emergency", "unwell",
              "medical_question", "cancel"}),
      sorted(dose_voice.DoseVoice.QUICK_INTENTS))

print("\n── it cannot fire on nothing ───────────────────────────────")
check("empty text", engine()._quick_answer("") is None)
check("whitespace", engine()._quick_answer("   ") is None)
check("None", engine()._quick_answer(None) is None)
check("a single word is never enough",
      engine()._quick_answer("time") is None)
check("a medication needed but not matched falls through",
      engine(meds=())._quick_answer("did i take my aspirin today")
      is None)
check("a broken med list is survivable",
      (lambda e: (setattr(e, "_med_names",
                          lambda: (_ for _ in ()).throw(OSError())),
                  e._quick_answer("what time is it"))[1])(engine())
      is None)

print("\n── a hit is counted, a miss costs nothing ──────────────────")
e = engine()
e._quick_answer("what time is it")
e._quick_answer("how many pills to i have left")
check("hits are counted", getattr(e, "_quick_hits", 0) == 2)
e._quick_answer("how many pills two i tablet")
check("misses are not", getattr(e, "_quick_hits", 0) == 2)

print("\n── it is wired in, ahead of the recogniser ─────────────────")
SRC = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "dose_voice.py"), encoding="utf-8").read()
CODE = "\n".join(ln for ln in SRC.splitlines()
                 if not ln.lstrip().startswith("#"))

QUICK_CALL = "self._quick_answer(hint)"
check("finish() consults it", QUICK_CALL in CODE)
check("...before waiting for the speculation",
      CODE.index(QUICK_CALL) < CODE.index('spec["done"].wait('))
check("...and before the full recogniser",
      CODE.index(QUICK_CALL)
      < CODE.index("return self._better_transcribe(final_buf, hint)"))

print("\n── but it does NOT get in front of the Mac ─────────────────")
# This shortcut is why the Mac's hit counter sat at zero while the Mac
# was paired, running, and answering in 1.76 s: Vosk's live text got
# there first on exactly the phrases people say most, so the better
# recogniser was never asked anything at all.
#
# Ryan: "have it put more emphasis so that it focuses on the mac first
# more heavily" and "it should of course fall back but it shouldn't
# fall back so easily."
check("the shortcut is skipped when the Mac is available",
      "None if self._remote_ready() else self._quick_answer(hint)" in CODE,
      "the local path is the parachute, not the plan")
check("the readiness check makes no network call on a turn",
      "_remote_stt.available()" in CODE,
      "available() reads a cached flag; probe() does the talking")
check("something refreshes that flag between turns",
      "_remote_probe_tick" in CODE and "_remote_stt.probe()" in CODE)
check("...from the heartbeat thread, never from a turn",
      "self._remote_probe_tick()" in CODE
      and CODE.index("def _heartbeat_loop")
      < CODE.index("self._remote_probe_tick()")
      < CODE.index("def stop(self)"))
check("...and never while the station is mid-turn",
      'if self.state != "idle":' in CODE)
check("the heartbeat says WHERE the recognising happens",
      "Mac speech server:" in CODE and "turns answered by Mac" in CODE,
      "a claim that cannot be checked by reading a file is a claim "
      "somebody has to take on trust")
check("a quick answer records zero recogniser time, honestly",
      "self._t_fast = 0.0" in CODE and "self._t_slow = 0.0" in CODE)
check("...and says in the log how it was answered",
      "answered from the live transcript" in SRC)
check("the engine is named so the turn log is not misleading",
      'self._last_engine = "vosk (live)"' in CODE)
check("quick answers are counted in the turn row", '"quick":' in CODE)

print("\n── the screen shows the words BEFORE the thinking ──────────")
# Several seconds of a screen that says nothing reads as "it missed me"
# and makes people repeat themselves, which starts another turn.
check("the live transcript is put on screen at the endpoint",
      "PUT IT ON THE SCREEN NOW" in SRC)
check("...before the transcription starts",
      CODE.index('self._set_ui_state("listening", user_text=text)')
      < CODE.index("got = finish(self._cap_audio(buf), text)"))

print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("quick answer OK")
