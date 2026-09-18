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
      "_ch_l" in CODE and "_ch_r" in CODE and "0.9)" in CODE)
# The re-check is throttled, because doing it every block walked the
# samples four times on the one thread that must drain arecord's pipe.
# The decay is per RE-CHECK now, not per block, so it is faster on
# purpose: 0.9 once a second, not 0.999 forty-seven times a second.
check("the channel is re-decided on a schedule, not every block",
      "% CHANNEL_RECHECK) == 1" in CODE)
check("...about once a second", 20 <= dose_voice.CHANNEL_RECHECK <= 100,
      dose_voice.CHANNEL_RECHECK)
check("in between it is a single tomono with the winning weights",
      CODE.count("audioop.tomono(data, 2, 1, 0)") == 2
      and CODE.count("audioop.tomono(data, 2, 0, 1)") == 2)


def downmix(frames, state):
    """The production rule at a re-check, isolated: 'L' or 'R'."""
    lp = max([abs(v) for v, _ in frames] or [0])
    rp = max([abs(v) for _, v in frames] or [0])
    state["l"] = max(lp, state.get("l", 0) * 0.9)
    state["r"] = max(rp, state.get("r", 0) * 0.9)
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

print("\n── undecided means BOTH, never left by default ──────────────")
# The comparison is `>=`, so before either channel has shown a sample
# the tie resolves to left — every block, for as long as left stays
# silent, which on a capsule wired to the right is forever. The
# re-check cannot rescue it: it looks at ONE block, and a quiet block
# leaves both maxima at 0 and resolves to left again.
#
# The device, with capture finally correct at 48 kHz stereo:
#     blocks/sec: 46.9   (nominal 46.9)
#     live level: peak 0
# A flawless capture delivering silence — the signature that started
# all of this, one layer further in.


def choose(l_max, r_max):
    """The production rule between re-checks: 'SUM', 'L' or 'R'."""
    if l_max == 0 and r_max == 0:
        return "SUM"
    return "L" if l_max >= r_max else "R"


check("nothing proven yet sums the channels", choose(0, 0) == "SUM")
check("left proven takes left", choose(40, 0) == "L")
check("right proven takes right", choose(0, 40) == "R")
check("both proven takes the louder", choose(9, 40) == "R")


def sum_pair(l, r):
    """audioop.tomono(data, 2, 1, 1) on one frame."""
    return l + r


check("a capsule on the RIGHT is audible while undecided",
      sum_pair(0, 29) == 29,
      "picking left here is the bug; averaging would give 14")
check("a capsule on the LEFT is audible while undecided",
      sum_pair(29, 0) == 29)
check("summing is not the averaging that caused the original fault",
      sum_pair(1, 0) == 1 and (1 + 0) // 2 == 0,
      "1+0 survives; (1+0)/2 rounds to zero, which is the whole story")
check("one non-zero block is enough to decide for good",
      choose(*[max(a, b * 0.9) for a, b in ((0, 0), (29, 0))]) in
      ("L", "R"))

check("the summing weights appear in the reader",
      CODE.count("audioop.tomono(data, 2, 1, 1)") == 2,
      "once at the re-check, once between re-checks")
# Positional, not whitespace-exact: three assertions in this suite have
# already broken because they pinned the exact text of a line rather
# than the property, and each time the code was right and the test was
# wrong. Anchor on the summing call, then look forward.
_i_sum = CODE.find("audioop.tomono(data, 2, 1, 1)",
                   CODE.find("elif"))
_i_ge = CODE.find('_ch_l", 0) >= getattr(')
check("the undecided branch is tested BEFORE the >= comparison",
      0 < _i_sum < _i_ge,
      "otherwise >= wins the tie and left is chosen anyway")

# The flapping this replaces: an RMS tie resolves left every time, so a
# right-wired capsule is silence until something is loud enough to
# break the tie, and then the choice changes mid-utterance.
st4 = {}
seq = [downmix([(0, 0)] * 900 + [(0, 6000)] * 20, st4),
       downmix([(0, 0)] * 920, st4),
       downmix([(0, 0)] * 900 + [(0, 6000)] * 20, st4)]
