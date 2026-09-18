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


print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("route liveness OK")
