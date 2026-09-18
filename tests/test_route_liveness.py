#!/usr/bin/env python3
"""Capture-route liveness: PEAK, never RMS.

WHY THIS TEST EXISTS
--------------------
The station's microphone "frequently heard nothing" for months. The
hardware was fine the whole time. The cause was one wrong function:
route selection decided whether a capture device was connected by
measuring audioop.rms(), while its own docstring described peak.

Measured on the device in a quiet room, three seconds per route:

    card 5 (AIRHUG, the real mic) @16k    RMS 0    PEAK  29
    card 5 (AIRHUG, the real mic) @48k    RMS 0    PEAK  33
    card 5 via sysdefault                 RMS 0    PEAK 107
    card 4 (dead "USB Composite Device")  RMS 0    PEAK   0

RMS separates none of them. PEAK separates them perfectly.

So the working microphone scored 0, failed the "is this live" test,
and was rejected — and route selection walked on to the end of its
list, where PortAudio device probing wedged inside Pa_StopStream and
never returned. Deaf station.

These tests fail if either half of that comes back: the peak/RMS
distinction, or an unbounded deadline on device selection.

Run:  python3 tests/test_route_liveness.py
"""
import os
import sys
import ast
import wave
import struct
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import audioop
except ImportError:                                   # py3.13 dropped it
    try:
        import audioop_lts as audioop                  # noqa: F401
    except ImportError:
        print("SKIP: no audioop (install audioop-lts)")
        sys.exit(0)

FAILURES = []
CHECKS = [0]


