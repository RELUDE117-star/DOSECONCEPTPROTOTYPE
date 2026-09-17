"""Cloud-primary STT + the diagnostic tool.

Proves the architecture the owner asked for: the Pi hands transcription
to a FREE cloud service when online and keeps the local model only as an
offline fallback — and that a cloud failure never crashes the assistant.
Also checks the voice_diagnostics measurements and its fault verdict,
which is the whole point of the tool: prove WHERE recognition breaks.

No network and no models are needed here — providers are mocked, audio is
synthetic. The live paths are exercised on the device.
"""
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

PASSED = FAILED = 0
FAILS = []


def ok(cond, label):
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
        FAILS.append(label)
        print("  FAIL", label)


print("== 1. free-only: no paid providers, ever ==")
import dose_cloud_stt as cloud                                # noqa: E402
src = open(os.path.join(ROOT, "dose_cloud_stt.py")).read()
for banned in ("api.openai.com", "elevenlabs",
               "inference-endpoint", "paid request", "billing",
               "purchase capacity"):
    ok(banned not in src.lower(),
       "no reference to a paid service (%r)" % banned)
ok("api.groq.com" in src, "Groq free endpoint is present")
ok("gradio_client" in src, "HF free ZeroGPU path via gradio_client")
ok("rate_limited" in src,
   "a rate-limit is caught and flagged, not paid around")

print("== 2. LIVE path needs a credential; anon HF is diagnostic-only ==")
for k in ("GROQ_API_KEY", "HF_TOKEN", "HUGGINGFACE_TOKEN",
          "HUGGING_FACE_HUB_TOKEN", "GROQ_MODEL"):
    os.environ.pop(k, None)
# The LIVE assistant only goes to the cloud with a real credential. An
# anonymous HF ZeroGPU call goes through gradio_client, whose cold-start
# can stall for many seconds — putting that in a live turn made the mic
# look deaf. So with no key, the live path offers NOTHING and the Pi
# stays on the fast local model.
live = cloud.available_providers()             # anonymous_ok=False
ok(live == [],
   "no credential -> live cloud offers nothing (Pi stays local, mic "
   "keeps working): %r" % live)
# The DIAGNOSTIC path may use anonymous HF, because it runs offline.
diag = cloud.available_providers(anonymous_ok=True)
ok(("hf" in diag) == cloud._gradio_client_available(),
   "diagnostic path allows anonymous HF when gradio_client is present")
ok(cloud.hf_is_authenticated() is False,
   "with no token, HF (diagnostic) is on the shared anonymous quota")
w, allr = cloud.cloud_transcribe("/does/not/exist.wav", order=[])
ok(not w.ok and "no cloud provider" in w.error,
   "an EMPTY order returns a clean error, never a crash")

print("== 3. Groq: free-tier behaviour, graceful failure ==")
r = cloud.transcribe_groq("/tmp/none.wav", api_key="")
ok(not r.ok and "no GROQ_API_KEY" in r.error,
   "no key -> reported, not raised")
# simulate a 429 by monkeypatching requests.post
import types                                                  # noqa: E402


class _Resp:
    def __init__(self, code, payload=None, text=""):
        self.status_code = code
        self._p = payload or {}
        self.text = text

    def json(self):
        return self._p


def _run_with_fake_post(code, payload=None, text=""):
    import requests
    real = requests.post

    # a tiny wav so open() succeeds
    import wave
    p = "/tmp/_cloudtest.wav"
    with wave.open(p, "wb") as wv:
        wv.setnchannels(1)
        wv.setsampwidth(2)
        wv.setframerate(16000)
        wv.writeframes(b"\x00\x00" * 1600)
    requests.post = lambda *a, **k: _Resp(code, payload, text)
    try:
        return cloud.transcribe_groq(p, api_key="test-key")
    finally:
        requests.post = real


