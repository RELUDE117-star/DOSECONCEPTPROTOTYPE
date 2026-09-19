#!/usr/bin/env python3
"""Every "can it hear?" answer is PEAK. Never RMS. Fourth time.

Ryan, on the station he had been using successfully all evening:

    "When I try audio test it never hears anything but when I use the
     regular Dose hold logo it works"
    "i just tried the select microhpone thing and although it could
     hear me with the dose logo path when i went to check how much it
     heard it did nothing"

Both halves true at once, and that pairing is the whole diagnosis: the
RECOGNISER heard him, and every INSTRUMENT beside it read zero. The
station was never deaf — the meters were.

Three functions, all answering "did this device deliver signal", all
measuring `audioop.rms()`:

    ingest()'s level probe   -> the Settings mic test and level meter
    _arecord_probe()         -> "select microphone"
    _probe_device()          -> the PortAudio device scan

CLAUDE.md has the measurement, on this exact microphone:

    route                      RMS    PEAK
    card 5 plughw:5,0           0      29
    card 5 via sysdefault:5     0     107
    card 4 (dead device)        0       0

RMS separates none of them. And the level probe is worse than that
table, because it sees ONE 21 ms block at a time: only ~1.2% of blocks
in this room carry a non-zero sample, so a single block's RMS rounds
to zero while somebody is talking. `_voice_mic_test` then asks
`if level <= 5` and reports a dead microphone.

`_arecord_probe` additionally asked for `-c 1`, which is the OTHER
documented root cause — ALSA averaging a stereo capsule's one-LSB
noise floor to literal zero. One function, both of this project's
audio bugs, neither of them new.

Run:  python3 tests/test_meter_peak.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DV = open(os.path.join(ROOT, "dose_voice.py"), encoding="utf-8").read()

FAILURES = []
CHECKS = [0]


def check(label, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


def body(name, after=None):
    """A function's source, cut at the next def at the same depth."""
    seg = DV.split("def %s(" % name)[1]
    end = seg.find("\n    def ")
    return seg[:end if end > 0 else len(seg)]


print("\n── the level probe: what the Settings mic test reads ───────")
# The probe lives inside ingest(), which is a nested closure, so it is
# found by its own marker rather than by a def.
probe = DV.split("lp = self._level_probe")[1]
probe = probe[:probe.index("self._audio_q.put_nowait")]
check("the live level is PEAK",
      "lp[\"max\"] = max(lp[\"max\"], audioop.max(data, 2))" in probe,
      "the key is called max, the docstring says peak, and it "
      "measured rms")
check("...and so is the pre-gain reading",
      'lp["raw"] = max(lp.get("raw", 0), peak_raw)' in probe)
check("no audioop.rms survives in the probe",
      "audioop.rms" not in probe, probe[:200])
check("peak_raw is captured BEFORE any gain is applied",
      DV.index("peak_raw = audioop.max(data, 2)")
      < DV.index("data = audioop.mul(data, 2, self._gain)"),
      "a meter that reports post-gain as 'raw' cannot answer "
      "'did the microphone deliver anything'")
check("...and it is initialised, so an exception cannot leave it unbound",
      "peak_raw = 0" in DV)

print("\n── 'select microphone' ─────────────────────────────────────")
ap = body("_arecord_probe")
check("it records at the card's NATIVE channel count first",
      'for chans in (2, 1):' in ap,
      "-c 1 asks ALSA to average a stereo capsule, and this one's "
      "quiet-room floor averages to exactly zero")
check("...and the channel count is passed through, not hardcoded",
      '"-c", str(chans)' in ap)
check("the verdict is PEAK", "peak = audioop.max(data, 2)" in ap)
check("...and it is the value RETURNED",
      "return (peak," in ap,
      "computing peak and returning rms would be the same bug with "
      "an extra line")
check("RMS is kept, for loudness, not for liveness",
      "rms = audioop.rms(data, 2)" in ap and "peak %d rms %d" in ap)
check("a device that opens and delivers silence is not accepted",
      "if peak <= 0 and chans == 2:" in ap,
      "'it did not error' is not 'it heard something'")
check("...and neither is one that delivers no frames",
      "opened but delivered no audio" in ap)

print("\n── the PortAudio device scan ───────────────────────────────")
pd = body("_probe_device")
check("its verdict is PEAK too", "audioop.max(data, 2)" in pd)
check("...and no rms decides anything in it",
      "audioop.rms" not in pd, pd[-300:])
check("the docstring no longer says RMS",
      "measure real signal (PEAK)" in DV,
      "a docstring that contradicts the code is how all four of "
      "these survived")

print("\n── and the engine's own rule is unchanged ──────────────────")
# The recogniser path was always right — that is WHY the logo worked
# while the meters read zero. This guards against "fixing" it to
# match the broken instruments.
check("route liveness is still peak",
      "ROUTE_LIVE_PEAK" in DV)
check("the energy gate still uses RMS, correctly",
      "rms = rms_raw = audioop.rms(data, 2)" in DV,
      "gating on loudness is what RMS is FOR; the bug was using it "
      "to answer 'is anything there at all'")
check("the clip detector still uses peak",
      "peak = audioop.max(data, 2)" in DV and "peak >= 31000" in DV)

print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("METERS: they measure what the engine measures")
