#!/usr/bin/env python3
"""Synthesise speech in a SEPARATE PROCESS, so a native abort is survivable.

WHY THIS EXISTS
---------------
ONNX Runtime aborts this application. Logged three times in one
afternoon:

    Fatal Python error: Aborted
      onnxruntime_inference_collection.py line 395 in run
      piper/voice.py phoneme_ids_to_audio / synthesize / synthesize_wav
      dose_voice.py _synth / render_to_cache      <- the TTS prewarm

SIGABRT comes from native code, so **nothing in Python can catch it**.
No try/except, anywhere, at any level, will save the process. A
medication dispenser that disappears mid-sentence because a speech
model tripped over itself is not acceptable, and the existing
mitigation — the supervisor restarts in about twenty seconds — is a
twenty-second silence in front of whoever is standing at the machine.

It was not caused by the ONNX thread cap (one abort predates it), and
Piper synthesises perfectly well in an isolated process. That last fact
is the whole design: the abort is survivable if the thing that aborts
is not the app.

WHY A PERSISTENT WORKER AND NOT A PROCESS PER SENTENCE
------------------------------------------------------
Because a process per sentence would be far worse than the bug. Loading
the Piper voice means building an ONNX session and reading the model
off an SD card — seconds on a Pi 4, every sentence. The crash happens
occasionally; the latency would happen always.

So the model is loaded ONCE here, and this process then sits waiting.
One line of JSON in, one line of JSON out, for as long as the app runs.
If it aborts, the parent notices the pipe close, respawns it, and pays
the model load once more — at which point the crash has cost a retry
instead of the application.

PROTOCOL — one JSON object per line, in and out.
  in:   {"text": "...", "wav": "/abs/path.wav",
         "length_scale": 1.0, "noise_scale": 0.62, "noise_w": 0.75}
  out:  {"ok": true, "wav": "/abs/path.wav"}
        {"ok": false, "error": "..."}

Deliberately NOT a general-purpose service: it takes a model path on
argv, writes only where it is told, speaks only this protocol, and has
no network use of any kind. It is a crash blast-shield, and the smaller
its surface the better it does that job.
"""
import json
import os
import sys


def _log(msg):
    """Diagnostics go to stderr; stdout is the protocol channel only."""
    try:
        sys.stderr.write("[piper_worker] %s\n" % msg)
        sys.stderr.flush()
    except Exception:
        pass


def _cap_threads(n):
    """Match the parent's ONNX thread cap.

    A stock onnxruntime wheel is not built with OpenMP, so
    OMP_NUM_THREADS is ignored and the only lever is
    SessionOptions.intra_op_num_threads. Without this the worker would
    take all four cores for synthesis and starve the microphone — the
    exact fault the parent already fixed for itself, which would return
    the moment synthesis moved out of process.
    """
    try:
        import onnxruntime as ort
    except Exception:
        return
    if getattr(ort, "_dose_thread_cap", None) == n:
        return
    orig = ort.InferenceSession

    def _capped(*args, **kwargs):
        try:
            so = kwargs.get("sess_options")
            if so is None:
                so = ort.SessionOptions()
                kwargs["sess_options"] = so
            if getattr(so, "intra_op_num_threads", 0) == 0:
                so.intra_op_num_threads = n
            if getattr(so, "inter_op_num_threads", 0) == 0:
                so.inter_op_num_threads = 1
        except Exception:
            pass          # never block synthesis over a tuning knob
        return orig(*args, **kwargs)

    ort.InferenceSession = _capped
    ort._dose_thread_cap = n


def _synth(voice, text, wav, length_scale, noise_scale, noise_w):
    """The same three-way Piper call the parent uses, in the same order.

    Kept identical on purpose: if the in-process fallback and this
    worker produced different-sounding speech, the station's voice would
    change depending on whether a crash had happened recently, which is
    worse than either voice on its own.
    """
    try:
        from piper import SynthesisConfig
        cfg = SynthesisConfig(length_scale=length_scale,
                              noise_scale=noise_scale,
                              noise_w_scale=noise_w)
        voice.synthesize_wav(text, wav, syn_config=cfg)
        return
    except Exception:
        pass
    try:
        voice.synthesize_wav(text, wav, length_scale=length_scale,
                             noise_scale=noise_scale, noise_w=noise_w)
        return
    except Exception:
        pass
    voice.synthesize_wav(text, wav)


def main(argv):
    if len(argv) < 2:
        _log("usage: piper_worker.py MODEL.onnx [threads]")
        return 2
    model = argv[1]
    threads = 2
    if len(argv) > 2:
        try:
            threads = max(1, int(argv[2]))
        except Exception:
            pass
    if not os.path.exists(model):
        _log("model not found: %s" % model)
        return 3

    _cap_threads(threads)
    try:
        from piper import PiperVoice
        voice = PiperVoice.load(model)
    except Exception as e:
        _log("load failed: %r" % (e,))
        return 4

    # READY is the parent's signal that the model is up and the next
    # request will not pay the load cost. Without it the parent cannot
    # tell "still loading" from "hung".
    sys.stdout.write(json.dumps({"ready": True}) + "\n")
    sys.stdout.flush()
    _log("ready (%s, %d thread(s))" % (os.path.basename(model), threads))

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            sys.stdout.write(json.dumps(
                {"ok": False, "error": "bad json: %r" % (e,)}) + "\n")
            sys.stdout.flush()
            continue
        wav = req.get("wav") or ""
        text = req.get("text") or ""
        if not wav or not text:
            sys.stdout.write(json.dumps(
                {"ok": False, "error": "text and wav are both required"})
                + "\n")
            sys.stdout.flush()
            continue
        try:
            os.makedirs(os.path.dirname(wav) or ".", exist_ok=True)
            _synth(voice, text, wav,
                   float(req.get("length_scale", 1.0)),
                   float(req.get("noise_scale", 0.62)),
                   float(req.get("noise_w", 0.75)))
            ok = os.path.exists(wav) and os.path.getsize(wav) > 44
            out = {"ok": bool(ok), "wav": wav}
            if not ok:
                out["error"] = "no audio produced"
        except Exception as e:
            # A Python-level failure is reportable. A native abort is
            # not: this process simply dies, the parent's pipe closes,
            # and that is the entire point of the worker existing.
            out = {"ok": False, "error": repr(e)[:200]}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