def check(label, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


# ── the recordings, reconstructed ────────────────────────────────────
# A quiet room through a real microphone: mostly zeros, with occasional
# single-sample excursions of a few dozen counts. That is an analog
# noise floor. Its RMS over a 3-second block rounds to zero; its peak
# does not. A dead endpoint is all zeros and has neither.

def quiet_real_mic(peak=29, n=48000, every=4001):
    """Digital silence with a sparse analog noise floor — a real mic."""
    out = bytearray()
    for i in range(n):
        v = peak if i % every == 0 else (-peak if i % every == 1 else 0)
        out += struct.pack("<h", v)
    return bytes(out)


def dead_endpoint(n=48000):
    """Perfect digital silence — a device that is not connected."""
    return b"\x00\x00" * n


def speech(n=48000, level=3000):
    """Something loud enough that even RMS can see it."""
    out = bytearray()
    for i in range(n):
        out += struct.pack("<h", level if (i // 40) % 2 else -level)
    return bytes(out)


print("\n1. The measurement that was wrong")
real = quiet_real_mic()
dead = dead_endpoint()

check("quiet real mic has RMS 0 (this is why RMS cannot be the test)",
      audioop.rms(real, 2) == 0,
      "rms=%d" % audioop.rms(real, 2))
check("quiet real mic has a NON-ZERO peak",
      audioop.max(real, 2) >= 3,
      "peak=%d" % audioop.max(real, 2))
check("dead endpoint has RMS 0", audioop.rms(dead, 2) == 0)
check("dead endpoint has peak 0", audioop.max(dead, 2) == 0)
check("RMS cannot tell a live mic from a dead one",
      audioop.rms(real, 2) == audioop.rms(dead, 2))
check("PEAK can",
      audioop.max(real, 2) != audioop.max(dead, 2))


print("\n2. The threshold, against the real measurements")
import dose_voice                                            # noqa: E402
LIVE = dose_voice.ROUTE_LIVE_PEAK
check("ROUTE_LIVE_PEAK is a small positive integer",
      isinstance(LIVE, int) and 1 <= LIVE <= 10, "got %r" % (LIVE,))

for label, measured_peak, should_pass in (
        ("card 5 AIRHUG @16k",   29,  True),
        ("card 5 AIRHUG @48k",   33,  True),
        ("card 5 sysdefault",   107,  True),
        ("card 4 dead device",    0, False)):
    check("%s: %s" % (label, "accepted" if should_pass else "rejected"),
          (measured_peak >= LIVE) is should_pass,
          "peak=%d threshold=%d" % (measured_peak, LIVE))


print("\n3. Speech is still obviously live either way")
check("speech passes on peak", audioop.max(speech(), 2) >= LIVE)
check("speech would also have passed on RMS (the old test was not "
      "wrong about loud audio — only about quiet rooms)",
      audioop.rms(speech(), 2) > 5)


print("\n4. No unbounded deadline in device selection")
for name in ("PROBE_RATE_SECONDS", "PROBE_CLOSE_TIMEOUT",
             "PROBE_DEVICE_BUDGET", "PROBE_PICK_BUDGET",
             "CAPTURE_OPEN_BUDGET"):
    v = getattr(dose_voice, name, None)
    check("%s is set and finite" % name,
          isinstance(v, float) and 0 < v < 600, "got %r" % (v,))

check("a device probe cannot outlast the whole scan",
      dose_voice.PROBE_DEVICE_BUDGET <= dose_voice.PROBE_PICK_BUDGET)
check("device scanning cannot outlast the whole route walk",
      dose_voice.PROBE_PICK_BUDGET <= dose_voice.CAPTURE_OPEN_BUDGET)


print("\n5. The source itself: no liveness decision may read RMS")
src_path = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "dose_voice.py")
src = open(src_path, encoding="utf-8").read()

# route_floor() and capture_is_live() are the two functions that decide
# "is a microphone attached here". Both must return a PEAK.
for fn in ("def route_floor", "def capture_is_live"):
    i = src.find(fn)
    check("%s() still exists" % fn.split()[-1], i > 0)
    if i < 0:
        continue
    # THE BODY, TO THE NEXT def AT THE SAME INDENT — not a fixed number
    # of characters. This was src[i:i + 3000], and adding a paragraph of
    # explanation to route_floor's docstring pushed the code it was
    # checking past the end of the slice: the assertion failed against
    # a function that had not changed. A window that moves when a
    # comment grows is not measuring the code.
    lines = src[i:].splitlines(True)
    indent = len(lines[0]) - len(lines[0].lstrip())
    body = lines[0]
    for ln in lines[1:]:
        if ln.strip() and (len(ln) - len(ln.lstrip())) <= indent \
                and ln.lstrip().startswith(("def ", "class ", "@")):
            break
        body += ln
    check("%s() measures peak via _peak_rms()" % fn.split()[-1],
          "_peak_rms(" in body)
    check("%s() does not return an RMS as its verdict"
          % fn.split()[-1],
          "return audioop.rms(" not in body
          and "peak = max(peak, audioop.rms(" not in body)

check("no stream teardown calls a bare blocking .stop() any more",
      "st.stop()" not in src and "stream.stop()" not in src)
check("the non-blocking teardown helper exists",
      "def _shut_stream" in src)
check("it aborts rather than drains",
      '"abort"' in src)


print("\n6. No audioop is not permission to skip the measurement")
# Python 3.13 removed audioop from the stdlib — which is what the
# station runs. Every measurement site used to guard the import with a
# fallback that assumed success (route_floor returned 999, meaning
# "accept any route"; capture_is_live returned True). A station without
# audioop did not select a microphone, it took the first thing that
# opened. _peak_rms() has to give the same answer either way.
import builtins                                              # noqa: E402
_real_import = builtins.__import__


def _no_audioop(name, *a, **k):
    if name == "audioop":
        raise ImportError("removed in 3.13")
    return _real_import(name, *a, **k)


for label, data in (("real mic", real), ("dead endpoint", dead),
                    ("speech", speech())):
    with_c = dose_voice._peak_rms(data)
    builtins.__import__ = _no_audioop
    try:
        without_c = dose_voice._peak_rms(data)
    finally:
        builtins.__import__ = _real_import
    check("%s measures identically with and without audioop" % label,
          with_c == without_c, "%r vs %r" % (with_c, without_c))

check("_peak_rms survives empty input", dose_voice._peak_rms(b"") == (0, 0))
check("_peak_rms survives a truncated sample",
      isinstance(dose_voice._peak_rms(b"\x01\x02\x03"), tuple))

# "return 999" appears once more in this file, inside the _peak_rms
# docstring that explains why it used to be there. Strip docstrings
# before asserting, so the explanation cannot be mistaken for the fault.
def _code_only(text):
    tree = ast.parse(text)
    spans = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            spans.append((first.lineno, first.end_lineno))
    lines = text.splitlines()
    for a, b in spans:
        for i in range(a - 1, min(b, len(lines))):
            lines[i] = ""
    return "\n".join(lines)


code = _code_only(src)
check("no measurement site still returns a fake 999",
      "return 999" not in code,
      "still present outside docstrings")
check("no measurement site still short-circuits to True on ImportError",
      "except Exception:\n                return True" not in code)


print("\n7. A real WAV round-trip, end to end")
with tempfile.TemporaryDirectory() as td:
    for label, data, expect_live in (("real", real, True),
                                     ("dead", dead, False)):
        p = os.path.join(td, label + ".wav")
        w = wave.open(p, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(data)
        w.close()
        r = wave.open(p, "rb")
        got = r.readframes(r.getnframes())
        r.close()
        check("%s.wav survives a round trip and is judged correctly"
              % label,
              (audioop.max(got, 2) >= LIVE) is expect_live,
              "peak=%d" % audioop.max(got, 2))


print("\n8. A live route must not be closed and reopened")
# The walk used to: open a route, measure it, CLOSE it, then call the
# same opener again to get the stream it would actually use. Two opens
# of the same USB device a fraction of a second apart is a race against
# the kernel releasing the PCM, and the device lost it in a loop — 33
# selections in three minutes, each one correctly choosing card 5,0 and
# then throwing it away. Its own stderr recorded both halves:
#
#   arecord: pcm_read:2272: read error: Interrupted system call
#       (our SIGTERM to the probe recorder; EINTR is fatal to arecord)
#   arecord sysdefault card 4: audio open error: Device or resource busy
#       (the reopen arriving before the kernel had let go)
#
# A live, non-speaker route now returns the stream it just measured.
i = src.find("def open_capture")
walk = src[i:i + 14000] if i > 0 else ""
check("open_capture() exists", i > 0)
check("a live non-speaker route returns its stream from the loop",
      "return cap" in walk and "KEEP THE STREAM WE ALREADY HAVE" in walk)
check("the old close-then-reopen pair is gone",
      "floor = route_floor()\n                close_capture(cap)" not in src)
check("the recorder is spawned in its own session, so a group signal "
      "cannot kill it",
      "start_new_session=True" in src)
check("every reopen trigger records a reason",
      src.count("self._note_reopen(") >= 4)
# The reader's message changed: it now reports the exit code AND how
# many blocks it delivered AND why it ended, because "0 blocks" was
# indistinguishable from a silent microphone and cost a whole evening.
check("the reader reports the recorder's exit code when capture is lost",
      "rc=%s" in src and "ended after %d blocks" in src)
check("it says WHY the reader stopped, not just that it did",
      "stop event was ALREADY SET at thread start" in src
      and "recorder closed its pipe (EOF)" in src)
check("start() clears the stop latch, so one stop() cannot deafen the "
      "station for ever",
      "self._stop.clear()" in src
      and src.index("self._stop.clear()") < src.index("def stop(self)"))

print("\n9. Measurement must beat mute and barge-in")
# route_floor() decides a device is real by reading _audio_q for 1.6 s.
# Every block can be dropped before reaching that queue: while muted,
# and while the engine is SPEAKING, when audio goes to barge-in
# detection and returns.
#
# Selection runs at startup — exactly when the station plays its
# greeting and prewarms replies. So the walk measured an empty queue and
# scored EVERY route digitally silent, including the pinned AIRHUG that
# measures peak 29-107 standalone. From the device, 2026-09-17 22:05:
#
#   arecord FORCED card 5,0: opened, peak 0 — rejected
#   ... every other route:   peak 0 — rejected
#   took: 63.6s (budget 45s)   chose: NOTHING
#
# The microphone was fine. The ruler was being held while the engine
# talked over it.
import queue as _q                                            # noqa: E402
import threading as _th                                       # noqa: E402
import time as _time                                          # noqa: E402


def _blk(peak=29, n=512):
    return b"".join(struct.pack("<h", peak if i == 0 else 0)
                    for i in range(n))


def _measure_while(state, muted):
    """Replicate ingest's gate and route_floor's read, together."""
    eng = object.__new__(dose_voice.DoseVoice)
    eng._audio_q = _q.Queue(maxsize=200)
    eng.state = state
    eng._muted = muted
    eng._measuring_route = False
    eng._native_rate = dose_voice.SAMPLE_RATE
    eng._detect_barge_in = lambda d: None
    stop = _th.Event()

    def feed():
        while not stop.is_set():
            eng._last_block_ts = _time.time()
            measuring = getattr(eng, "_measuring_route", False)
            if eng._muted and not measuring:
                _time.sleep(0.01)
                continue
            if eng.state == "speaking" and not measuring:
                eng._detect_barge_in(_blk())
                _time.sleep(0.01)
                continue
            try:
                eng._audio_q.put_nowait(_blk())
            except _q.Full:
                pass
            _time.sleep(0.01)

    t = _th.Thread(target=feed, daemon=True)
    t.start()
    eng._measuring_route = True
    _time.sleep(0.05)
    peak = 0
    end = _time.time() + 0.5
    while _time.time() < end:
        try:
            d = eng._audio_q.get(timeout=0.2)
        except _q.Empty:
            continue
        p, _r = dose_voice._peak_rms(d)
        peak = max(peak, p)
    eng._measuring_route = False
    stop.set()
    return peak


for _state, _muted, _label in (("idle", False, "idle and unmuted"),
                               ("speaking", False,
                                "while SPEAKING (what startup does)"),
                               ("idle", True, "while MUTED")):
    _pk = _measure_while(_state, _muted)
    check("a real mic measures LIVE %s" % _label,
          _pk >= LIVE, "peak=%d threshold=%d" % (_pk, LIVE))

check("ingest lets a measurement through mute and speaking",
      "_measuring_route" in src)
check("route_floor raises and ALWAYS lowers the flag (finally)",
      src.count("self._measuring_route = False") >= 2)
check("the trail records how many blocks a route delivered, so "
      "'silent' can be told from 'nothing arrived'",
      "_last_route_blocks" in src)
check("the selection dump records the engine state during the walk",
      "engine state during the walk" in src)

print("\n10. The microphone is handed back before a self-restart")
# dose_app.py restarts itself with os.execv() after an update. execv
# REPLACES THE PROCESS IMAGE: no finally runs, no atexit fires, every
# thread ceases. The recorder is a child in its own session, so it
# survives all of that — still holding the USB device — and the process
# taking our place is deaf.
#
# Measured on the device. Two selections, both "#1 since start", 54
# seconds apart, while systemd reported ZERO restarts (the app restarted
# ITSELF):
#
#   22:11:51  took 2.0s   chose card 5,0   peak 9542, 88 blocks
#   22:12:45  took 63.8s  chose NOTHING    every route, 0 blocks
#
# "audio blocks delivered since start: 0" — no audio reached the new
# process at all.
import subprocess as _sp                                      # noqa: E402
import threading as _th2                                      # noqa: E402

_eng = object.__new__(dose_voice.DoseVoice)
_eng._stop = _th2.Event()
_eng._pw_proc = None
_rec = _sp.Popen(["sleep", "300"], start_new_session=True,
                 stdout=_sp.PIPE)
_eng._cap_proc = _rec
check("the stand-in recorder is in its own session, like the real one",
      os.getsid(_rec.pid) == _rec.pid)
check("it is alive before release", _rec.poll() is None)

_t0 = __import__("time").time()
_eng.release_audio()
_el = __import__("time").time() - _t0

check("release_audio() reaps the recorder", _rec.poll() is not None,
      "rc=%s" % _rec.poll())
check("it TERMs rather than KILLs, so the PCM is released cleanly",
      _rec.poll() == -15, "rc=%s (-15 = SIGTERM)" % _rec.poll())
check("it sets the stop event", _eng._stop.is_set())
check("it clears the handle", _eng._cap_proc is None)
check("it is fast enough to sit in front of execv", _el < 5,
      "took %.2fs" % _el)
_gone = False
try:
    os.kill(_rec.pid, 0)
except OSError:
    _gone = True
check("no zombie is left behind", _gone)
check("calling it twice is safe", (_eng.release_audio(), True)[1])
_e2 = object.__new__(dose_voice.DoseVoice)
_e2._stop = _th2.Event()
_e2._pw_proc = None
_e2._cap_proc = None
check("safe with no recorder at all", (_e2.release_audio(), True)[1])

_app = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "dose_app.py"), encoding="utf-8").read()
_i = _app.index("def _restart_app")
_body = _app[_i:_i + 1600]
# Compare CODE, not the comment that explains the fix — "os.execv"
# appears in that prose, and matching it there is the same mistake the
# injection detector made four times.
_code = "\n".join(ln for ln in _body.splitlines()
                  if not ln.lstrip().startswith("#"))
check("_restart_app releases audio BEFORE os.execv",
      "release_audio()" in _code and "os.execv" in _code
      and _code.index("release_audio()") < _code.index("os.execv"))
check("the live recorder is tracked so it can be released",
      "self._cap_proc = p" in src)

print("\nThe sweep is bounded, and does not ask for devices that "
      "cannot exist")
# From the device, at startup:
#
#     took: 64.0s (budget 45s)
#     chose: NOTHING — no route opened
#     route walks that hit their budget: 1
#
# and the log full of
#
#     arecord -D plughw:5,2 ...: audio open error: No such file
#
# Card 5 has subdevices_count: 1. Three quarters of the sweep was
# asking the kernel for devices that cannot exist, and the budget was
# consulted only BETWEEN routes, so one route could overrun it by
# twenty seconds while the report said the budget had been respected.

_src = src if "src" in dir() else open(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "dose_voice.py"), encoding="utf-8").read()
_code = "\n".join(ln for ln in _src.splitlines()
                   if not ln.lstrip().startswith("#"))

