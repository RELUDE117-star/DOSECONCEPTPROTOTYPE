#!/usr/bin/env python3
"""What the hot paths COST, in process spawns, and what must not be cached.

Ryan's brief: find work that takes far too long and condense it without
breaking anything. On a Pi 4 the dominant cost in the audio paths is not
computation — it is process spawns, each roughly 5-15 ms, and the code
was doing dozens of them per capture open and several per spoken
sentence, all to re-derive answers that could not have changed.

This file measures that with a fake subprocess, so the numbers are
counted rather than estimated. It also pins the four places where
caching would be a REGRESSION rather than a saving:

    _apply_capture_level()  pushes a NEW level to the hardware. A
                            cached skip means the level silently never
                            arrives — the exact bug that
                            _max_capture_by_numid's docstring already
                            records happening once.
    force_sink()            a person just tapped a speaker in Settings.
    mic_report()            a person tapped the diagnostic.
    full_mic_test()         a person is watching it test each device.

Caching is for the happy path. Every one of those is somebody waiting
for an answer about the hardware as it is right now.

Run:  python3 tests/test_hot_path_cost.py
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


# ── a fake shell ─────────────────────────────────────────────────────
class FakeResult:
    def __init__(self, stdout="", rc=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = rc


class SpawnCounter:
    """Stands in for subprocess.run and records what was asked for."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, cmd, *a, **kw):
        self.calls.append(list(cmd))
        prog = cmd[0] if cmd else ""
        for key, out in self.responses.items():
            if key in " ".join(str(c) for c in cmd):
                return FakeResult(out)
        if prog == "amixer":
            return FakeResult("")
        return FakeResult("")

    def count(self, prog=None):
        if prog is None:
            return len(self.calls)
        return sum(1 for c in self.calls if c and c[0] == prog)

    def reset(self):
        self.calls = []


class Engine:
    """A DoseVoice with nothing running — just the methods under test.

    Built without __init__ on purpose: __init__ probes real hardware,
    loads models and starts threads, none of which this measures.
    """

    def __new__(cls):
        e = object.__new__(dose_voice.DoseVoice)
        e._capture_level = None
        e._mixer_done = {}
        e._default_set = {}
        e._kicked_at = 0.0
        e._forced_card = None
        e._out_cache = None
        e._forced_sink = None
        return e


def fresh():
    return Engine()


AMIXER_TWO_CONTROLS = ("Simple mixer control 'Mic',0\n"
                       "Simple mixer control 'Mic',1\n")


def instrument(engine, responses=None):
    counter = SpawnCounter(responses)
    dose_voice.subprocess.run = counter
    engine._audio_env = lambda: {}
    return counter


_real_run = dose_voice.subprocess.run


def restore():
    dose_voice.subprocess.run = _real_run


# ── 1. the mixer sweep ───────────────────────────────────────────────
print("\n1. Unmuting a card: measured, then cached")
e = fresh()
c = instrument(e, {"scontrols": AMIXER_TWO_CONTROLS})
e._max_capture_by_numid = lambda card, **k: None   # measured separately
e._max_capture(5)
first = c.count("amixer")
check("one card with two controls costs several amixer spawns",
      first >= 5, "got %d" % first)

c.reset()
e._max_capture(5)
check("the SAME card immediately after costs nothing",
      c.count() == 0, "got %d spawns" % c.count())

c.reset()
e._max_capture(5, force=True)
check("force=True still does the work",
      c.count("amixer") >= 5, "got %d" % c.count("amixer"))

# A full six-card sweep still costs for the FIVE cards not yet done —
# only card 5 was cached above. What must be free is the sweep AFTER a
# sweep, which is what open_capture actually did over and over.
c.reset()
e._unmute_alsa_inputs()
cold_rest = c.count()
check("the first full sweep still pays for the cards not yet done",
      cold_rest > 0, "got %d" % cold_rest)
c.reset()
for _ in range(4):        # open_capture did this ~4x per selection
    e._unmute_alsa_inputs()
    e._max_capture(5)
warm = c.count()
print("      first card: %d spawns   rest of the sweep: %d   "
      "four further sweeps: %d" % (first, cold_rest, warm))
check("repeat sweeps within one selection are free", warm == 0,
      "got %d" % warm)

print("\n2. A hot-plug must invalidate it — a card number can be reused")
e._mixer_cache_clear()
c.reset()
e._max_capture(5)
check("after _mixer_cache_clear the work is redone",
      c.count("amixer") >= 5, "got %d" % c.count("amixer"))

print("\n3. A card that does not exist is not retried in a loop")
e3 = fresh()
c3 = instrument(e3, {})          # amixer returns nothing for every card
e3._max_capture_by_numid = lambda card, **k: None
e3._max_capture(3)
n1 = c3.count()
c3.reset()
e3._max_capture(3)
check("a failed/absent card is stamped, not retried immediately",
      c3.count() == 0, "got %d (first call used %d)" % (c3.count(), n1))

print("\n4. systemctl start pipewire — once, not per capture open")
e4 = fresh()
c4 = instrument(e4)
e4._kick_audio_services()
check("the first kick spawns systemctl", c4.count("systemctl") == 1,
      "got %d" % c4.count("systemctl"))
