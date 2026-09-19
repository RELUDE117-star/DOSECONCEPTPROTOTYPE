#!/usr/bin/env python3
"""The Mac is the recogniser. The Pi is the parachute.

Ryan, after being told the station was still doing all the work
locally:

    "lets have it put more emphasis so that it focuses on the mac
     first more heavily"
    "It should of course fall back but it shouldnt fall back so
     easily if that makes sense"

It makes sense, and the old policy was the opposite of it. ONE failed
request wrote the Mac off for a flat two minutes. A laptop waking from
sleep, a Wi-Fi roam, a single dropped packet — each cost the next
twenty turns, silently, while the heartbeat still said "paired".

Worse, the Mac was often never asked at all: the live-transcript
shortcut answered the common phrases before any recogniser ran, so a
paired Mac that was up and answering in 1.76 s had a hit counter of
exactly zero.

WHAT THIS FILE PINS

  * a failure is not a verdict — three consecutive refusals are
  * the back-off starts small and grows, and any success clears it
  * a TIMEOUT is not a REFUSAL. A timeout means the Mac answered the
    door and is busy; only refusals count toward giving up
  * a health probe runs between turns, so a Mac that comes back is
    used again in seconds rather than when a timer happens to expire
  * the first request after a cold start gets a longer budget, because
    giving up on the model load is giving up on the whole point

Run:  python3 tests/test_remote_priority.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import dose_remote_stt as R                                   # noqa: E402

FAILURES = []
CHECKS = [0]


def check(label, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


def reset(paired=True):
    R._STATE.update({"down_until": 0.0, "hits": 0, "misses": 0,
                     "last_error": "", "fails": 0, "backoff_at": 0,
                     "timeouts": 0, "healthy_at": 0.0, "health_at": 0.0,
                     "warmed": False, "last_ms": 0})
    # Pretend a conf is loaded, without writing one to disk.
    R._STATE["conf"] = ("192.168.4.21", 8765, "x") if paired else None
    R._STATE["conf_at"] = 1e18 if paired else 0.0


print("\n── one bad second is not a verdict ──────────────────────────")
reset()
R._mark_down("connection refused")
check("one refusal does not stop us asking", R.available() is True,
      R._STATE["down_until"])
R._mark_down("connection refused")
check("two refusals do not either", R.available() is True)
R._mark_down("connection refused")
check("three consecutive refusals do", R.available() is False,
      "this is the only thing that should cost the Mac a turn")

print("\n── and any success wipes the slate ─────────────────────────")
reset()
R._mark_down("x"); R._mark_down("x")
check("two failures are remembered", R._STATE["fails"] == 2)
R._mark_ok(120)
check("a single success clears the count", R._STATE["fails"] == 0)
check("...and the back-off", R._STATE["down_until"] == 0.0)
check("...and the station is available again", R.available() is True)
check("...and the round trip is recorded", R._STATE["last_ms"] == 120)
R._mark_down("x"); R._mark_down("x")
check("it takes three MORE to go down again", R.available() is True)

print("\n── a timeout is not a refusal ──────────────────────────────")
# A timeout means the Mac answered the door and is busy. Writing it off
# for that is exactly the too-easy fallback Ryan objected to.
reset()
for _ in range(6):
    R._mark_soft("no answer in 6s")
check("six timeouts do not write the Mac off", R.available() is True,
      "a busy Mac is still the better recogniser")
check("...but they are counted, so a slow Mac is visible",
      R._STATE["timeouts"] == 6)
check("...and do not pollute the refusal count", R._STATE["fails"] == 0)

print("\n── the back-off grows, and is never permanent ──────────────")
reset()
ladders = []
for _ in range(5):
    for _ in range(R.FAILS_BEFORE_DOWN):
        R._mark_down("refused")
    ladders.append(round(R._STATE["down_until"] - __import__("time").time()))
    R._STATE["down_until"] = 0.0          # pretend the wait elapsed
check("each spell of failure waits longer than the last",
      all(b >= a for a, b in zip(ladders, ladders[1:])), ladders)
check("it starts small", ladders[0] <= 15, ladders[0])
check("and it is capped", max(ladders) <= 200, ladders)
check("the ladder is ordered", R.BACKOFF == sorted(R.BACKOFF), R.BACKOFF)

print("\n── the first request is given time to wake the Mac ─────────")
check("a cold start gets a longer budget than a warm one",
      R.WARM_TIMEOUT > R.TIMEOUT, (R.WARM_TIMEOUT, R.TIMEOUT))
check("the warm budget is long enough to load a model",
      R.WARM_TIMEOUT >= 10.0, R.WARM_TIMEOUT)
check("the normal budget still fits inside a turn",
      R.TIMEOUT <= 7.0, R.TIMEOUT)

print("\n── the Mac is asked whether it is awake, between turns ─────")
src = open(os.path.join(ROOT, "dose_remote_stt.py"),
           encoding="utf-8").read()
check("there is a probe", "def probe(" in src)
check("it is rate limited, so it cannot become traffic",
      "HEALTH_EVERY" in src and "_STATE[\"health_at\"]" in src)
# "Cheap" was pinned at 3 s and the device then measured a 1269 ms
# round trip with three health refusals in one run. A probe budget
# barely above the measured round trip turns ordinary Wi-Fi jitter
# into "the Mac is gone" — the too-easy fallback again, one layer
# down. The probe is off the critical path, so what matters is that it
# cannot become traffic (HEALTH_EVERY does that) and cannot outlast a
# turn, not that it is fast.
check("the probe cannot outlast a turn", R.HEALTH_TIMEOUT <= 8.0,
      R.HEALTH_TIMEOUT)
check("...and is comfortably longer than a measured round trip",
      R.HEALTH_TIMEOUT >= 3.0,
      "1269 ms was measured; 2 s produced three false refusals")
check("it uses the health route, not the transcription route",
      "/health" in src)
check("a failed probe uses the same three-strikes rule",
      "_mark_down(\"health: \"" in src,
      "one bad probe must not take the Mac out of service")
check("a good probe marks the Mac warm, so the next real request "
      "is not paying for a model load",
      "_STATE[\"warmed\"] = True" in src)

print("\n── none of this weakens the address rule ───────────────────")
reset(paired=False)
R._STATE["conf"] = None
R._STATE["conf_at"] = 0.0
check("no configuration means no remote at all",
      R.available() is False)
check("the private-address check still happens before a socket",
      "ipaddress.ip_address(host)" in src
      and src.index("ipaddress.ip_address(host)")
      < src.index("def transcribe("))
check("a public address is still refused",
      "not a local address" in src)
check("proxies are still refused", "ProxyHandler({})" in src)
check("the upload cap is still there", "MAX_UPLOAD" in src)
check("transcribe still cannot raise into a turn",
      "except Exception as e:" in src
      and src.rindex("return \"\"") > src.index("def transcribe("))

print("\n── and the station can SAY where the work happened ─────────")
reset()
R._STATE["hits"] = 7
R._STATE["healthy_at"] = __import__("time").time()
s = R.stats()
for k in ("hits", "misses", "timeouts", "paired", "down_for",
          "healthy", "last_ms", "fails_in_a_row"):
    check("stats() reports %s" % k, k in s, sorted(s))
check("a healthy paired server reads as healthy", s["healthy"] is True)

print("\n── and the turn actually REACHES the Mac ───────────────────")
# Everything above was true on the device and the Mac still answered
# nothing. Paired, healthy, 71 ms away:
#
#   Mac speech server: MAC   turns answered by Mac: 0
#   heard='what do i take today'      engine=whisper-tiny.en
#   heard='how many pills do i have'  fast=8.40  total=12.55
#
# Four real turns, every one heard correctly, every one transcribed on
# the Pi in eight to twelve seconds, with an M1 Pro doing nothing.
#
# finish() reuses the local SPECULATION whenever it is not "going
# cloud" — and that test only ever asked about the cloud. With no
# cloud credential it was always False, so the speculation was
# returned and _better_transcribe, the only place the Mac is asked,
# was never reached at all. The parachute won the race by starting
# first.
DV = open(os.path.join(ROOT, "dose_voice.py"), encoding="utf-8").read()
DVC = "\n".join(l for l in DV.splitlines()
                if not l.lstrip().startswith("#"))

check("the speculation short-circuit asks about the MAC, not only "
      "the cloud",
      "going_remote = (self._remote_ready()" in DVC,
      "with no cloud credential this was always False, and the local "
      "speculation was returned every single turn")
check("the old cloud-only name is gone", "going_cloud" not in DVC)
# AND THEN THAT GUARD INVERTED, on purpose.
#
# `not going_remote` was right when the speculation ran
# whisper-tiny.en on the Pi: reusing it would have thrown away a much
# better recogniser on the LAN. The speculation uses the MAC now, so
# its answer is what a fresh Mac call would return — same audio, same
# model, already finished — and re-asking is a second round trip for
# an identical string. The device measured that mistake: speculation
# hits 0, and stt up from 0.80-0.96s to 1.17-1.49s, because every
# turn was making two requests and the second queued behind the first.
check("a Mac-pointed speculation is reused instead of re-asked",
      "_spec_remote or not going_remote" in DVC,
      "two round trips for an identical string is slower, not safer")
# AND IT ASKS THE RIGHT QUESTION. The first attempt tested who
# ANSWERED the speculation — which is unknowable when finish() runs,
# because the endpoint fires 0.35 s after the last voice and the
# speculation starts at 0.18 s, so it has a sixth of a second of head
# start on a round trip of nearly a second. The answer was therefore
# always "nobody", every turn paid for a second full pass, and the
# device read `spec hits: 0` with stt up from 0.85 s to 1.08-1.49 s.
check("...decided by where it was POINTED, recorded when it started",
      'spec.get("remote")' in DVC
      and '"remote": bool(_remote_stt is not None' in DVC,
      "who answered it is not known yet at that moment")
check("...and it still waits for the answer before using it",
      DVC.index("_spec_remote or not going_remote")
      < DVC.index('spec["done"].wait('))
check("a local speculation still defers to a Mac that is up",
      "not going_remote" in DVC,
      "reusing whisper-tiny.en when an M1 is idle on the LAN is the "
      "bug this whole path exists to avoid")
# TWO MODELS, PICKED BY ROUTE. The speculation fires 0.18s into a
# pause and the endpointer fires at 0.45s, so it has about a quarter
# of a second to come back. Measured on the Mac with identical audio:
# base.en 0.21s, small.en 0.57s, and identical output on every command
# phrase this station is asked. One fits in that window; one does not.
check("the speculative pass asks for the fast model",
      "fast_remote=True" in DVC,
      "0.57s cannot finish inside a 0.25s window, and that is the "
      "whole difference between a 0.45s turn and a 1.1s one")
check("...and the real pass does not",
      "fast_remote=False" in DVC or "fast_remote=False)" in DVC
      or "fast_remote = False" in DVC,
      "a drug name the fast model heard as 'medformin' has to be "
      "asked again properly")
check("...and which model runs is chosen by the URL, not the body",
      '"stt-fast" if fast else "stt"' in src,
      "nothing in the request selects behaviour on that machine")
check("the row says WHICH branch ran, not just that it missed",
      "_spec_why" in DVC and "spec_why" in DV,
      "`spec_hit 0` covers never-started, audio-changed and refused, "
      "and those need three different fixes")
check("...and only when no new speech arrived",
      'spec.get("voice_ts") == self._last_voice_ts' in DVC)
check("the speculation records who answered on a channel the live "
      "turn does not own",
      'box["by"] = getattr(_TL, "engine", "")' in DVC
      and "_TL.engine" in DVC,
      "self._last_engine is guarded by _recording() so this thread "
      "cannot write it — that is the right rule, and the reason a "
      "separate channel is needed")
check("the Mac is still tried before the local models",
      DVC.index("_remote_stt.available()")
      # NOT the exact call text — it gained a `budget` argument and
      # this failed with nothing wrong. Assert the ORDERING, from a
      # part of the call that names what it is.
      < DVC.index("fast, feng = self._fast_transcribe("))
check("a Mac answer is labelled as one in the turn log",
      'self._last_engine = "mac"' in DVC,
      "'engine=whisper-tiny.en' on every row is how this was found")
check("the speculative pass never calls the Mac",
      "allow_cloud=False" in DVC,
      "one turn, one remote request")

print("\n── and the LOG says who actually answered ──────────────────")
# The Mac answered three turns in a row and the turn log credited it
# with one:
#
#   Mac log:  stt 0.98s of audio in 0.78s -> 'What time is it?'
#   turn row: engine=whisper-tiny.en   fast=5.66
#
# _better_transcribe records what answered onto `self`, and the
# speculative pass runs on ANOTHER THREAD at the same time. Whichever
# finished last wrote the record. Being unable to tell where the work
# happened is the exact thing Ryan asked to be able to check, so a log
# that quietly misattributes it is worse than no log.
check("which pass is running is thread-local, not an attribute",
      "_TL = threading.local()" in DVC,
      "both passes are the same object and overlap in time")
check("there is one predicate for it", "def _recording():" in DVC)
check("the speculative worker declares itself",
      "_TL.speculative = True" in DVC)
check("...and clears the flag even if it raises",
      "finally:" in DVC and "_TL.speculative = False" in DVC)
check("every turn-log field is written behind that guard",
      DVC.count("if _recording():") >= 10,
      DVC.count("if _recording():"))
for field in ("_last_engine", "_t_fast", "_t_slow", "_stt_note"):
    # No write to these inside _better_transcribe may be unguarded.
    seg = DVC[DVC.index("def _better_transcribe"):]
    seg = seg[:seg.index("\n    def ", 10)]
    guarded = True
    lines = seg.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().startswith("self.%s =" % field):
            if i == 0 or "if _recording():" not in lines[i - 1]:
                guarded = False
    check("every %s write is guarded" % field, guarded,
          "an unguarded one lets the speculation rename the turn")

print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("remote priority OK")