check("the subdevice list comes from the kernel",
      "def _capture_subdevices" in _code
      and "subdevices_count:" in _src)
check("it falls back to [0], never to a guess at more",
      "return [0]" in _code)
check("a card that does not exist yields exactly one subdevice",
      dose_voice.DoseVoice._capture_subdevices(9999) == [0])
check("a nonsense card number is survivable",
      dose_voice.DoseVoice._capture_subdevices(-3) == [0])
check("the sweep iterates only real subdevices",
      "real = self._capture_subdevices(card)" in _code
      and "for d in [device] + real:" in _code)
check("the old 0,1,2,3 guess is gone",
      "for d in (device, 0, 1, 2, 3):" not in _code)

check("open_arecord takes a deadline", "def open_arecord(card, device=0, "
      "deadline=None):" in _code)
check("...and checks it INSIDE the sweep, not after it",
      "if deadline and time.time() > deadline:" in _code)
check("...and says so when it bails",
      "arecord sweep on card" in _src and "deadline" in _src)
check("the walk's deadline exists before the openers are built",
      _code.index("_walk_box = [") < _code.index("open_arecord(c, d, deadline="))
check("both arecord routes are given it",
      _code.count("open_arecord(c, d, deadline=walk_deadline())") == 2)
check("the walk clock is re-stamped when the walk actually starts",
      "_walk_box[0] = walk_end = t_walk + CAPTURE_OPEN_BUDGET" in _code)