c4.reset()
for _ in range(10):
    e4._kick_audio_services()
check("ten more kicks in the same moment spawn nothing",
      c4.count() == 0, "got %d" % c4.count())
c4.reset()
e4._kick_audio_services(force=True)
check("force=True kicks for real", c4.count("systemctl") == 1,
      "got %d" % c4.count("systemctl"))

print("\n5. Default sink/source — not three spawns per sentence")
e5 = fresh()
c5 = instrument(e5)
e5._pw_node_id = lambda t, m: None
e5._make_default("usb_speaker", "Audio/Sink", 1.0)
first5 = c5.count("pactl")
check("setting a default costs three pactl spawns", first5 == 3,
      "got %d" % first5)
c5.reset()
for _ in range(8):       # eight sentences in a reply
    e5._make_default("usb_speaker", "Audio/Sink", 1.0)
check("the same target eight more times costs nothing",
      c5.count() == 0, "got %d spawns" % c5.count())

c5.reset()
e5._make_default("hdmi_out", "Audio/Sink", 1.0)
check("a DIFFERENT target is applied immediately",
      c5.count("pactl") == 3, "got %d" % c5.count("pactl"))
c5.reset()
e5._make_default("usb_speaker", "Audio/Sink", 1.0)
check("switching BACK re-applies (the old key was superseded)",
      c5.count("pactl") == 3, "got %d" % c5.count("pactl"))

c5.reset()
e5._make_default("usb_speaker", "Audio/Sink", 1.0, force=True)
check("force=True re-applies", c5.count("pactl") == 3,
      "got %d" % c5.count("pactl"))

c5.reset()
e5._make_default("usb_mic", "Audio/Source", 1.0)
check("a source and a sink are cached independently",
      c5.count("pactl") == 3, "got %d" % c5.count("pactl"))

print("\n6. The windows are finite, so system settings are reasserted")
for name in ("MIXER_REDO_AFTER", "SERVICE_KICK_AFTER",
             "DEFAULT_REAPPLY_AFTER"):
    v = getattr(dose_voice, name, None)
    check("%s is set and finite" % name,
          isinstance(v, float) and 0 < v <= 3600, "got %r" % (v,))

e6 = fresh()
c6 = instrument(e6, {"scontrols": AMIXER_TWO_CONTROLS})
e6._max_capture_by_numid = lambda card, **k: None
e6._max_capture(5)
# pretend the window elapsed
e6._mixer_done[5] = time.time() - dose_voice.MIXER_REDO_AFTER - 1
c6.reset()
e6._max_capture(5)
check("once the window elapses the mixer is forced again",
      c6.count("amixer") >= 5, "got %d" % c6.count("amixer"))

restore()

print("\n6b. Silero is not asked its input names 31 times a second")
# vad_speech_prob() ran `{i.name for i in sess.get_inputs()}` on EVERY
# frame. A frame is 512 samples at 16 kHz — 32 ms — so while anybody was
# speaking this crossed into the ONNX Runtime C API, allocated a NodeArg
# per input and built a fresh set about thirty-one times a second, to
# answer a question fixed for the life of a loaded session.
try:
    import numpy as _np                                      # noqa: E402
except ImportError:
    _np = None


class _NodeArg:
    def __init__(self, n):
        self.name = n


class _FakeSess:
    def __init__(self, names):
        self._names = names
        self.get_inputs_calls = 0
        self.feed_keys = None

    def get_inputs(self):
        self.get_inputs_calls += 1
        return [_NodeArg(n) for n in self._names]

    def run(self, _out, feed):
        self.feed_keys = set(feed)
        return [_np.array([0.7], dtype=_np.float32),
                _np.zeros((2, 1, 128), dtype=_np.float32)]


if _np is None:
    # Same reasoning as test_cloud_stt: a missing dev dependency is not
    # a defect. A suite that reports RED because a machine lacks numpy
    # is the cry-wolf problem the security tooling already had three
    # rounds of — the owner learns to skim past red, and then skims past
    # the red that matters. Say so plainly and skip.
    print("  SKIP numpy not installed here — the Silero frame-cost "
          "checks need it to build a fake session")
else:
    ev = object.__new__(dose_voice.DoseVoice)
    ev._vad_state = None
    s1 = _FakeSess(["input", "sr", "state"])
    ev._load_vad = lambda: s1
    frame = (_np.zeros(dose_voice.DoseVoice.VAD_FRAME, dtype=_np.int16)
             + 100).tobytes()
    prob = None
    for _ in range(120):                  # about four seconds of speech
        prob = ev.vad_speech_prob(frame)
    check("the VAD still returns a probability", prob is not None
          and 0.0 <= prob <= 1.0, "got %r" % (prob,))
    check("120 frames ask the model its input names ONCE",
          s1.get_inputs_calls == 1,
          "got %d calls" % s1.get_inputs_calls)
    check("all three inputs are still fed",
          s1.feed_keys == {"input", "sr", "state"},
          "got %r" % (s1.feed_keys,))

    # A swapped model must not inherit the old session's names.
    s2 = _FakeSess(["input"])
    ev._load_vad = lambda: s2
    ev._vad_state = None
    ev.vad_speech_prob(frame)
    check("a swapped model re-reads its own input names",
          s2.get_inputs_calls == 1, "got %d" % s2.get_inputs_calls)
    check("and is not fed the previous model's inputs",
          s2.feed_keys == {"input"}, "got %r" % (s2.feed_keys,))
    check("the old session is not re-read either",
          s1.get_inputs_calls == 1, "got %d" % s1.get_inputs_calls)

