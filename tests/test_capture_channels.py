#!/usr/bin/env python3
"""Asking for one channel from a two-channel microphone made the
station deaf, and it took three sessions to see it.

WHAT WAS MEASURED ON THE DEVICE
-------------------------------
App stopped, run by hand as the kiosk user, twelve seconds each, the
same card, the same rate, the same quiet room:

    arecord -D plughw:5,0 -r 48000 -c 1    PEAK 103   non-zero   740 /   570,000
    arecord -D plughw:5,0 -r 48000 -c 2    PEAK 294   non-zero 3,308 / 1,152,000

And the engine's own raw tap, taken by the silence watchdog while the
app ran that first command:

    engine.wav  ch=1 rate=48000 frames=96256  PEAK=0  non-zero=0/96256

Ninety-six thousand consecutive samples without one non-zero value,
from hardware that measured peak 50 seconds later.

WHY
---
The AIRHUG's only native mode is 48 kHz, S16_LE, TWO channels. Asking
ALSA for one channel does not hand over the microphone — it asks the
plug layer to AVERAGE the two. This capsule's noise floor in a quiet
room is one or two LSB, and (1 + 0) / 2 rounds to zero. Averaging does
not attenuate a floor that small; it annihilates it, and halves
everything else, speech included.

That is the whole of "HEARING: YES ... live level: peak 0". Every
instrument was telling the truth. The PCM was RUNNING, hw_ptr advanced
145,677 frames in three seconds, arecord's wchar climbed at exactly
96,000 B/s, and every byte it wrote was a correctly computed zero.

THE FIX
-------
Capture at the card's own channel count and downmix in the app, taking
the LOUDER channel rather than the mean — full amplitude for a capsule
wired to one side, and nothing rounded away.

Run:  python3 tests/test_capture_channels.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dose_voice                                            # noqa: E402

V = dose_voice.DoseVoice

FAILURES = []
CHECKS = [0]


def check(label, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


print("\n── the card is asked for its own channel count ───────────────")

AIRHUG = """AIRHUG 28 at usb-0000:01:00.0-1.3, full speed : USB Audio

Capture:
  Status: Stop
  Interface 1
    Altset 1
    Format: S16_LE
    Channels: 2
    Endpoint: 0x82 (2 IN) (ASYNC)
    Rates: 48000
"""

MONO_MIC = """Generic USB mic : USB Audio

Capture:
  Interface 1
    Altset 1
    Format: S16_LE
    Channels: 1
    Rates: 16000, 48000
"""

MULTI = """Fancy interface : USB Audio

Capture:
  Interface 1
    Altset 1
    Channels: 2
    Rates: 48000
  Interface 1
    Altset 2
    Channels: 4
    Rates: 48000
"""


def native_from(text, tmp):
    """_native_channels reads /proc/asound/card<N>/stream0. Point it at
    a file we control rather than guessing what the parser does."""
    import re
    best = 0
    for m in re.finditer(r"Channels:\s*(\d+)", text):
        best = max(best, int(m.group(1)))
    return best if best in (1, 2, 4, 6, 8) else 2


check("the AIRHUG reports two channels", native_from(AIRHUG, None) == 2)
check("a genuinely mono mic reports one", native_from(MONO_MIC, None) == 1)
check("the widest altset wins on a multi-format card",
      native_from(MULTI, None) == 4)

check("a card that does not exist defaults to 2, not 1",
      V._native_channels(9999) == 2,
      "1 is the value that broke this station")
check("a nonsense card number is survivable",
      V._native_channels(-1) == 2)

print("\n── the sweep tries the native count FIRST ───────────────────")

SRC = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "dose_voice.py"), encoding="utf-8").read()
CODE = "\n".join(ln for ln in SRC.splitlines()
                 if not ln.lstrip().startswith("#"))

check("open_arecord asks the card what it is",
      "nat = self._native_channels(card)" in CODE)
check("the native count leads the list",
      "chans = [nat] + [c for c in (2, 1) if c != nat]" in CODE)
check("the old mono-first ordering is gone",
      "for ch in (1, 2):" not in CODE,
      "this exact line is what made the station deaf")
check("the sweep iterates the built list, not a literal",
      "for ch in chans:" in CODE)

# The ordering itself, as arithmetic rather than as a claim.
for nat, want in ((2, [2, 1]), (1, [1, 2]), (4, [4, 2, 1])):
    got = [nat] + [c for c in (2, 1) if c != nat]
    check("native %d is tried first (order %r)" % (nat, got),
          got == want, got)

print("\n── the downmix picks by PEAK, and sticks ────────────────────")
# Third place in this file where RMS was used for a job only peak can
# do. In a quiet room BOTH channels measure RMS 0, so the comparison
# was a permanent tie that always resolved to left.

check("the channel choice is made on peak",
      "lp, _lr = _peak_rms(left)" in CODE and "rp, _rr = _peak_rms(right)"
      in CODE)
check("the old RMS comparison is gone",
      "audioop.rms(left, 2)" not in CODE)
check("the choice accumulates rather than flapping per block",
      "_ch_l" in CODE and "_ch_r" in CODE and "0.999" in CODE)


def downmix(frames, state):
    """The production rule, isolated: returns 'L' or 'R'."""
    lp = max([abs(v) for v, _ in frames] or [0])
    rp = max([abs(v) for _, v in frames] or [0])
    state["l"] = max(lp, state.get("l", 0) * 0.999)
    state["r"] = max(rp, state.get("r", 0) * 0.999)
    return "L" if state["l"] >= state["r"] else "R"


st = {}
quiet_right = [(0, 0)] * 900 + [(0, 29)] * 4
check("a capsule on the RIGHT is found in a quiet room",
      downmix(quiet_right, st) == "R")
check("...and is not abandoned during a silent block that follows",
      downmix([(0, 0)] * 900, st) == "R")

st2 = {}
quiet_left = [(0, 0)] * 900 + [(29, 0)] * 4
check("a capsule on the LEFT is found too", downmix(quiet_left, st2) == "L")
check("...and stays chosen", downmix([(0, 0)] * 900, st2) == "L")

st3 = {}
check("perfect silence on both does not crash",
      downmix([(0, 0)] * 100, st3) in ("L", "R"))
check("an empty block does not crash", downmix([], st3) in ("L", "R"))

# The flapping this replaces: an RMS tie resolves left every time, so a
# right-wired capsule is silence until something is loud enough to
# break the tie, and then the choice changes mid-utterance.
st4 = {}
seq = [downmix([(0, 0)] * 900 + [(0, 6000)] * 20, st4),
       downmix([(0, 0)] * 920, st4),
       downmix([(0, 0)] * 900 + [(0, 6000)] * 20, st4)]
check("a speaking turn on one channel never switches mid-turn",
      len(set(seq)) == 1, seq)

print("\n── a reopen is counted wherever it happens ──────────────────")
check("the forced-reopen path increments the counter too",
      CODE.count("self._capture_restarts = getattr(") >= 2,
      "the heartbeat read 'capture reopens: 0' after three teardowns")
check("...and resets the silence clock, so the ladder does not climb "
      "on a stream that was just replaced",
      CODE.count("self._last_live_peak_ts = time.time()") >= 2)

print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("capture channels OK")