check("a speaking turn on one channel never switches mid-turn",
      len(set(seq)) == 1, seq)

print("\n── the buffer is a REQUEST, and a refusal is not fatal ──────")
# A decode on this board takes about four seconds and ALSA's default
# capture buffer is about half of one, so a bigger buffer is right. I
# asked for five seconds, deployed it, and read this as a fix:
#
#     reopens: 168 -> 4     overruns: 0
#
# The next two fields on the same line said what had really happened:
#
#     rec=0     blocks/sec 0.0
#
# No recorder at all. A USB card need not install a 240,000-frame
# capture buffer, and arecord does not negotiate — it exits. Every
# arecord route failed to open, the walk fell through to endpoints that
# deliver nothing, and the reopen counter stopped climbing because
# there was nothing left to reopen. A zero can mean "fixed" or "gone".

check("there is a ladder, not a single demand",
      isinstance(dose_voice.CAPTURE_BUFFER_LADDER, list)
      and len(dose_voice.CAPTURE_BUFFER_LADDER) >= 3)
check("it is tried largest first",
      dose_voice.CAPTURE_BUFFER_LADDER ==
      sorted(dose_voice.CAPTURE_BUFFER_LADDER, reverse=True),
      dose_voice.CAPTURE_BUFFER_LADDER)
check("the preferred size leads it",
      dose_voice.CAPTURE_BUFFER_LADDER[0] == dose_voice.CAPTURE_BUFFER_US)
check("it ENDS in asking for nothing, so ALSA's own default is always "
      "reachable",
      dose_voice.CAPTURE_BUFFER_LADDER[-1] == 0,
      "without this rung a fussy card is a deaf station")
check("every rung is at least the half-second we started with",
      all(us == 0 or us >= 500000
          for us in dose_voice.CAPTURE_BUFFER_LADDER))
# Pinned to the exact old call shape, which broke the moment the period
# was added beside the buffer. Assert the property instead: both flags
# are appended by one statement guarded by `if us:`, so the rung that
# appends neither cannot leak one.
check("a rung of 0 passes no buffer and no period at all",
      "if us:" in CODE
      and CODE.count("--buffer-time") == 1
      and CODE.count("--period-time") == 1
      and abs(CODE.index("--period-time")
              - CODE.index("--buffer-time")) < 120,
      (CODE.count("--buffer-time"), CODE.count("--period-time")))
check("the accepted size is remembered per card, not re-laddered per "
      "combination",
      "bufs[card] = us" in CODE and "if card in bufs:" in CODE,
      "laddering inside the sweep turns 12 spawns into 60")
check("a remembered size that stops working is forgotten",
      "bufs.pop(card, None)" in CODE)
check("the sweep deadline is still honoured inside the ladder",
      "if deadline is not None and time.time() >= deadline:" in CODE)
check("the label says which buffer was accepted, so the log can be "
      "read without guessing", "buf%.1fs" in CODE and "default buf" in CODE)


RUNGS = [5000000, 2000000, 1000000, 500000, 0]


def first_accepted(rungs, accepts):
    """What the loop settles on, given a card that accepts `accepts`."""
    for us in rungs:
        if us in accepts:
            return us
    return None


check("a card that takes five seconds gets five seconds",
      first_accepted(RUNGS, {5000000, 2000000, 1000000, 500000, 0})
      == 5000000)
check("a card that caps at one second gets one second, not silence",
      first_accepted(RUNGS, {1000000, 500000, 0}) == 1000000)
check("a card that refuses every explicit size still opens",
      first_accepted(RUNGS, {0}) == 0,
      "this is the case that made the station deaf")

print("\n── the winner cache is fed by ACCEPTANCE, not by opening ────")
# The channel fix above was correct and was being bypassed. open_arecord
# remembers the combination that worked for a card and tries it first on
# the next reopen — and it wrote that cache the moment arecord STARTED.
# On this hardware everything starts: plughw converts anything to
# anything, so 16 kHz mono "worked" by that test while delivering the
# averaged silence root-caused above. Cached once, tried first forever,
# and evicted only by a failure to open — which never came.
#
# The device, nine times in twenty seconds:
#   mic arecord plughw:5,0 @16000 1ch ended after 32 blocks (rc=1):
#     read error: Interrupted system call
# 32 blocks is 2.05 s, which is route_floor()'s window; EINTR is
# close_capture()'s own SIGTERM. Open a silent route, measure peak 0,
# reject it, close it, start again — every two seconds, indefinitely.
# Not a crash. The same "shopping forever" this station has done before,
# wearing a new hat.