# The arithmetic the fix rests on.
_combos = 2 * 3 * 2          # bases x rates x channels
check("one real subdevice means %d attempts, not %d"
      % (_combos, _combos * 4), _combos * 1 == 12)

print("\nThe capture buffer survives a decode")
# The recorder said it: "overrun!!! (at least 4201.874 ms long)", and
# arecord exits on an overrun — the microphone was being destroyed
# about thirty times a minute. A controlled test on the device settles
# the mechanism: the same command piped into a prompt reader survives
# 25 s; piped into a deliberately slow one it overruns in under one.
#
# It is not the downmix. Measured in the app's own interpreter with the
# real C audioop, four passes over a block cost 0.023 ms against a
# 21.3 ms budget. The reader stalls because a decode takes four seconds
# on a Pi and the whole machine is busy.
check("arecord is given an explicit buffer",
      "--buffer-time" in _code)
check("it is a named constant, not a number in a list",
      "CAPTURE_BUFFER_LADDER" in _code and "str(us)" in _code
      and "--buffer-time\", str(5000000)" not in _code)
check("it is an environment override",
      "DOSE_CAPTURE_BUFFER_US" in _src)
check("it is longer than a decode on this board (>= 4s)",
      dose_voice.CAPTURE_BUFFER_US >= 4_000_000,
      dose_voice.CAPTURE_BUFFER_US)
