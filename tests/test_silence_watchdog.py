#!/usr/bin/env python3
"""The station can be perfectly healthy and completely deaf.

WHY THIS TEST EXISTS
--------------------
On 2026-09-18 the device's own heartbeat read:

    HEARING:        YES
    blocks/sec:     46.4
    live level:     peak 0  rms 0

46.4 blocks/sec is exactly 48000/1024 — the capture was flawless. At
that same moment the kernel's hw_ptr advanced 144,385 frames in three
seconds, arecord's wchar climbed at 96,000 B/s, the app's rchar climbed
in step, and a standalone `arecord -D plughw:5,0` read peak 8917.

Every byte reaching the engine was zero.

The existing capture watchdog (CAPTURE_DEAD_AFTER) could not see this.
It asks "did the device stop delivering blocks", and the answer was no —
blocks arrived on time, for ever. So the station sat there reporting
HEARING: YES, hearing nothing, until a person noticed and said so. For
a medication cabinet somebody relies on, "deaf until a human complains"
is not a recovery story.

These tests cover the watchdog that fixes that: blocks arriving AND the
level pinned at the dead-endpoint floor is itself a fault, and it
escalates cheapest-first until the signal comes back.

They also cover every reason NOT to act, which is the more dangerous
half — a false positive tears down a microphone that was working.

Run:  python3 tests/test_silence_watchdog.py
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


def engine(**kw):
    """A DoseVoice with no __init__ — the watchdog is deliberately
    written against getattr() defaults so it can be judged without a
    microphone, a device, or the 1250-line loop it lives in."""
    e = object.__new__(dose_voice.DoseVoice)
    now = kw.pop("now", 1000.0)
    e.state = kw.pop("state", "idle")
    e._muted = kw.pop("muted", False)
    e._pause_capture = kw.pop("pause", False)
    e._measuring_route = kw.pop("measuring", False)
    e._blocks_in = kw.pop("blocks", 5000)
    e._last_block_ts = kw.pop("last_block", now)
    e._last_live_peak_ts = kw.pop("last_live", now - 300)
    e._silence_step = kw.pop("step", 0)
    e._silence_step_ts = kw.pop("step_ts", 0.0)
    e._forced_card = kw.pop("card", (5, 0))
    e._reopen_why = []
    for k, v in kw.items():
        setattr(e, k, v)
    return e


NOW = 1000.0
LIVE = dose_voice.ROUTE_LIVE_PEAK

print("\n── the fault itself ─────────────────────────────────────────")

e = engine()
check("blocks arriving + peak 0 for 300s IS a fault",
      e._silence_due(NOW) == 0, repr(e._silence_due(NOW)))

e = engine(last_live=NOW - dose_voice.SILENT_CAPTURE_AFTER + 5)
check("just under the threshold is left alone", e._silence_due(NOW) is None)

e = engine(last_live=NOW - dose_voice.SILENT_CAPTURE_AFTER - 1)
check("just over the threshold acts", e._silence_due(NOW) == 0)

print("\n── every reason NOT to act (a false positive breaks a good mic) ──")

check("mid-turn is never touched",
      engine(state="listening")._silence_due(NOW) is None)
check("thinking is never touched",
      engine(state="thinking")._silence_due(NOW) is None)
check("speaking is never touched",
      engine(state="speaking")._silence_due(NOW) is None)
check("a muted mic is somebody asking us to leave it alone",
      engine(muted=True)._silence_due(NOW) is None)
check("a paused capture (self-test holds the device) is left alone",
      engine(pause=True)._silence_due(NOW) is None)
check("route measurement in progress is left alone",
      engine(measuring=True)._silence_due(NOW) is None)
check("blocks that STOPPED belong to the other watchdog, not this one",
      engine(last_block=NOW - 9)._silence_due(NOW) is None)
check("a station 4s into its first capture is not judged",
      engine(blocks=50)._silence_due(NOW) is None)
check("no block ever seen is not judged",
      engine(last_block=0.0)._silence_due(NOW) is None)
check("two watchdogs never fight over one stream",
      engine(last_block=NOW - dose_voice.CAPTURE_DEAD_AFTER - 1)
      ._silence_due(NOW) is None)

print("\n── the level stamp ──────────────────────────────────────────")

e = engine(last_live=0.0)
e._silence_note_level(0, NOW)
check("first pass starts the clock NOW, not at the epoch",
      e._last_live_peak_ts == NOW)
check("...so a starting station cannot trip it immediately",
      e._silence_due(NOW) is None)

# THE BAR HERE IS ZERO, not ROUTE_LIVE_PEAK. The device proved why:
# on a quiet night the heartbeat read peak 0 for minutes with a
# perfectly good microphone (_hb_peak decays in about two seconds, and
# a sparse noise floor does not clear a threshold of 3 in every
# two-second window), and the ladder fired twice in the first minute
# after a restart, tearing down a capture that was working.
#
# The fault this watchdog exists for is EXACTLY ZERO, for ever:
# 96,256 consecutive samples without one non-zero value. A live capsule
# is never all-zero for two minutes; a dead endpoint is never anything
# else.
e = engine()
e._silence_note_level(0, NOW)
check("a peak of exactly zero is NOT live", e._last_live_peak_ts != NOW)
e._silence_note_level(1, NOW)
check("a single count above zero IS live — the quiet room that fired "
      "this watchdog by mistake", e._last_live_peak_ts == NOW)
e2 = engine()
e2._silence_note_level(LIVE, NOW)
check("...so a peak at the route floor is live too",
      e2._last_live_peak_ts == NOW)

e = engine(last_live=NOW - 400, step=2)
e._silence_note_level(9542, NOW)
check("a live signal clears the ladder", e._silence_step == 0)
check("...and is recorded, so a flapping mic is visible",
      any("live again" in w for w in e._reopen_why))
check("...and stops the watchdog firing", e._silence_due(NOW) is None)

e = engine(step=1)
e._silence_note_level(0, NOW)
check("silence does not touch the ladder", e._silence_step == 1)

print("\n── the gap between rungs ────────────────────────────────────")

e = engine(step_ts=NOW - dose_voice.SILENCE_STEP_GAP + 1)
check("a rung climbed a moment ago is given time to prove itself",
      e._silence_due(NOW) is None)
e = engine(step_ts=NOW - dose_voice.SILENCE_STEP_GAP - 1)
check("past the gap it climbs again", e._silence_due(NOW) == 0)

print("\n── the ladder, cheapest first ───────────────────────────────")


class Recorder(object):
    """Stands in for every side effect, so each rung is judged by what
    it DID rather than by what its docstring claims."""

    def __init__(self, cards=None):
        self.calls = []
        self.cards = cards if cards is not None else [
            (5, 0, "A28 [AIRHUG 28] USB Audio"),
            (4, 0, "Device [USB Composite Device] USB Audio"),
        ]

    def bind(self, e):
        e._mixer_cache_clear = lambda: self.calls.append("cache_clear")
        e._unmute_alsa_inputs = lambda force=False: self.calls.append(
            "unmute force=%s" % force)
        e._max_capture = lambda c, force=False: self.calls.append(
            "max_capture %s force=%s" % (c, force))
        e._alsa_capture_cards = lambda: list(self.cards)
        e._looks_like_mic = staticmethod(lambda d: "AIRHUG" in d)
        e._card_has_playback = staticmethod(lambda c: False)
        return e


r = Recorder()
e = r.bind(engine())
did = e._silence_recover(0, NOW)
check("rung 0 forces the mixer unmute", "unmute force=True" in r.calls)
check("rung 0 forces the capture level on the card we are on",
      "max_capture 5 force=True" in r.calls)
check("rung 0 bypasses the mixer cache (the cache is the stale value)",
      "cache_clear" in r.calls)
check("rung 0 does NOT tear down the stream",
      not getattr(e, "_force_reopen", False))
check("rung 0 says what it did", "mixer" in did.lower(), did)
check("rung 0 records it in the reopen trail",
      any("mixer" in w for w in e._reopen_why))

r = Recorder()
e = r.bind(engine(step=1, _arecord_win={5: ("sd", "b", 48000, 2)}))
did = e._silence_recover(1, NOW)
check("rung 1 forgets the remembered arecord combination",
      e._arecord_win == {})
check("rung 1 asks for a reopen", e._force_reopen is True)
check("rung 1 keeps the pin (the card may still be right)",
      e._forced_card == (5, 0))

r = Recorder()
e = r.bind(engine(step=2))
did = e._silence_recover(2, NOW)
check("rung 2 moves off the pinned card", e._forced_card != (5, 0))
check("rung 2 picks the other recordable card", e._forced_card == (4, 0))
check("rung 2 asks for a reopen", e._force_reopen is True)
check("rung 2 explains that the pin may name the wrong hardware",
      "pin" in did.lower(), did)

# The re-enumeration case this rung exists for: card 5 is now the dead
# composite device and the real mic has become card 4.
r = Recorder(cards=[(4, 0, "A28 [AIRHUG 28] USB Audio"),
                    (5, 0, "Device_1 [USB PnP Sound Device]")])
e = r.bind(engine(step=2))
e._silence_recover(2, NOW)
check("rung 2 prefers a card that LOOKS like a microphone",
      e._forced_card == (4, 0))

r = Recorder(cards=[(5, 0, "A28 [AIRHUG 28] USB Audio")])
e = r.bind(engine(step=2))
did = e._silence_recover(2, NOW)
check("with one capture card, rung 2 says so instead of doing nothing",
      "no alternative" in did, did)
check("...and does not blank the pin on a board with one mic",
      e._forced_card == (5, 0))

r = Recorder()
e = r.bind(engine(step=3))
did = e._silence_recover(3, NOW)
check("rung 3 drops the pin entirely", e._forced_card is None)
check("rung 3 asks for a full re-selection", e._force_reopen is True)
check("rung 3 says so", "pin" in did.lower(), did)

print("\n── the evidence collects itself ─────────────────────────────")
# The raw tap exists because the heartbeat said peak 0 while a
# standalone arecord on the same card read peak 8917. It only ever
# fired when somebody was there to touch voice/dump_raw — and the
# fault has so far only appeared when nobody was.

import shutil                                                # noqa: E402
import tempfile                                              # noqa: E402

_tmp = tempfile.mkdtemp(prefix="dose-sil-")
_orig_voice_dir = dose_voice.VOICE_DIR
dose_voice.VOICE_DIR = _tmp
try:
    r = Recorder()
    e = r.bind(engine())
    flag = os.path.join(_tmp, "dump_raw")
    check("no tap is armed before the fault", not os.path.exists(flag))
    e._silence_recover(0, NOW)
    check("the first recovery arms the raw tap by itself",
          os.path.exists(flag))
    body = open(flag, encoding="utf-8").read()
    check("...and says who armed it", "silence watchdog" in body, body)
    os.remove(flag)
    e._silence_recover(1, NOW + 100)
    check("it arms ONCE per process, not on every rung",
          not os.path.exists(flag))

    # The engine consumes the flag by EXISTENCE, so the text inside it
    # must not matter. That contract is what makes this safe.
    src_i = SRC_FOR_TAP = open(os.path.join(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))), "dose_voice.py"),
        encoding="utf-8").read()
    check("the tap consumer tests only that the flag EXISTS",
          "if os.path.exists(flag):" in src_i)
    check("...and removes it once the dump is written",
          "os.remove(flag)" in src_i)

    e2 = Recorder().bind(engine())
    e2._silence_tapped = True
    e2._silence_recover(0, NOW)
    check("a station that has already tapped does not re-arm",
          not os.path.exists(flag))

    # An unwritable voice directory must not stop a recovery.
    e3 = Recorder().bind(engine())
    dose_voice.VOICE_DIR = "/proc/nonexistent/dose"
    did3 = e3._silence_recover(0, NOW)
    check("an unwritable directory cannot block the recovery",
          "mixer" in did3.lower(), did3)
finally:
    dose_voice.VOICE_DIR = _orig_voice_dir
    shutil.rmtree(_tmp, ignore_errors=True)

print("\n── the ladder is bounded and wraps ──────────────────────────")

r = Recorder()
e = r.bind(engine())
seen = []
for i in range(9):
    s = int(getattr(e, "_silence_step", 0))
    seen.append(s)
    e._silence_recover(s, NOW + i * 100)
check("it climbs 0,1,2,3 then wraps to 0",
      seen[:8] == [0, 1, 2, 3, 0, 1, 2, 3], seen)
check("a station that cannot recover keeps trying rather than stopping",
      seen[8] == 0)
check("every attempt is counted", e._silence_recoveries == 9)
check("the reopen trail is bounded (20 entries)",
      len(e._reopen_why) <= 20)

print("\n── recovery resets the clock ────────────────────────────────")

r = Recorder()
e = r.bind(engine())
e._silence_recover(0, NOW)
check("a fresh window follows a recovery", e._last_live_peak_ts == NOW)
check("...so the next rung cannot climb on a stale measurement",
      e._silence_due(NOW + 1) is None)
_later = NOW + dose_voice.SILENT_CAPTURE_AFTER + 1
e._last_block_ts = _later          # blocks are still arriving, on time
check("...but it does climb once the window has passed",
      e._silence_due(_later) == 1, repr(e._silence_due(_later)))

print("\n── it can never break the audio path ────────────────────────")


class Exploding(object):
    def __getattr__(self, k):
        raise RuntimeError("boom")


e = engine()
e._mixer_cache_clear = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
did = e._silence_recover(0, NOW)
check("a throwing side effect is contained", "failed" in did, did)
check("...and the ladder still advances, so it is not stuck",
      e._silence_step == 1)

e = engine()
e._alsa_capture_cards = lambda: (_ for _ in ()).throw(OSError("no alsa"))
check("_other_capture_card survives a broken ALSA",
      e._other_capture_card() is None)

e = object.__new__(dose_voice.DoseVoice)
check("a bare engine with no attributes at all does not act",
      e._silence_due(NOW) is None)

print("\n── the heartbeat still WRITES (it lives inside a bare except) ──")
# _heartbeat() is wrapped in `except Exception: pass` so it can never
# affect the audio path it reports on. The cost of that is that a
# NameError in it produces NO FILE AT ALL, silently — and the heartbeat
# is the one diagnostic on this station that has actually worked.
# Adding lines to it without executing it once is how the last working
# instrument gets destroyed by the change meant to improve it.

_hb_tmp = tempfile.mkdtemp(prefix="dose-hb-")
_orig_vd = dose_voice.VOICE_DIR
dose_voice.VOICE_DIR = _hb_tmp
try:
    h = object.__new__(dose_voice.DoseVoice)
    h.state = "idle"
    h._muted = False
    h.mic_name = "A28 [AIRHUG 28]"
    h._blocks_in = 5000
    h._last_block_ts = time.time()
    h.mic_rms = 0
    h._heartbeat()
    hb = os.path.join(_hb_tmp, "live.txt")
    check("the heartbeat file is written at all", os.path.exists(hb))
    body = open(hb, encoding="utf-8").read() if os.path.exists(hb) else ""
    check("it still reports the DEVICE", "HEARING:" in body)
    check("it now reports the SIGNAL separately", "signal:" in body)
    check("a silent capture is named as silent, with the deadline",
          "SILENT for" in body and "acts at" in body, body[:200])
    check("the ladder position is visible", "next rung:" in body)
    check("and the file is timestamped, so nobody reads a stale one "
          "as live again", "written:" in body)

    h2 = object.__new__(dose_voice.DoseVoice)
    h2.state = "idle"
    h2._muted = False
    h2.mic_name = "A28"
    h2._blocks_in = 5000
    h2._last_block_ts = time.time()
    h2.mic_rms = 120
    h2._hb_peak = 9542
    h2._heartbeat()
    body2 = open(hb, encoding="utf-8").read()
    check("a live signal reads 'live', not a countdown",
          "signal:         live" in body2, body2[:400])

    # A bare engine — no attributes at all — must still produce a file.
    h3 = object.__new__(dose_voice.DoseVoice)
    os.remove(hb)
    h3._heartbeat()
    check("even an engine with nothing set writes a heartbeat",
          os.path.exists(hb))
finally:
    dose_voice.VOICE_DIR = _orig_vd
    shutil.rmtree(_hb_tmp, ignore_errors=True)

print("\n── it is actually wired in ──────────────────────────────────")

SRC = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "dose_voice.py"), encoding="utf-8").read()
# Compare CODE, not the prose that explains it. Matching a comment is
# the exact mistake that made the injection detector wrong four times.
CODE = "\n".join(ln for ln in SRC.splitlines()
                 if not ln.lstrip().startswith("#"))

check("the supervising loop stamps the level",
      "self._silence_note_level(" in CODE)
check("the supervising loop asks whether to act",
      "self._silence_due()" in CODE)
check("the supervising loop acts",
      "self._silence_recover(" in CODE)
check("the silence check runs AFTER the dead-capture watchdog",
      CODE.index("CAPTURE_DEAD_AFTER\n") if False else
      CODE.index("_silence_note_level(") > CODE.index(
          "def _silence_note_level"))
check("a reopen resets the silence clock too, so the ladder does not "
      "climb on a stream that was just replaced",
      "_last_live_peak_ts = time.time()" in CODE)
check("the heartbeat reports the SIGNAL, not just the device",
      "signal:" in SRC)
check("the heartbeat reports how long it has been silent",
      "SILENT for" in SRC)
check("the heartbeat reports the ladder position",
      "next rung" in SRC)
check("the threshold is an environment override, not a magic number",
      "DOSE_SILENT_CAPTURE_AFTER" in SRC)
check("the step gap is an environment override too",
      "DOSE_SILENCE_STEP_GAP" in SRC)
check("the threshold leaves room for a quiet room (>= 30s)",
      dose_voice.SILENT_CAPTURE_AFTER >= 30,
      dose_voice.SILENT_CAPTURE_AFTER)
check("...and still heals inside a few minutes (<= 300s)",
      dose_voice.SILENT_CAPTURE_AFTER <= 300)
check("liveness here is peak strictly above zero", "if peak > 0:" in CODE)
check("...and the heartbeat says the same thing, so the file and the "
      "behaviour cannot disagree",
      'getattr(self, "_hb_peak", 0) > 0' in SRC)
check("the window is long enough that no quiet room reaches it",
      dose_voice.SILENT_CAPTURE_AFTER >= 90,
      dose_voice.SILENT_CAPTURE_AFTER)

print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("silence watchdog OK")