class FakeVoice(object):
    """Just enough object for the three cache methods, which is the
    point of splitting them out of the walk: the question 'did this
    route prove itself' is answerable without a microphone."""
    _arecord_propose = V._arecord_propose
    _arecord_confirm = V._arecord_confirm
    _arecord_reject = V._arecord_reject


fv = FakeVoice()
fv._arecord_win = {}
fv._arecord_pending = None

check("proposing alone caches nothing",
      (setattr(fv, "_arecord_pending", (5, (0, "plughw", 16000, 1)))
       or fv._arecord_win) == {},
      fv._arecord_win)

check("a rejected combination is never remembered",
      fv._arecord_reject() == (5, (0, "plughw", 16000, 1))
      and fv._arecord_win == {},
      fv._arecord_win)
check("...and the proposal is cleared, so the next route cannot "
      "inherit the blame", fv._arecord_pending is None)

fv._arecord_pending = (5, (0, "plughw", 48000, 2))
check("a route that proved itself IS remembered",
      fv._arecord_confirm() == (5, (0, "plughw", 48000, 2))
      and fv._arecord_win == {5: (0, "plughw", 48000, 2)},
      fv._arecord_win)

# The eviction that matters: a cached combination that later measures
# silent must be dropped, or the sweep underneath never runs again.
fv._arecord_pending = (5, (0, "plughw", 48000, 2))
fv._arecord_reject()
check("a cached combination that goes silent is EVICTED",
      fv._arecord_win == {}, fv._arecord_win)

# ...but only its own. A rejection must not clear a different card's
# good answer, or one dead USB port takes the working microphone with it.
fv._arecord_win = {5: (0, "plughw", 48000, 2)}
fv._arecord_pending = (4, (0, "plughw", 16000, 1))
fv._arecord_reject()
check("rejecting card 4 leaves card 5's proven combination alone",
      fv._arecord_win == {5: (0, "plughw", 48000, 2)}, fv._arecord_win)

# And a rejection carrying a STALE tuple must not evict the entry that
# replaced it.
fv._arecord_win = {5: (0, "plughw", 48000, 2)}
fv._arecord_pending = (5, (0, "hw", 44100, 2))
fv._arecord_reject()
check("a stale proposal cannot evict the combination that replaced it",
      fv._arecord_win == {5: (0, "plughw", 48000, 2)}, fv._arecord_win)

fv._arecord_pending = None
check("confirming with nothing proposed is a no-op",
      fv._arecord_confirm() is None and fv._arecord_win ==
      {5: (0, "plughw", 48000, 2)})
check("rejecting with nothing proposed is a no-op",
      fv._arecord_reject() is None and fv._arecord_win ==
      {5: (0, "plughw", 48000, 2)})
check("propose() clears a leftover proposal",
      (fv._arecord_propose() or fv._arecord_pending) is None)

check("open_arecord only PROPOSES; it no longer writes the cache",
      "self._arecord_pending = (card, (sd, base, rate, ch))" in CODE
      and "self._arecord_win[card] = (sd, base, rate, ch)" not in CODE,
      "caching on open is what made a deaf combination permanent")
check("the walk clears the proposal before each opener",
      "self._arecord_propose()" in CODE and "cap = opener()" in CODE
      and CODE.index("self._arecord_propose()") < CODE.index("cap = opener()"))
