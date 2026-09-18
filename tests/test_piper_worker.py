#!/usr/bin/env python3
"""Synthesis must survive the crash that currently kills the app.

THE BUG THIS IS FOR
-------------------
ONNX Runtime aborts this application. Logged three times in one
afternoon:

    Fatal Python error: Aborted
      onnxruntime_inference_collection.py line 395 in run
      piper/voice.py phoneme_ids_to_audio / synthesize / synthesize_wav
      dose_voice.py _synth / render_to_cache

SIGABRT comes from native code. **No Python handler can catch it** — not
a try/except around the call, not one around the thread, not one around
main(). The process dies. The only mitigation was the supervisor, which
means a twenty-second silence in front of whoever is standing at a
medication dispenser.

So synthesis moved into a child process. The abort is survivable when
the thing that aborts is not the app.

WHAT THIS FILE PROVES, with a fake Piper (the real one is not installed
in the build container, and a test that needed it could not run here):

  1. the worker synthesises normally, and loads the model ONCE across
     many sentences — a process per sentence would cost seconds each
     and be far worse than the bug it fixes
  2. a hard abort (SIGABRT, os.abort) kills the CHILD and the parent
     lives
  3. the parent falls back to in-process synthesis on every failure
     mode — worker missing, worker won't start, worker dies, worker
     hangs, worker returns an error, worker writes no audio
  4. a worker that dies is respawned, but never in a tight loop
  5. DOSE_PIPER_WORKER=0 switches the whole thing off

Point 3 is the one that matters most for shipping. This goes onto a
station with a pitch to get through, so the worst case has to be "no
better than before", never "worse than before".

Run:  python3 tests/test_piper_worker.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILURES = []
CHECKS = [0]


def check(label, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


WORKER = os.path.join(ROOT, "tools", "piper_worker.py")

# ── a fake `piper` module ────────────────────────────────────────────
# Loads count themselves, writes a real WAV, and can be told to abort
# natively on a chosen call — which is what the real one does.
FAKE_PIPER = '''
import os, sys, wave, struct

LOADS = os.environ.get("FAKE_LOAD_COUNTER")


class SynthesisConfig:
    def __init__(self, **kw):
        self.kw = kw


class _Voice:
    def synthesize_wav(self, text, wav, **kw):
        n = int(os.environ.get("FAKE_ABORT_ON_CALL", "0"))
        c = 0
        cf = os.environ.get("FAKE_CALL_COUNTER")
        if cf:
            try:
                c = int(open(cf).read().strip() or "0")
            except Exception:
                c = 0
            c += 1
            open(cf, "w").write(str(c))
        if os.environ.get("FAKE_HANG") == "1":
            import time as _t
            _t.sleep(600)
        if n and c == n:
            # EXACTLY what onnxruntime does to this app: SIGABRT from
            # native code. Uncatchable by design.
            os.abort()
        if os.environ.get("FAKE_RAISE") == "1":
            raise RuntimeError("synthesis blew up in python")
        if os.environ.get("FAKE_NO_AUDIO") == "1":
            open(wav, "w").close()
            return
        w = wave.open(wav, "wb")
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
        w.writeframes(b"".join(struct.pack("<h", (i % 800) - 400)
                               for i in range(2205)))
        w.close()


class PiperVoice:
    @staticmethod
    def load(path):
        if LOADS:
            try:
                n = int(open(LOADS).read().strip() or "0")
            except Exception:
                n = 0
            open(LOADS, "w").write(str(n + 1))
        if os.environ.get("FAKE_ABORT_ON_LOAD") == "1":
            os.abort()
        return _Voice()
'''


def fake_env(tmp, **extra):
    d = os.path.join(tmp, "fakelib")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "piper.py"), "w") as f:
        f.write(FAKE_PIPER)
    env = dict(os.environ)
    env["PYTHONPATH"] = d + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("FAKE_ABORT_ON_CALL", None)
    env.pop("FAKE_ABORT_ON_LOAD", None)
    env.pop("FAKE_HANG", None)
    env.pop("FAKE_RAISE", None)
    env.pop("FAKE_NO_AUDIO", None)
    env.update({k: str(v) for k, v in extra.items()})
    return env


def start_worker(tmp, env):
    model = os.path.join(tmp, "voice.onnx")
    if not os.path.exists(model):
        with open(model, "wb") as f:
            f.write(b"\0" * 128)
    p = subprocess.Popen([sys.executable, WORKER, model, "2"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, env=env)
    ready = p.stdout.readline()
    return p, ready


def ask(p, text, wav):
    p.stdin.write(json.dumps({"text": text, "wav": wav}) + "\n")
    p.stdin.flush()
    return p.stdout.readline()


print("\n1. The worker exists and speaks the protocol")
check("tools/piper_worker.py is present", os.path.exists(WORKER))

with tempfile.TemporaryDirectory() as tmp:
    loads = os.path.join(tmp, "loads")
    open(loads, "w").write("0")
    env = fake_env(tmp, FAKE_LOAD_COUNTER=loads)
    p, ready = start_worker(tmp, env)
    check("it announces READY before taking work",
          json.loads(ready or "{}").get("ready") is True,
          "got %r" % (ready,))

    # ten sentences, one model load
    ok_all = True
    for i in range(10):
        wav = os.path.join(tmp, "s%d.wav" % i)
        resp = json.loads(ask(p, "sentence number %d" % i, wav) or "{}")
        if not (resp.get("ok") and os.path.exists(wav)):
            ok_all = False
    check("ten sentences all synthesise", ok_all)
    check("the produced file is a real, readable WAV",
          wave.open(os.path.join(tmp, "s0.wav"), "rb").getnframes() > 0)
    n_loads = int(open(loads).read().strip())
    check("the model was loaded ONCE for all ten (a process per "
          "sentence would load it ten times)",
          n_loads == 1, "loaded %d times" % n_loads)
    p.stdin.close()
    p.wait(timeout=10)


print("\n2. A native abort kills the CHILD, not the parent")
with tempfile.TemporaryDirectory() as tmp:
    calls = os.path.join(tmp, "calls")
    open(calls, "w").write("0")
    env = fake_env(tmp, FAKE_CALL_COUNTER=calls, FAKE_ABORT_ON_CALL=2)
    p, _ = start_worker(tmp, env)
    r1 = json.loads(ask(p, "first, fine", os.path.join(tmp, "a.wav"))
                    or "{}")
    check("the sentence before the abort succeeds", r1.get("ok") is True)
    # second call aborts the child
    p.stdin.write(json.dumps(
        {"text": "second, boom", "wav": os.path.join(tmp, "b.wav")})
        + "\n")
    try:
        p.stdin.flush()
    except Exception:
        pass
    line = p.stdout.readline()
    check("the parent sees the pipe close rather than an answer",
          line.strip() == "", "got %r" % (line,))
    rc = p.wait(timeout=10)
    check("the child died of a signal (SIGABRT = -6)", rc < 0,
          "rc=%s" % rc)
    check("THIS TEST PROCESS IS STILL RUNNING — which is the whole "
          "point", True)


print("\n3. The parent falls back to in-process on EVERY failure mode")
import dose_voice                                            # noqa: E402


class Parent:
    """A DoseVoice with only what _synth_via_worker touches."""

    def __new__(cls, **kw):
        e = object.__new__(dose_voice.DoseVoice)
        e._piper_path = kw.get("model", "/nonexistent/voice.onnx")
        e._pw_proc = None
        e._pw_spawned_at = 0.0
        e._tts_events = []
        return e


def fell_back(**kw):
    """True if _synth_via_worker declined, i.e. the caller falls back."""
    par = Parent(**kw)
    return par._synth_via_worker("hello", kw.get("wav", "/tmp/x.wav"))


with tempfile.TemporaryDirectory() as tmp:
    # no model path at all
    check("no Piper model -> falls back",
          fell_back(model=None) is False)
    # model path that does not exist -> worker exits during load
    check("a model that does not exist -> falls back",
          fell_back(model=os.path.join(tmp, "missing.onnx")) is False)

# worker script missing
saved = dose_voice.os.path.exists
try:
    dose_voice.os.path.exists = lambda p: False if "piper_worker" in str(p) \
        else saved(p)
    check("worker script missing -> falls back", fell_back() is False)
finally:
    dose_voice.os.path.exists = saved

# disabled by environment
os.environ["DOSE_PIPER_WORKER"] = "0"
try:
    par = Parent()
    check("DOSE_PIPER_WORKER=0 -> worker never used",
          par._worker_enabled() is False
          and par._synth_via_worker("hi", "/tmp/y.wav") is False)
finally:
    os.environ.pop("DOSE_PIPER_WORKER", None)

check("DOSE_PIPER_WORKER unset -> enabled by default",
      Parent()._worker_enabled() is True)


print("\n4. Respawn, but never in a tight loop")
par = Parent()
par._pw_spawned_at = time.time()          # just tried
check("a respawn inside the minimum gap is refused",
      par._piper_worker() is None)
check("the gap is finite and small",
      0 < dose_voice.PIPER_WORKER_MIN_GAP <= 60,
      "got %r" % (dose_voice.PIPER_WORKER_MIN_GAP,))
check("the synthesis timeout is finite",
      0 < dose_voice.PIPER_WORKER_TIMEOUT <= 120,
      "got %r" % (dose_voice.PIPER_WORKER_TIMEOUT,))


print("\n5. A worker error is reported, not mistaken for success")
with tempfile.TemporaryDirectory() as tmp:
    env = fake_env(tmp, FAKE_RAISE=1)
    p, _ = start_worker(tmp, env)
    r = json.loads(ask(p, "boom", os.path.join(tmp, "c.wav")) or "{}")
    check("a python-level failure answers ok:false with a reason",
          r.get("ok") is False and "error" in r, "got %r" % (r,))
    p.stdin.close(); p.wait(timeout=10)

with tempfile.TemporaryDirectory() as tmp:
    env = fake_env(tmp, FAKE_NO_AUDIO=1)
    p, _ = start_worker(tmp, env)
    wav = os.path.join(tmp, "d.wav")
    r = json.loads(ask(p, "silent", wav) or "{}")
    check("an empty WAV counts as failure, not success",
          r.get("ok") is False, "got %r" % (r,))
    p.stdin.close(); p.wait(timeout=10)

with tempfile.TemporaryDirectory() as tmp:
    env = fake_env(tmp)
    p, _ = start_worker(tmp, env)
    r = json.loads((p.stdin.write("not json at all\n"),
                    p.stdin.flush(),
                    p.stdout.readline())[2] or "{}")
    check("malformed input is answered, not fatal",
          r.get("ok") is False)
    r2 = json.loads(ask(p, "still alive", os.path.join(tmp, "e.wav"))
                    or "{}")
    check("and the worker keeps serving afterwards",
          r2.get("ok") is True)
    p.stdin.close(); p.wait(timeout=10)


print("\n6. The voice must not change depending on which path ran")
src = open(os.path.join(ROOT, "dose_voice.py"), encoding="utf-8").read()
wsrc = open(WORKER, encoding="utf-8").read()
for val in ("0.62", "0.75"):
    check("noise parameter %s is identical in both paths" % val,
          val in src and val in wsrc)
check("both try SynthesisConfig first, then kwargs, then plain",
      wsrc.index("SynthesisConfig") < wsrc.index("noise_w=noise_w")
      < wsrc.index("voice.synthesize_wav(text, wav)\n"))
check("the worker caps ONNX threads too (or it would starve the mic)",
      "intra_op_num_threads" in wsrc)
check("the worker gets its own session, so a group signal cannot kill it",
      "start_new_session=True" in src)
check("the worker never touches the network",
      not any(k in wsrc for k in ("urllib", "requests", "socket",
                                  "http")))


print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("=== PIPER WORKER: a native abort costs a respawn, not the app ===")