check("...and not absurd (<= 20s)",
      dose_voice.CAPTURE_BUFFER_US <= 20_000_000)
# AND THE REFUSAL PATH, which is the half I shipped without.
# Asking for five seconds on this card meant arecord did not open at
# all: rec=0, blocks/sec 0.0, and a reopen counter that stopped
# climbing because there was nothing left to reopen. The preferred
# size is a request; the station must still hear when it is declined.
check("a card that refuses the preferred size still opens",
      dose_voice.CAPTURE_BUFFER_LADDER[-1] == 0,
      dose_voice.CAPTURE_BUFFER_LADDER)

print("\n── a quiet microphone is not a dead one, and 1.6s cannot tell ──")
# With the channel fix in and the buffer negotiated, the device was
# delivering 45.9 blocks/sec at live level peak 11 — a working
# microphone — and TERMing it every 2.07 seconds, 146 times in six
# minutes, always at 94 blocks. 94 blocks at 47/sec is 1.6 s, which was
# route_floor's entire window.
#
# The arithmetic was already in this repo, about the silence watchdog:
# in this room about 1.2% of blocks carry a non-zero sample (38 of
# 3100, measured on the device). So:
P_SIGNAL = 38.0 / 3100.0
BLOCKS_PER_S = 46.9


def p_all_silent(seconds):
    """Chance a window of this length sees NOTHING, on a live mic."""
    return (1.0 - P_SIGNAL) ** (seconds * BLOCKS_PER_S)