# ORDERING IS MEASURED FROM THE LINE IT IS ABOUT, not from the first
# occurrence in the file. My first two attempts here compared against
# CODE.index("close_capture(cap)"), which finds a DIFFERENT, earlier
# call site and made both checks meaningless — they failed against code
# that is correct. Same family as the self-matching greps: anchor to the
# thing, then search forward from it.
_i_conf = CODE.find("kept = self._arecord_confirm()")
_i_rej = CODE.find("dropped = self._arecord_reject()")
check("the walk confirms on the live-route path, before it returns "
      "the stream",
      _i_conf > 0 and 0 < CODE.find("return cap", _i_conf)
      < CODE.find("close_capture(cap)", _i_conf),
      (_i_conf, CODE.find("return cap", _i_conf)))
check("the walk evicts a rejected route BEFORE it closes it",
      _i_rej > 0 and CODE.find("close_capture(cap)", _i_rej) > _i_rej,
      _i_rej)
check("acceptance is handled before rejection, so a live route never "
      "falls through to the evicting branch",
      0 < _i_conf < _i_rej, (_i_conf, _i_rej))
check("the cache is still consulted first on a reopen",
      "known = win.get(card)" in CODE and "cap = attempt(*known)" in CODE,
      "evicting is right; throwing the optimisation away is not")

print("\n── our own teardown is not the microphone dying ─────────────")
# THE REOPEN LOOP, and the most expensive single line in this file's
# history. close_capture() terminates the recorder. The reader thread's
# finally could not see who ended it, so it reported OUR OWN
# terminate() as the microphone dying and set _force_reopen — which
# brought the supervisor straight back to tear down the replacement.
#
# One legitimate reopen, from anything at all, and the station spends
# the rest of its life doing this:
#
#   recorder ... ended after 47 blocks (rc=1):
#     said: Aborted by signal Terminated...     (every second, 332 times)
#   blocks/sec: 47.7      live level: peak 0
#
# py-spy found the engine parked in open_pipe_cmd's settle-sleep,
# reached from _run -> open_capture -> open_arecord -> attempt. Not
# stuck, not crashed: opening a microphone it was about to close.


class Recorder(object):
    def __init__(self, pid):
        self.pid = pid


def reader_should_reopen(voice, proc, stopping=False):
    """The production rule from the reader's finally, isolated."""
    on_purpose = False
    seen = getattr(voice, "_closed_on_purpose", None)
    if seen and proc.pid in seen:
        seen.discard(proc.pid)
        on_purpose = True
    return (not stopping) and (not on_purpose)


class Fake(object):
    pass


fk = Fake()
fk._closed_on_purpose = set()

check("a recorder that died on its own DOES trigger a reopen",
      reader_should_reopen(fk, Recorder(101)) is True)

fk._closed_on_purpose.add(202)
check("a recorder WE closed does not",
      reader_should_reopen(fk, Recorder(202)) is False,
      "this is the loop: our terminate reported as a death")
check("...and the pid is consumed, so a later recorder reusing that "
      "pid is not silently ignored",
      reader_should_reopen(fk, Recorder(202)) is True,
      fk._closed_on_purpose)

fk._closed_on_purpose = {303, 404}
check("overlapping teardowns are told apart by pid",
      reader_should_reopen(fk, Recorder(303)) is False
      and reader_should_reopen(fk, Recorder(404)) is False
      and reader_should_reopen(fk, Recorder(505)) is True,
      "a bare flag set by one teardown would silence the other's "
      "genuine death report")

check("shutting down suppresses it regardless",
      reader_should_reopen(fk, Recorder(606), stopping=True) is False)

check("close_capture records the pid BEFORE it terminates",
      0 < CODE.find("seen.add(h.pid)") < CODE.find("h.terminate()"),
      "recorded after the signal is a race the reader can win")
check("the reader consumes the pid rather than leaving it set",
      "seen.discard(proc.pid)" in CODE)
check("the set cannot grow without bound",
      "if len(seen) > 32:" in CODE,
      "a reader that dies before consuming its entry must not leak")
check("a deliberate close is COUNTED, not silent",
      "self._capture_closes = getattr(" in CODE,
      "suppressing the reopen must not suppress the evidence")
check("the heartbeat reports the two separately",
      "closed on purpose: %d" in CODE,
      "332 reopens turned out to be 332 of the other kind")
check("_force_reopen is guarded by the on-purpose test",
      "if not self._stop.is_set() and not on_purpose:" in CODE)

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