r429 = _run_with_fake_post(429, text="rate limit")
ok(r429.rate_limited and not r429.ok,
   "a 429 is flagged rate-limited and fails over (no purchase)")
rok = _run_with_fake_post(200, payload={"text": "open storage"})
ok(rok.ok and rok.text == "open storage", "a 200 returns the transcript")
r401 = _run_with_fake_post(401, text="bad key")
ok(not r401.ok and "unauthorized" in r401.error.lower(),
   "a 401 is a clean auth error, not a crash")

print("== 4. fallback order: groq -> hf, all free ==")
order = cloud.provider_order()
ok(order and order[0] == "groq",
   "Groq is preferred (fast free STT): %s" % order)
# want_all runs every provider even after one wins (for the A/B test)
calls = []


def fake_groq(path, **k):
    calls.append("groq")
    return cloud.Result("groq", "hello", 0.3)


def fake_hf(path, **k):
    calls.append("hf")
    return cloud.Result("hf", "hello there", 0.9)


cloud.transcribe_groq, cloud.transcribe_hf_space = fake_groq, fake_hf
w, allr = cloud.cloud_transcribe("/x.wav", order=["groq", "hf"])
ok(w.engine == "groq" and calls == ["groq"],
   "first success wins and stops (quota-friendly)")
calls.clear()
w, allr = cloud.cloud_transcribe("/x.wav", order=["groq", "hf"],
                                 want_all=True)
ok(len(allr) == 2 and calls == ["groq", "hf"],
   "want_all runs both for the A/B comparison")

print("== 5. the assistant: cloud primary, local fallback, no crash ==")
import dose_voice as dv                                       # noqa: E402
vsrc = open(os.path.join(ROOT, "dose_voice.py")).read()
bt = vsrc.split("def _better_transcribe")[1].split("\n    def ")[0]
ok("_cloud_enabled" in bt and "_is_online" in bt,
   "cloud is tried first, gated on being enabled AND online")
ok(bt.index("_cloud_transcribe") < bt.index("_fast_transcribe"),
   "cloud runs BEFORE the local models")
ok("allow_cloud=False" in vsrc,
   "the speculative pass stays local to spare free quota")
ct = vsrc.split("def _cloud_transcribe")[1].split("\n    def ")[0]
ok("except Exception" in ct and "return \"\"" in ct,
   "a cloud failure returns empty, never raises into the turn")
ce = vsrc.split("def _cloud_enabled")[1].split("\n    def ")[0]
ok('"local"' in ce and "available_providers" in ce,
   "DOSE_STT_MODE=local disables cloud; otherwise gated on a real key")

# behavioural: with cloud disabled it must still transcribe locally
v = dv.DoseVoice.__new__(dv.DoseVoice)
v._med_names = lambda: []
for a, val in (("_raw_vosk", ""), ("_raw_fast", ""), ("_raw_slow", ""),
               ("_fw_conf", 0.0), ("_fast_choice", "whisper")):
    setattr(v, a, val)
v._cloud_enabled = lambda: False
v._fast_transcribe = lambda a: ("open storage", "whisper-tiny.en")
v._whisper_transcribe = lambda a: ""
v._trim_silence = lambda a, keep_ms=140: a
got = v._better_transcribe(b"x" * 4000, "")
ok(got == "open storage",
   "cloud OFF -> local path still answers (%r)" % got)

# behavioural: cloud ON returns the cloud transcript
v2 = dv.DoseVoice.__new__(dv.DoseVoice)
v2._med_names = lambda: []
for a, val in (("_raw_vosk", ""), ("_raw_fast", ""), ("_raw_slow", ""),
               ("_fw_conf", 0.0), ("_fast_choice", "whisper")):
    setattr(v2, a, val)