check("a 1.6s window misses a live microphone far too often",
      p_all_silent(1.6) > 0.30,
      "%.0f%% of windows see nothing at all" % (100 * p_all_silent(1.6)))
check("the patience window almost never does",
      p_all_silent(dose_voice.ROUTE_FLOOR_PATIENCE) < 0.10,
      "%.1f%%" % (100 * p_all_silent(dose_voice.ROUTE_FLOOR_PATIENCE)))
check("patience is longer than the first look",
      dose_voice.ROUTE_FLOOR_PATIENCE > dose_voice.ROUTE_FLOOR_SECONDS,
      (dose_voice.ROUTE_FLOOR_SECONDS, dose_voice.ROUTE_FLOOR_PATIENCE))
check("...and still fits inside the walk's budget several times over",
      dose_voice.ROUTE_FLOOR_PATIENCE * 4
      <= dose_voice.CAPTURE_OPEN_BUDGET,
      (dose_voice.ROUTE_FLOOR_PATIENCE, dose_voice.CAPTURE_OPEN_BUDGET))
check("both are environment-overridable on the device",
      "DOSE_ROUTE_FLOOR_SECONDS" in _src
      and "DOSE_ROUTE_FLOOR_PATIENCE" in _src)

check("listening STOPS the moment the route proves itself",
      "if peak >= ROUTE_LIVE_PEAK:" in _code and "break" in _code,
      "otherwise every good route costs the full patience")
check("a route delivering NO blocks is not given the patience",
      "if now >= floor_end and seen == 0:" in _code,
      "more time cannot produce blocks that are not coming")
check("the walk's deadline is passed in and bounds the patience",
      "def route_floor(seconds=ROUTE_FLOOR_SECONDS," in _code
      and "route_floor(deadline=walk_end)" in _code
      and "patient_end = min(patient_end, deadline)" in _code)
check("the trail records how long each route was actually listened to",
      "_last_route_secs" in _code and "blocks in %.1fs" in _code,
      "'peak 0, rejected' read the same after 1.6s and after none")


def floor_sim(samples, seconds, patience, live_peak=3, rate=46.9):
    """route_floor's stopping rule, isolated: returns (peak, secs)."""
    peak = 0
    n = 0
    for v in samples:
        t = n / rate
        if peak >= live_peak:
            break
        if t >= patience:
            break
        if t >= seconds and n == 0:
            break
        n += 1
        peak = max(peak, v)
    return peak, n / rate


# A live mic whose first signal arrives at 3.5 s: rejected at 1.6 s,
# found with patience.
late = [0] * 165 + [11] * 5 + [0] * 200
check("a live route whose first signal is late is now FOUND",
      floor_sim(late, 1.6, 5.0)[0] >= 3, floor_sim(late, 1.6, 5.0))
check("...and was rejected before", floor_sim(late, 1.6, 1.6)[0] < 3)

early = [0] * 10 + [29] * 5 + [0] * 400
pk, secs = floor_sim(early, 1.6, 5.0)
check("a route that proves itself early is not held for the patience",
      pk >= 3 and secs < 1.0, (pk, secs))

dead = [0] * 500
pk, secs = floor_sim(dead, 1.6, 5.0)
check("a genuinely dead route is still rejected",
      pk == 0, (pk, secs))
check("...and costs the patience, which is the price of not being "
      "wrong about a real one", 4.5 <= secs <= 5.1, secs)
check("the ladder descends from the preferred size",
      dose_voice.CAPTURE_BUFFER_LADDER[0] == dose_voice.CAPTURE_BUFFER_US
      and dose_voice.CAPTURE_BUFFER_LADDER ==
      sorted(dose_voice.CAPTURE_BUFFER_LADDER, reverse=True),
      dose_voice.CAPTURE_BUFFER_LADDER)
check("-q is gone, so the recorder can still say why it stopped",
      '"-q"' not in _code.split("def open_arecord")[1][:1500])

print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("route liveness OK")
