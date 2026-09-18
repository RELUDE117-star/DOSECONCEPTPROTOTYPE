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
    # the body, to the next def at the same or lower indent
    body = src[i:i + 3000]
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
check("the reader reports the recorder's exit code when capture is lost",
      "capture lost" in src and "rc=%s" in src)

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

print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("route liveness OK")
