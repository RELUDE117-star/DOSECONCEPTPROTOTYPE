"""LISTENING IN A ROOM THAT ISN'T SILENT.

Reported: dictation only really works when it's quiet — with a tap
running it understood almost nothing.

Some of that is physics and a 6-inch-rated microphone a foot away;
no software fixes that. But one part was a real bug, reproduced here.

The ambient noise floor used to snap straight DOWN to the quietest
block ever seen and then climb back at 0.0005 per block — roughly four
minutes. A single momentary dip therefore pinned the gate at its
minimum, and anything continuous after that (a running tap, a fan, a
television) sat above the gate and was treated as speech: amplified by
the auto-gain, fed to the recogniser, and — because every block kept
stamping "speech heard" — the turn never ended at all.

These tests drive the REAL floor-tracking arithmetic with simulated
room audio and check that it settles where it should, how fast, and
that speech still stands clear of it.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dose_voice as dv                                    # noqa: E402

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


BLOCK_S = 0.125          # one capture block


class Floor:
    """Exactly the tracking arithmetic from ingest()."""

    def __init__(self, start=50.0):
        self.nf = start
        self.stamped = 0

    def feed(self, rms):
        nf = self.nf
        if rms < nf:
            nf = nf * 0.95 + rms * 0.05
        else:
            nf = nf * 0.98 + rms * 0.02
        self.nf = max(1.0, nf)
        gate = max(40.0, self.nf * dv.NOISE_GATE_RATIO)
        speech = rms > gate
        if speech:
            self.stamped += 1
        return speech

    def run(self, rms, blocks):
        return sum(1 for _ in range(blocks) if self.feed(rms))


print("== 1. the floor follows a room that gets noisy ==")
f = Floor()
TAP = 600.0                       # a running tap
f.run(TAP, int(10 / BLOCK_S))     # 10 s of it
ok(abs(f.nf - TAP) / TAP < 0.25,
   "after 10 s of steady noise the floor sits at the noise level "
   "(%.0f vs %.0f)" % (f.nf, TAP))

# how long does it take to get there?
f2 = Floor()
blocks = 0
while f2.nf < TAP * 0.7 and blocks < int(60 / BLOCK_S):
    f2.feed(TAP)
    blocks += 1
ok(blocks * BLOCK_S < 8.0,
   "and it gets there in %.1f s, not minutes" % (blocks * BLOCK_S))

print("== 2. THE BUG: one quiet moment must not pin the gate ==")
# Room is noisy, then a brief dip, then noisy again. The old code set
# the floor to that dip instantly and took ~4 minutes to recover, so
# every block of tap noise afterwards counted as speech.
f = Floor()
f.run(TAP, int(10 / BLOCK_S))     # settled on the tap
f.feed(5.0)                       # one momentary near-silent block
ok(f.nf > TAP * 0.5,
   "a single quiet block barely moves the floor (%.0f)" % f.nf)
after = f.run(TAP, int(10 / BLOCK_S))
ok(after == 0,
   "and the tap is STILL not mistaken for speech afterwards "
   "(%d blocks of %d)" % (after, int(10 / BLOCK_S)))

# several quiet blocks (the tap turned off) — the floor SHOULD drop
f = Floor()
f.run(TAP, int(10 / BLOCK_S))
f.run(20.0, int(4 / BLOCK_S))
ok(f.nf < TAP * 0.4,
   "when the room really does go quiet the floor follows it down "
   "(%.0f)" % f.nf)

print("== 3. the turn still ENDS in a noisy room ==")
# Continuous noise stamping "speech heard" on every block is what made
# the endpointer never fire.
f = Floor()
f.run(TAP, int(10 / BLOCK_S))
f.stamped = 0
f.run(TAP, int(30 / BLOCK_S))     # 30 s more of the same tap
ok(f.stamped == 0,
   "30 s of steady noise produces no speech stamps, so the endpointer "
   "can close the turn (%d stamps)" % f.stamped)
ok(dv.ENDPOINT_MAX_UTTERANCE <= 10,
   "and anything truly endless is cut at %.0f s regardless"
   % dv.ENDPOINT_MAX_UTTERANCE)

print("== 4. speech still gets through ==")
for label, noise, speech in (
        ("a quiet room", 30.0, 900.0),
        ("a fan running", 200.0, 1500.0),
        ("a tap running, speaking up", 600.0, 3000.0)):
    f = Floor()
    f.run(noise, int(10 / BLOCK_S))
    got = f.feed(speech)
    ok(got, "%s: speech at %.0f over a floor of %.0f is heard"
       % (label, speech, f.nf))

print("== 5. it is honest about a room it cannot work in ==")
# Quiet speech under loud noise genuinely cannot be separated by a
# gate. It must NOT be passed off as speech — being silent is the
# correct, safe answer.
f = Floor()
f.run(TAP, int(10 / BLOCK_S))
ok(not f.feed(700.0),
   "a voice barely above a running tap is NOT reported as speech "
   "(floor %.0f) — silence beats a wrong transcript" % f.nf)
ok(dv.NOISE_GATE_RATIO >= 2.5,
   "speech must stand %.1fx clear of the room, not merely be audible "
   "in it" % dv.NOISE_GATE_RATIO)

print("== 6. a microphone that hears properly is LEFT ALONE ==")
# The ceiling was pushed to 40x chasing a weak far-field capsule. That
# was the wrong lever: every quiet block between words is multiplied
# too, so the room comes up with the voice and the recogniser gets an
# overdriven mix of everything instead of a person talking. A decent
# USB mic does its own conditioning; stacking ours on top fights it.
from dose_voice import DoseVoice as _DV                      # noqa: E402

agc = object.__new__(_DV)
agc._max_gain = float(os.environ.get("DOSE_MAX_GAIN", "4"))
ok(agc._max_gain <= 6,
   "the auto-gain ceiling is a modest %.0fx, not a boost"
   % agc._max_gain)

for label, lvl in (("a good mic, close", 2600.0),
                   ("a good mic, a foot away", 1800.0),
                   ("right at the healthy threshold",
                    _DV.HEALTHY_SPEECH_RMS)):
    agc._speech_level = lvl
    ok(abs(agc._agc_ceiling() - 1.0) < 0.001,
       "%s (RMS %.0f): gain 1.0x — the audio is untouched"
       % (label, lvl))

# ...but a genuinely weak capsule still gets help
for label, lvl in (("a weak capsule", 900.0),
                   ("a very weak capsule", 300.0)):
    agc._speech_level = lvl
    c = agc._agc_ceiling()
    ok(1.0 < c <= agc._max_gain,
       "%s (RMS %.0f) still gets a modest %.2fx" % (label, lvl, c))

# and the boost eases in rather than switching on abruptly
agc._speech_level = _DV.HEALTHY_SPEECH_RMS * 0.99
just_under = agc._agc_ceiling()
ok(just_under < 1.1,
   "just below the threshold the boost is still nearly nothing "
   "(%.3fx) — no cliff" % just_under)

agc._speech_level = 0.0
ok(agc._agc_ceiling() == agc._max_gain,
   "before anything is known it allows the full %.0fx, then settles"
   % agc._max_gain)

# and the room is never amplified either way: gain is applied only
# above the gate, so ambient noise passes through untouched
f = Floor()
f.run(TAP, int(10 / BLOCK_S))
ok(not f.feed(TAP),
   "steady room noise never clears the gate, so it is never amplified")

print("== 7. the microphone is not turned up past clipping ==")
# The capture gain was pinned at 100%. On a cheap USB capsule that is
# past the clipping point: a clap saturates, room tone reads hot, and
# a clipped waveform carries LESS for the recogniser than a quiet one.
from dose_voice import DoseVoice                            # noqa: E402

VSRC = open(os.path.join(ROOT, "dose_voice.py"), errors="ignore").read()
cap = VSRC.split("attempts = []")[1][:1400]
ok('"100%", "cap"' not in cap,
   "capture gain is no longer slammed to 100%")
ok('["cap", "unmute"]' in cap,
   "the device is only unmuted and selected — its level is left alone")
ok("if self._capture_level:" in cap,
   "a percentage is forced ONLY when something asks for one")

# with nothing asking, nothing is forced
import importlib                                            # noqa: E402
os.environ.pop("DOSE_CAPTURE_LEVEL", None)
importlib.reload(dv)
fresh = object.__new__(dv.DoseVoice)
_lvl = os.environ.get("DOSE_CAPTURE_LEVEL", "").strip()
ok(not _lvl, "no capture level is set by default")

trim = object.__new__(DoseVoice)
trim._capture_level = None
trim._forced_card = None
trim._clip_recent = 0.0
trim._speech_level = 2000.0        # a healthy voice level

# not enough audio yet -> no judgement
trim._blocks_seen, trim._clip_blocks = 10, 10
ok(trim.trim_capture_if_clipping() is None,
   "it will not judge the level on a handful of blocks")

# a clean signal -> left alone
trim._blocks_seen, trim._clip_blocks = 200, 0
ok(trim.trim_capture_if_clipping() is None,
   "a clean input is left alone")
ok(trim._capture_level is None,
   "and the hardware level is never touched")

# an occasional loud moment is NOT a level problem
trim._blocks_seen, trim._clip_blocks = 200, 3      # 1.5%
ok(trim.trim_capture_if_clipping() is None,
   "one loud moment does not trigger a change")

# persistent clipping -> stepped down, and it keeps stepping
trim._blocks_seen, trim._clip_blocks = 200, 40     # 20%
lvl = trim.trim_capture_if_clipping()
ok(lvl == 90,
   "persistent clipping DOES step it down as a safety net, starting "
   "from whatever the mixer holds (-> %s%%)" % lvl)
for _ in range(12):
    trim._speech_level = 2000.0
    trim._blocks_seen, trim._clip_blocks = 200, 40
    trim.trim_capture_if_clipping()
ok(trim._capture_level == 30,
   "it keeps stepping down to a floor, never to nothing (%d%%)"
   % trim._capture_level)

ok("audioop.max" in VSRC and "31000" in VSRC,
   "clipping is measured from the samples, not guessed")

print("== 8. it says when the input is too hot ==")
hot = object.__new__(DoseVoice)
hot._capture_level = 70
hot._speech_level = 0.0
hot._max_gain = 4.0
hot._nfloor = 50.0
hot._clip_recent = __import__("time").time()
note = hot._room_note()
ok("too hot" in note,
   "recent clipping is reported plainly: %r" % note)
hot._clip_recent = 0.0
for nf, word in ((40, "quiet"), (250, "some background"),
                 (600, "noisy"), (2000, "too loud")):
    hot._nfloor = nf
    ok(word in hot._room_note(),
       "a floor of %-5d reads as %r" % (nf, hot._room_note()))

print("== 9. nothing boosts the input any more ==")
# Three boosts were multiplying: ALSA capture at 100%, the PipeWire
# source at 150%, and a software auto-gain of up to 40x. That is why
# speech arrived at nearly full scale.
ok(dv.SOURCE_VOLUME <= 1.0,
   "the input is at unity (%.2f), never boosted past it"
   % dv.SOURCE_VOLUME)
ok("1.5 if self._is_usb_name" not in VSRC,
   "the 150%% boost for USB mics is gone")
ok('"Audio/Sink", 0.9' not in VSRC,
   "the speaker is no longer pinned near maximum")
ok(0.4 <= dv.SINK_VOLUME <= 0.8,
   "it sits at %.0f%% — audible across a room without deafening the "
   "microphone inches away" % (dv.SINK_VOLUME * 100))
ok(dv.TARGET_SPEECH_RMS <= 4000,
   "speech should land around %.0f, a third of full scale, with "
   "headroom" % dv.TARGET_SPEECH_RMS)

print("== 10. a level a PREVIOUS version pinned gets walked back ==")
# ALSA remembers the mixer across runs, so deciding to stop touching
# the level does not undo a 100% that an earlier version wrote.
hot2 = object.__new__(DoseVoice)
hot2._capture_level = None
hot2._forced_card = None
hot2._clip_recent = 0.0
hot2._speech_level = 9955.0        # what the device actually reported
hot2._blocks_seen, hot2._clip_blocks = 200, 0
lvl = hot2.trim_capture_if_clipping()
ok(lvl is not None and lvl < 100,
   "speech at 9955 is recognised as too hot and the mic is turned "
   "down (-> %s%%)" % lvl)
ok(hot2._speech_level == 0.0,
   "and the measurement restarts, so it steps once per reading "
   "instead of overshooting")
for _ in range(12):
    hot2._speech_level = 9955.0
    hot2._blocks_seen, hot2._clip_blocks = 200, 0
    hot2.trim_capture_if_clipping()
ok(hot2._capture_level == 30,
   "it keeps walking down to a floor (%d%%)" % hot2._capture_level)

# a healthy level is never touched
calm = object.__new__(DoseVoice)
calm._capture_level = None
calm._forced_card = None
calm._clip_recent = 0.0
calm._speech_level = 2600.0
calm._blocks_seen, calm._clip_blocks = 200, 0
ok(calm.trim_capture_if_clipping() is None,
   "a healthy voice level is left completely alone")
ok(calm._capture_level is None, "the mixer is not touched at all")

print("== 11. the noise level is visible, not guesswork ==")
VSRC = open(os.path.join(ROOT, "dose_voice.py"), errors="ignore").read()
ok("self._snr" in VSRC,
   "the signal-to-noise ratio is measured every block")
ok("_nfloor" in VSRC and "NOISE_GATE_RATIO" in VSRC,
   "and the gate is derived from the room, not a fixed number")
ok("_room_note" in VSRC,
   "Settings says in words whether the room is too loud, so 'it "
   "can't hear me' has somewhere to start")

print()
print("noise suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== NOISE: floor tracks the room, turns still end ===")