v2._cloud_enabled = lambda: True
v2._is_online = lambda ttl=30.0: True
v2._cloud_transcribe = lambda a: ("what time is it", "cloud:groq", 0.4)
v2._fast_transcribe = lambda a: ("WRONG local", "whisper-tiny.en")
v2._whisper_transcribe = lambda a: ""
v2._trim_silence = lambda a, keep_ms=140: a
got2 = v2._better_transcribe(b"x" * 4000, "")
ok(got2 == "what time is it" and v2._last_engine == "cloud:groq",
   "cloud ON + online -> cloud transcript wins (%r via %s)"
   % (got2, getattr(v2, "_last_engine", "?")))

# behavioural: cloud ON but cloud FAILS -> falls back to local, no crash
v3 = dv.DoseVoice.__new__(dv.DoseVoice)
v3._med_names = lambda: []
for a, val in (("_raw_vosk", ""), ("_raw_fast", ""), ("_raw_slow", ""),
               ("_fw_conf", 0.0), ("_fast_choice", "whisper")):
    setattr(v3, a, val)
v3._cloud_enabled = lambda: True
v3._is_online = lambda ttl=30.0: True
v3._cloud_transcribe = lambda a: ("", "cloud", 0.0)   # failed
v3._fast_transcribe = lambda a: ("open settings", "whisper-tiny.en")
v3._whisper_transcribe = lambda a: ""
v3._trim_silence = lambda a, keep_ms=140: a
got3 = v3._better_transcribe(b"x" * 4000, "")
ok(got3 == "open settings",
   "cloud FAILED -> local fallback answers, no crash (%r)" % got3)

# THE DEAFNESS REGRESSION: a cloud call that HANGS must not freeze the
# turn. _cloud_transcribe enforces a hard budget on a worker thread; the
# turn must return the LOCAL answer within roughly that budget.
import time as _t                                             # noqa: E402
v4 = dv.DoseVoice.__new__(dv.DoseVoice)
v4._med_names = lambda: []
for a, val in (("_raw_vosk", ""), ("_raw_fast", ""), ("_raw_slow", ""),
               ("_raw_cloud", ""), ("_fw_conf", 0.0),
               ("_fast_choice", "whisper")):
    setattr(v4, a, val)
v4.CLOUD_BUDGET_S = 0.5          # tiny budget for the test
v4._cloud_enabled = lambda: True
v4._is_online = lambda ttl=30.0: True
v4._fast_transcribe = lambda a: ("open storage", "whisper-tiny.en")
v4._whisper_transcribe = lambda a: ""
v4._trim_silence = lambda a, keep_ms=140: a
v4._write_wav = lambda a: "/tmp/_hang.wav"

# real _cloud_transcribe with a provider that sleeps far past the budget
import dose_cloud_stt as _cc                                  # noqa: E402
_real_ct = _cc.cloud_transcribe


def _hanging(*a, **k):
    _t.sleep(30)                 # would hang the turn if not bounded
    return _cc.Result("groq", "too late"), []


_cc.cloud_transcribe = _hanging
_cc.available_providers = lambda anonymous_ok=False: ["groq"]
t0 = _t.time()
got4 = v4._better_transcribe(b"x" * 4000, "")
elapsed = _t.time() - t0
_cc.cloud_transcribe = _real_ct
ok(got4 == "open storage",
   "a HANGING cloud call -> local answer still returned (%r)" % got4)
ok(elapsed < 2.0,
   "the turn was NOT frozen by the hung cloud call (%.2fs, budget 0.5s)"
   % elapsed)

ctsrc = vsrc.split("def _cloud_transcribe")[1].split("\n    def ")[0]
ok("join(self.CLOUD_BUDGET_S)" in ctsrc and "is_alive()" in ctsrc,
   "cloud runs on a worker joined with a hard budget — never blocks")

print("== 6. voice_diagnostics: measurements ==")
import voice_diagnostics as vd                                # noqa: E402
sr = 16000
sil = (np.random.default_rng(1).standard_normal(sr) * 30).astype(np.int16)
tt = np.arange(sr * 2) / sr
sp = ((np.sin(2 * np.pi * 220 * tt)) * 8000).astype(np.int16)
ok(vd.dbfs(vd.rms(sp)) > vd.dbfs(vd.rms(sil)),
   "speech reads louder than silence in dBFS")