print("\n6c. The half-built by-name mic picker is gone, not half-gone")
check("no mic_device setting (nothing ever read it)",
      '"mic_device": "auto"' not in
      open(os.path.join(os.path.dirname(os.path.dirname(
          os.path.abspath(__file__))), "dose_app.py"),
          encoding="utf-8").read())
check("no by-name PortAudio opener", "def open_named" not in
      open(os.path.join(os.path.dirname(os.path.dirname(
          os.path.abspath(__file__))), "dose_voice.py"),
          encoding="utf-8").read())
check("force_card (by ALSA card number) is still the live path",
      "def force_card" in open(os.path.join(os.path.dirname(
          os.path.dirname(os.path.abspath(__file__))),
          "dose_voice.py"), encoding="utf-8").read())

print("\n7. The four places a cached skip would be a BUG")
src = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "dose_voice.py"), encoding="utf-8").read()


def body_after(marker, span=900):
    i = src.find(marker)
    return src[i:i + span] if i >= 0 else ""


check("_apply_capture_level forces (a new level MUST reach the hardware)",
      "force=True" in body_after("def _apply_capture_level"))
check("force_sink forces (the user just picked that speaker)",
      "force=True" in body_after("def force_sink"))
check("mic_report forces (somebody tapped the diagnostic)",
      "force=True" in body_after("def mic_report", 1200))
check("full_mic_test forces per card (a person is watching it)",
      "self._max_capture(card, force=True)" in src)
check("total capture failure drops the caches so the retry pays in full",
      "self._kicked_at = 0.0" in src and "_mixer_cache_clear()" in src)

print("\n8. The 60-combination arecord sweep remembers its winner")
check("a winning (subdev, base, rate, channels) is recorded per card",
      "_arecord_win[card] = (sd, base, rate, ch)" in src)
check("the remembered combination is tried before the sweep",
      "known = win.get(card)" in src)
check("a combination that stops working is forgotten, not retried first",
      "win.pop(card, None)" in src)
check("the sweep skips the one it just tried",
      "continue     # just tried it" in src)
check("hot-plug clears it, because a card number can be reused",
      "self._arecord_win = {}" in src)

print("\n9. The overlay is not asked to redraw text that has not changed")
# _set_ui_state runs from the listening loop for EVERY audio block, and
# Vosk hands back the same partial over and over while somebody is
# mid-word. Each call was a root.after(0, ...) onto the Tk event queue
# — the same queue that drives the overlay's own animation. Flooding it
# with no-op repaints is how a panel feels sluggish while the machine
# looks idle, which is exactly the complaint that started this work.


class _FakeRoot(object):
    def __init__(self):
        self.posts = []

    def after(self, _ms, fn, *a):
        self.posts.append(a)


class _FakeApp(object):
    def __init__(self):
        self.root = _FakeRoot()

    def _voice_overlay_update(self, *a):
        pass


_ui = object.__new__(dose_voice.DoseVoice)
_ui.app = _FakeApp()
for _ in range(50):
    _ui._set_ui_state("listening", "what time")
check("fifty identical partials cost ONE repaint",
      len(_ui.app.root.posts) == 1, len(_ui.app.root.posts))
_ui._set_ui_state("listening", "what time is")
check("the moment the text changes, it repaints",
      len(_ui.app.root.posts) == 2, len(_ui.app.root.posts))
_ui._set_ui_state("thinking", "what time is")
check("a state change repaints even with the same text",
      len(_ui.app.root.posts) == 3, len(_ui.app.root.posts))
_ui._set_ui_state("speaking", "what time is", "It is ten past six.")
check("a reply repaints", len(_ui.app.root.posts) == 4)
_ui._set_ui_state("idle")
check("going idle repaints, so the panel retires",
      len(_ui.app.root.posts) == 5)
_ui._set_ui_state("idle")
check("...but only once", len(_ui.app.root.posts) == 5)
check("self.state is still assigned every time, deduped or not",
      _ui.state == "idle")
_ui._set_ui_state("listening", "what time")
check("the same text in a NEW turn repaints (it is a new triple in "
      "sequence, not a repeat of the last one)",
      len(_ui.app.root.posts) == 6)
_broken = object.__new__(dose_voice.DoseVoice)
_broken.app = None
_broken._set_ui_state("listening", "x")
check("no app at all is survivable — the engine must never die for "
      "want of a screen", _broken.state == "listening")


print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("=== HOT PATH: the same answer is not re-derived at a cost ===")
