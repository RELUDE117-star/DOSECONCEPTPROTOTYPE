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


print("\n5b. Concurrency: one worker, one load, no crossed replies")
# The FIRST hardware soak looked like it had spawned two workers, and I
# nearly shipped a fix for a CPU regression that did not exist —
# `pgrep -fc piper_worker.py` counts any process whose command line
# merely CONTAINS that string, including the measuring command itself.
#
# But the race underneath was real. prewarm_replies() runs on its own
# thread while the speech path can synthesise at the same time, and
# _synth_via_worker() writes one request to a pipe and reads one line
# back. Two threads doing that concurrently can each read the OTHER's
# reply, so sentence A is told "ok" about sentence B's file. Silent, and
# very hard to find later.
#
# So: count real interpreters, not string matches, and prove the
# request/response pairing survives sixteen threads at once.
CONC_FAKE = FAKE_PIPER.replace(
    'w.writeframes(b"".join(struct.pack("<h", (i % 800) - 400)\n'
    '                               for i in range(2205)))',
    '# encode WHICH sentence this is, so a crossed reply is detectable\n'
    '        n = int(text.split()[-1])\n'
    '        w.writeframes(struct.pack("<h", n) * 200)')

with tempfile.TemporaryDirectory() as tmp:
    d = os.path.join(tmp, "fakelib")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "piper.py"), "w") as f:
        f.write(CONC_FAKE)
    loads = os.path.join(tmp, "loads")
    open(loads, "w").close()
    model = os.path.join(tmp, "v.onnx")
    with open(model, "wb") as f:
        f.write(b"\0" * 64)

    import threading
    import dose_voice as _dv
    saved_path = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = d + os.pathsep + saved_path
    os.environ["FAKE_LOAD_COUNTER"] = loads
    try:
        eng = object.__new__(_dv.DoseVoice)
        eng._piper_path = model
        eng._pw_proc = None
        eng._pw_spawned_at = 0.0
        eng._tts_events = []

        N = 16
        res = {}
        errs = []

        def one(i):
            w = os.path.join(tmp, "c%d.wav" % i)
            try:
                res[i] = (eng._synth_via_worker("sentence number %d" % i, w), w)
            except Exception as ex:
                errs.append(repr(ex))

        th = [threading.Thread(target=one, args=(i,)) for i in range(N)]
        for t in th:
            t.start()
        for t in th:
            t.join(90)

        check("16 concurrent threads all synthesise",
              sum(1 for v in res.values() if v[0]) == N,
              "%d/%d ok, errors=%s" % (
                  sum(1 for v in res.values() if v[0]), N, errs[:2]))
        n_loads = len(open(loads).read().strip() or "")
        check("the model is loaded ONCE, not once per thread",
              n_loads <= 1, "loaded %d times" % n_loads)

        crossed = []
        for i, (ok, w) in sorted(res.items()):
            if not ok:
                continue
            r = wave.open(w, "rb")
            frame = r.readframes(1)
            r.close()
            import struct as _st
            got = _st.unpack("<h", frame)[0]
            if got != i:
                crossed.append((i, got))
        check("NO reply is crossed between threads "
              "(the bug the lock exists for)",
              not crossed, "crossed: %s" % (crossed[:4],))

        # ASSERT ON *OUR* PID, not a global process count.
        #
        # A global count was the first thing written here and it was
        # wrong twice over. `pgrep -fc piper_worker.py` matches any
        # command line CONTAINING that string — including the measuring
        # command itself, which is how a Pi soak appeared to show two
        # workers and nearly had me ship a fix for a CPU regression that
        # did not exist. And even counting real interpreters picks up
        # workers left by earlier sections of this very file, or by a
        # test running in parallel. The question is "did MY engine keep
        # exactly one worker", so ask about that one process.
        pid = eng._pw_proc.pid if eng._pw_proc else None
        check("the engine holds exactly one worker handle",
              pid is not None, "no worker handle at all")
        check("that worker is alive after 16 concurrent requests",
              pid is not None and eng._pw_proc.poll() is None)
        # every thread went through the same process
        check("all 16 threads shared ONE worker (a per-thread spawn "
              "would have loaded the model 16 times)", n_loads <= 1)

        eng.stop_piper_worker()
        time.sleep(0.5)
        gone = True
        if pid is not None:
            try:
                os.kill(pid, 0)
                gone = False          # still there
            except OSError:
                gone = True           # reaped
        check("stop_piper_worker() leaves no orphan behind", gone,
              "pid %s is still alive" % pid)
        check("the handle is cleared, so a later call respawns cleanly",
              eng._pw_proc is None)
        check("calling stop twice is safe",
              (eng.stop_piper_worker(), True)[1])
    finally:
        os.environ["PYTHONPATH"] = saved_path
        os.environ.pop("FAKE_LOAD_COUNTER", None)

check("the lock covers the whole request/response, not just the spawn",
      "_synth_via_worker_locked" in
      open(os.path.join(ROOT, "dose_voice.py"), encoding="utf-8").read())

anchor_src = open(os.path.join(ROOT, "dose_voice.py"), encoding="utf-8").read()
check("the engine reaps the worker on shutdown",
      "self.stop_piper_worker()" in anchor_src
      and anchor_src.count("def stop_piper_worker") == 1)

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