ok(vd.snr_db(sp, sil) > 20, "clean synthetic speech scores healthy SNR")
loud = (np.ones(sr) * 32767).astype(np.int16)
ok(vd.clipping_pct(loud) > 99, "a full-scale signal reads as clipping")

print("== 7. VAD keeps the pre-roll and hangover (never chop a word) ==")
buf = np.concatenate([sil, sp, sil]).astype(np.int16)
seg = vd.segment_utterance(buf, sr, preroll_ms=500, hangover_ms=800)
ok(seg.found, "an utterance is found inside silence-speech-silence")
ok(abs(seg.preroll - 0.5) < 0.06,
   "≈500 ms of pre-roll is prepended (%.3fs)" % seg.preroll)
ok(abs(seg.postroll - 0.8) < 0.06,
   "≈800 ms of hangover is appended (%.3fs)" % seg.postroll)
ok(seg.start_ts > 0 and seg.stop_ts > seg.start_ts,
   "VAD start/stop timestamps are recorded")
# the saved clip must be LONGER than the bare speech (edges kept)
ok(len(seg.samples) > 2 * sr, "the segment keeps audio beyond the speech")

print("== 8. the verdict names the right stage ==")


def mic(**kw):
    base = dict(silence={"rms": 30, "dbfs_rms": -60},
                speech={"rms": 3000, "peak": 9000, "dbfs_rms": -20},
                clipping_pct=0.0, snr_db=30,
                dropout={"overflows": 0, "underflows": 0,
                         "timing_gaps": 0, "errors": []},
                vad={"found": True, "preroll_s": 0.5, "speech_s": 1.5},
                load_local_stt={"cpu_pct": 40, "swap_pct": 0},
                local_stt={"text": "open storage", "secs": 0.9})
    base.update(kw)
    return base


ok(vd._classify(mic(dropout={"overflows": 6, "underflows": 0,
                             "timing_gaps": 0, "errors": []}), [], True)
   .startswith("MICROPHONE/CAPTURE"), "overruns -> capture problem")
ok(vd._classify(mic(vad={"found": False}), [], True)
   .startswith("VAD"), "no utterance found -> VAD problem")
ok(vd._classify(mic(load_local_stt={"cpu_pct": 98, "swap_pct": 20},
                    local_stt={"text": "x", "secs": 4.0}), [], True)
   .startswith("LOCAL COMPUTE"), "saturated Pi -> compute bottleneck")
ok(vd._classify(
    mic(local_stt={"text": "open the storm", "secs": 0.9}),
    [{"engine": "local", "text": "open the storm"},
     {"engine": "groq", "text": "how many pills do i have left today"}],
    True).startswith("STT MODEL ACCURACY"),
   "clean audio, engines disagree -> model accuracy problem")
ok(vd._classify(mic(), [], False).startswith("NETWORK"),
   "offline -> network problem")
ok(vd._classify(mic(), [{"engine": "local", "text": "open storage"},
                        {"engine": "groq", "text": "open storage"}], True)
   .startswith("NO OBVIOUS"), "all clean -> no obvious problem")

print("== 9. A/B sends ONE clip to every engine (no re-recording) ==")
abt = vd.__dict__["ab_stt"]
absrc = open(os.path.join(ROOT, "voice_diagnostics.py")).read()
abfn = absrc.split("def ab_stt")[1].split("\ndef ")[0]
ok("want_all=True" in abfn,
   "the A/B test runs every provider on the same file")
ok("capture(" not in abfn and "sd." not in abfn,
   "the A/B test never records — it only reads the given wav")

print()
print("cloud/diagnostics suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== CLOUD-PRIMARY STT + DIAGNOSTICS: ALL PASSED (free, "
      "fallback-safe) ===")
