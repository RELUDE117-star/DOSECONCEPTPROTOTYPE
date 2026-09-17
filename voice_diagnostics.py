#!/usr/bin/env python3
"""voice_diagnostics.py — prove WHERE speech recognition is failing.

The DOSE Home Station kept mishearing, and every fix was a guess because
we could not see which stage was at fault. This tool removes the guessing.
It exercises the real microphone, saves the UNTOUCHED recording next to
the exact clip sent to the recogniser, runs that one clip through the
local model AND free cloud models, and prints a single report that ends
with a verdict:

    MICROPHONE/CAPTURE PROBLEM
    VAD PROBLEM
    LOCAL COMPUTE BOTTLENECK
    STT MODEL ACCURACY PROBLEM
    NETWORK PROBLEM
    NO OBVIOUS PROBLEM

Design rule (the scientific one the user insisted on): change ONE thing
at a time. Every stage writes its own artifact — raw_test.wav,
stt_input.wav, per-repeat WAVs and transcripts — so a claim can always be
checked against the audio itself. If the raw audio is missing the word,
it is a CAPTURE/VAD fault, not the model's.

Everything here is free/open-source or a free API tier. No paid calls.

Usage:
    python3 voice_diagnostics.py devices        # list input devices
    python3 voice_diagnostics.py mic            # guided mic tests A–F
    python3 voice_diagnostics.py firstlast       # repeat a line 10x
    python3 voice_diagnostics.py ab  [file.wav]  # A/B local vs cloud STT
    python3 voice_diagnostics.py full            # everything + verdict
    python3 voice_diagnostics.py raw  [secs]     # just save raw_test.wav

Options: --device N  --outdir DIR  --hangover-ms 800  --preroll-ms 500
"""

import argparse
import os
import sys
import time
import wave

try:
    import numpy as np
except Exception:
    print("numpy is required: pip install numpy")
    sys.exit(1)

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2                       # 16-bit signed PCM
FULL_SCALE = 32768.0
OUTDIR = os.path.expanduser(
    os.environ.get("DOSE_DIAG_DIR", "~/dose-home-station/diagnostics"))


# ══════════════════════════════════════════════════════════════════════
#  small helpers
# ══════════════════════════════════════════════════════════════════════
def _ensure_outdir(path=None):
    d = path or OUTDIR
    os.makedirs(d, exist_ok=True)
    return d


def _pcm16(frames_bytes):
    return np.frombuffer(frames_bytes, dtype=np.int16)


def rms(samples):
    if len(samples) == 0:
        return 0.0
    x = samples.astype(np.float64)
    return float(np.sqrt(np.mean(x * x)))


def peak(samples):
    return int(np.max(np.abs(samples.astype(np.int32)))) if len(samples) \
        else 0


def dbfs(value):
    """dBFS of an amplitude value (RMS or peak). -inf guarded."""
    if value <= 0:
        return -120.0
    return round(20.0 * np.log10(value / FULL_SCALE), 1)


def clipping_pct(samples, thresh=0.99):
    if len(samples) == 0:
        return 0.0
    hot = np.abs(samples.astype(np.int32)) >= int(FULL_SCALE * thresh)
    return round(100.0 * float(np.count_nonzero(hot)) / len(samples), 3)


def snr_db(voice, noise):
    """Approximate SNR: voice RMS over noise-floor RMS, in dB."""
    nr = rms(noise)
    vr = rms(voice)
    if nr <= 0:
        return 99.0 if vr > 0 else 0.0
    return round(20.0 * np.log10(max(vr, 1.0) / nr), 1)


def write_wav(path, samples, sr=SAMPLE_RATE):
    if samples.dtype != np.int16:
        samples = samples.astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(sr)
        w.writeframes(samples.tobytes())
    return path


def read_wav(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        data = w.readframes(w.getnframes())
    return _pcm16(data), sr


# ══════════════════════════════════════════════════════════════════════
#  1) devices
# ══════════════════════════════════════════════════════════════════════
def _sd():
    import sounddevice as sd
    return sd


def list_devices(selected=None):
    """Print every input device; mark the one the assistant will use."""
    print("== RECORDING DEVICES ==")
    try:
        sd = _sd()
        devs = sd.query_devices()
    except Exception as e:
        print("  could not query devices:", e)
        return None
    default_in = None
    try:
        default_in = sd.default.device[0]
    except Exception:
        pass
    chosen = _pick_input_device(selected)
    for i, d in enumerate(devs):
        if d.get("max_input_channels", 0) <= 0:
            continue
        marks = []
        if i == default_in:
            marks.append("system-default")
        if i == chosen:
            marks.append(">> ASSISTANT USES THIS <<")
        print("  [%2d] %-40s in=%d  %d Hz  %s"
              % (i, d.get("name", "?")[:40],
                 d.get("max_input_channels", 0),
                 int(d.get("default_samplerate", 0) or 0),
                 " ".join(marks)))
    print("  Assistant capture device index:",
          chosen if chosen is not None else "system default")
    return chosen


def _pick_input_device(selected=None):
    """The device the assistant actually uses. Honour --device / env,
    else prefer a USB mic (the AIRHUG), else the system default."""
    if selected is not None:
        return selected
    env = os.environ.get("DOSE_INPUT_DEVICE")
    if env not in (None, ""):
        try:
            return int(env)
        except Exception:
            pass
    try:
        sd = _sd()
        devs = sd.query_devices()
        for i, d in enumerate(devs):
            n = (d.get("name", "") or "").lower()
            if d.get("max_input_channels", 0) > 0 and (
                    "usb" in n or "airhug" in n or "a28" in n):
                return i
        return sd.default.device[0]
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════
#  2) capture (with error detection — never silent)
# ══════════════════════════════════════════════════════════════════════
class CaptureResult:
    def __init__(self):
        self.samples = np.zeros(0, dtype=np.int16)
        self.sr = SAMPLE_RATE
        self.channels = CHANNELS
        self.width = SAMPLE_WIDTH
        self.frames = 0
        self.duration = 0.0
        self.overflows = 0
        self.underflows = 0
        self.errors = []          # human-readable capture problems
        self.gaps = []            # (index, seconds) timing discontinuities

    def summary(self):
        return {
            "sample_rate": self.sr, "channels": self.channels,
            "sample_width": self.width, "frames": self.frames,
            "duration": round(self.duration, 3),
            "overflows": self.overflows, "underflows": self.underflows,
            "dropped_or_errors": len(self.errors),
            "timing_gaps": len(self.gaps),
        }


def capture(seconds, device=None, sr=SAMPLE_RATE):
    """Record `seconds` of 16 kHz mono 16-bit PCM, capturing EVERY
    error the audio stack reports — overruns, underruns, dropped
    frames, timing gaps. Nothing is swallowed."""
    res = CaptureResult()
    res.sr = sr
    try:
        sd = _sd()
    except Exception as e:
        res.errors.append("sounddevice unavailable: %s" % e)
        return res

    chunks = []
    blocksize = int(sr * 0.032)       # 32 ms blocks
    last_t = [None]

    def cb(indata, frames, time_info, status):
        # status is a CallbackFlags; ANY set bit is a real event
        if status:
            s = str(status)
            if getattr(status, "input_overflow", False):
                res.overflows += 1
            if getattr(status, "input_underflow", False):
                res.underflows += 1
            res.errors.append("callback status: %s" % s)
        # detect timing discontinuities from the stream's own clock
        try:
            adc = time_info.inputBufferAdcTime
            if last_t[0] is not None and adc:
                gap = adc - last_t[0]
                expected = frames / float(sr)
                if gap > expected * 1.5 and gap - expected > 0.010:
                    res.gaps.append((len(chunks), round(gap, 4)))
            if adc:
                last_t[0] = adc
        except Exception:
            pass
        chunks.append(bytes(indata))

    try:
        with sd.RawInputStream(samplerate=sr, channels=CHANNELS,
                               dtype="int16", blocksize=blocksize,
                               device=device, callback=cb):
            t0 = time.time()
            while time.time() - t0 < seconds:
                time.sleep(0.02)
    except Exception as e:
        res.errors.append("stream error: %s: %s"
                          % (type(e).__name__, str(e)[:120]))
        return res

    raw = b"".join(chunks)
    res.samples = _pcm16(raw)
    res.frames = len(res.samples)
    res.duration = res.frames / float(sr)
    # a duration well short of what we asked for means dropped audio
    if res.duration < seconds * 0.9:
        res.errors.append(
            "captured only %.2fs of %.2fs requested — dropped audio"
            % (res.duration, seconds))
    return res


def capture_raw(seconds=5.0, device=None, outdir=None):
    """TEST: save the UNTOUCHED microphone recording as raw_test.wav
    before any VAD/denoise/STT/resample, and report the real capture
    parameters."""
    d = _ensure_outdir(outdir)
    print("== RAW CAPTURE (%.1fs) ==" % seconds)
    print("  Recording... speak or stay silent as the test requires.")
    res = capture(seconds, device=device)
    path = os.path.join(d, "raw_test.wav")
    write_wav(path, res.samples, res.sr)
    print("  saved untouched recording ->", path)
    s = res.summary()
    print("  sample rate     %d Hz" % s["sample_rate"])
    print("  channels        %d" % s["channels"])
    print("  sample width    %d bytes (16-bit)" % s["sample_width"])
    print("  frames          %d" % s["frames"])
    print("  duration        %.3f s" % s["duration"])
    print("  overruns        %d" % s["overflows"])
    print("  underruns       %d" % s["underflows"])
    print("  timing gaps     %d" % s["timing_gaps"])
    if res.errors:
        print("  !! CAPTURE ERRORS (not ignored):")
        for e in res.errors[:8]:
            print("     -", e)
    else:
        print("  no capture errors reported")
    return res, path


# ══════════════════════════════════════════════════════════════════════
#  3) VAD segmentation — pre-roll + hangover, never chop a word
# ══════════════════════════════════════════════════════════════════════
class Segment:
    def __init__(self):
        self.samples = np.zeros(0, dtype=np.int16)
        self.start_ts = 0.0
        self.stop_ts = 0.0
        self.speech_dur = 0.0
        self.preroll = 0.0
        self.postroll = 0.0
        self.found = False


def segment_utterance(samples, sr=SAMPLE_RATE, preroll_ms=500,
                      hangover_ms=800, frame_ms=20, floor_mult=3.0):
    """Find the utterance inside a recording and return it WITH a
    pre-roll prepended and a hangover appended.

    The two classic ways VAD eats words: it starts the clip after the
    first word has begun, and it ends the clip the instant it hears a
    gap between words. The pre-roll fixes the first, the hangover fixes
    the second. Both are configurable and both are reported."""
    seg = Segment()
    if len(samples) == 0:
        return seg
    fl = int(sr * frame_ms / 1000.0)
    n = len(samples) // fl
    if n < 2:
        return seg
    frames = samples[:n * fl].reshape(n, fl).astype(np.float64)
    energy = np.sqrt(np.mean(frames * frames, axis=1))
    # noise floor from the quietest 20% of frames
    floor = np.percentile(energy, 20)
    gate = max(floor * floor_mult, 120.0)
    voiced = energy > gate
    if not np.any(voiced):
        return seg
    idx = np.where(voiced)[0]
    first, last = int(idx[0]), int(idx[-1])
    pre_frames = int(preroll_ms / frame_ms)
    hang_frames = int(hangover_ms / frame_ms)
    lo = max(0, first - pre_frames)
    hi = min(n, last + 1 + hang_frames)
    seg.samples = samples[lo * fl: hi * fl].copy()
    seg.start_ts = first * frame_ms / 1000.0
    seg.stop_ts = (last + 1) * frame_ms / 1000.0
    seg.speech_dur = seg.stop_ts - seg.start_ts
    seg.preroll = (first - lo) * frame_ms / 1000.0
    seg.postroll = (hi - (last + 1)) * frame_ms / 1000.0
    seg.found = True
    return seg


def make_stt_input(raw_samples, sr=SAMPLE_RATE, outdir=None,
                   preroll_ms=500, hangover_ms=800):
    """Produce stt_input.wav from a raw recording, so the exact clip the
    recogniser sees can be compared against raw_test.wav."""
    d = _ensure_outdir(outdir)
    seg = segment_utterance(raw_samples, sr, preroll_ms, hangover_ms)
    path = os.path.join(d, "stt_input.wav")
    write_wav(path, seg.samples if seg.found else raw_samples, sr)
    return seg, path


# ══════════════════════════════════════════════════════════════════════
#  4) system load  (idle / recording / local STT)
# ══════════════════════════════════════════════════════════════════════
def _cpu_sampler():
    """Return a function that yields CPU utilisation % since last call,
    using psutil if present, else /proc/stat."""
    try:
        import psutil
        psutil.cpu_percent(None)
        return lambda: psutil.cpu_percent(None)
    except Exception:
        pass

    def read():
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]
        vals = list(map(int, parts))
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return sum(vals), idle
    state = {"last": read()}

    def sample():
        tot0, idle0 = state["last"]
        tot1, idle1 = read()
        state["last"] = (tot1, idle1)
        dt, di = tot1 - tot0, idle1 - idle0
        if dt <= 0:
            return 0.0
        return round(100.0 * (dt - di) / dt, 1)
    return sample


def _mem_usage():
    try:
        import psutil
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        return {"ram_pct": vm.percent, "ram_free_mb": vm.available // (1 << 20),
                "swap_pct": sw.percent}
    except Exception:
        pass
    info = {}
    try:
        for line in open("/proc/meminfo"):
            k, v = line.split(":")
            info[k.strip()] = int(v.strip().split()[0])   # kB
        total = info.get("MemTotal", 1)
        avail = info.get("MemAvailable", 0)
        stotal = info.get("SwapTotal", 0)
        sfree = info.get("SwapFree", 0)
        return {
            "ram_pct": round(100.0 * (total - avail) / max(total, 1), 1),
            "ram_free_mb": avail // 1024,
            "swap_pct": round(100.0 * (stotal - sfree) / max(stotal, 1), 1)
            if stotal else 0.0}
    except Exception:
        return {"ram_pct": 0.0, "ram_free_mb": 0, "swap_pct": 0.0}


def _cpu_temp():
    try:
        import dose_voice
        return dose_voice.pi_health().get("temp_c", 0.0)
    except Exception:
        pass
    for p in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            return round(int(open(p).read().strip()) / 1000.0, 1)
        except Exception:
            pass
    return 0.0


def load_snapshot(label, cpu_sample):
    m = _mem_usage()
    snap = {"label": label, "cpu_pct": cpu_sample(),
            "ram_pct": m["ram_pct"], "ram_free_mb": m["ram_free_mb"],
            "swap_pct": m["swap_pct"], "temp_c": _cpu_temp()}
    return snap


# ══════════════════════════════════════════════════════════════════════
#  5) STT engines — local + free cloud, on the SAME clip
# ══════════════════════════════════════════════════════════════════════
def transcribe_local(wav_path):
    """The existing on-device recogniser, so the A/B test measures what
    actually ships. Returns (text, secs, error)."""
    t0 = time.time()
    try:
        import dose_voice
        samples, sr = read_wav(wav_path)
        v = dose_voice.DoseVoice.__new__(dose_voice.DoseVoice)
        v._med_names = lambda: []
        # Bypassing __init__, so give the local pipeline every attribute
        # it reads directly. Force LOCAL ONLY (allow_cloud=False): this
        # function exists to measure the on-device model, not the cloud.
        for attr, val in (("_raw_vosk", ""), ("_raw_fast", ""),
                          ("_raw_slow", ""), ("_raw_cloud", ""),
                          ("_fw_conf", 0.0), ("_fast_choice", "whisper"),
                          ("_whisper", None), ("_whisper_loaded", False),
                          ("_whisper_size", (dose_voice.WHISPER_MODELS[0]
                                             if dose_voice.WHISPER_MODELS
                                             else "")),
                          ("_whisper_fast", None),
                          ("_whisper_fast_loaded", False),
                          ("_whisper_fast_size",
                           dose_voice.FAST_WHISPER_MODEL),
                          ("_moonshine", None), ("_ms_v2", "unset")):
            setattr(v, attr, val)
        text = v._better_transcribe(samples.tobytes(), "",
                                    allow_cloud=False)
        return (text or "", time.time() - t0, "")
    except Exception as e:
        return ("", time.time() - t0,
                "%s: %s" % (type(e).__name__, str(e)[:120]))


def ab_stt(wav_path, language="en"):
    """Send the SAME wav to every engine and collect results. No engine
    gets its own recording — that is the whole point."""
    import dose_cloud_stt as cloud
    rows = []
    tl, sl, el = transcribe_local(wav_path)
    rows.append({"engine": "local", "text": tl, "secs": round(sl, 3),
                 "error": el})
    online = cloud.is_online()
    if not online:
        rows.append({"engine": "cloud", "text": "", "secs": 0.0,
                     "error": "offline — cloud engines skipped"})
        return rows, online
    # run ALL providers (want_all), so we can diff them. The diagnostic
    # runs offline, so anonymous HF is allowed here even though the live
    # assistant requires a credential for it.
    _, results = cloud.cloud_transcribe(
        wav_path, order=cloud.available_providers(anonymous_ok=True),
        language=language, want_all=True)
    for r in results:
        rows.append({"engine": r.engine, "text": r.text,
                     "secs": r.secs, "error": r.error})
    if len(rows) == 1:
        rows.append({"engine": "cloud", "text": "", "secs": 0.0,
                     "error": "no cloud provider configured "
                     "(set GROQ_API_KEY or hf_token)"})
    return rows, online


def diff_report(rows):
    """Word-level difference between engines, so differing words jump
    out immediately."""
    import difflib
    out = ["  --- transcript differences ---"]
    texts = [(r["engine"], r["text"]) for r in rows if r.get("text")]
    if len(texts) < 2:
        out.append("  (need at least two transcripts to compare)")
        return "\n".join(out)
    base_eng, base = texts[0]
    for eng, txt in texts[1:]:
        out.append("  %s  vs  %s:" % (base_eng, eng))
        a, b = base.split(), txt.split()
        sm = difflib.SequenceMatcher(a=a, b=b)
        if sm.ratio() == 1.0:
            out.append("     identical")
            continue
        out.append("     similarity %.0f%%" % (100 * sm.ratio()))
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            out.append("     %-8s %r -> %r"
                       % (tag, " ".join(a[i1:i2]), " ".join(b[j1:j2])))
    return "\n".join(out)


# ══════════════════════════════════════════════════════════════════════
#  guided tests
# ══════════════════════════════════════════════════════════════════════
def _countdown(msg, secs):
    print("  %s" % msg)
    for i in range(int(secs), 0, -1):
        sys.stdout.write("\r  starting in %d... " % i)
        sys.stdout.flush()
        time.sleep(1)
    sys.stdout.write("\r  RECORDING NOW           \n")
    sys.stdout.flush()


def mic_test(device=None, outdir=None):
    """TESTS A–F: silence, speech, clipping, SNR, drop-outs, load."""
    d = _ensure_outdir(outdir)
    report = {}
    cpu = _cpu_sampler()

    # F(idle) first, before we stress anything
    time.sleep(0.3)
    report["load_idle"] = load_snapshot("idle", cpu)

    # A — SILENCE
    print("\n== TEST A: SILENCE (5s) — please be quiet ==")
    _countdown("Stay silent.", 3)
    sil = capture(5.0, device=device)
    write_wav(os.path.join(d, "test_silence.wav"), sil.samples, sil.sr)
    report["silence"] = {
        "rms": round(rms(sil.samples), 1), "peak": peak(sil.samples),
        "dbfs_rms": dbfs(rms(sil.samples)),
        "dbfs_peak": dbfs(peak(sil.samples)),
        "errors": sil.errors, "overflows": sil.overflows,
        "underflows": sil.underflows, "gaps": len(sil.gaps)}

    # B — NORMAL SPEECH  (also sample load WHILE recording)
    print("\n== TEST B: NORMAL SPEECH (10s) ==")
    print("  Speak normally, from where the device will actually sit.")
    _countdown("Get ready to talk.", 3)
    sp = capture(10.0, device=device)
    report["load_recording"] = load_snapshot("recording", cpu)
    write_wav(os.path.join(d, "test_speech.wav"), sp.samples, sp.sr)
    report["speech"] = {
        "rms": round(rms(sp.samples), 1), "peak": peak(sp.samples),
        "dbfs_rms": dbfs(rms(sp.samples)),
        "dbfs_peak": dbfs(peak(sp.samples)),
        "errors": sp.errors, "overflows": sp.overflows,
        "underflows": sp.underflows, "gaps": len(sp.gaps)}

    # C — CLIPPING
    clip = clipping_pct(sp.samples)
    report["clipping_pct"] = clip

    # D — SNR
    report["snr_db"] = snr_db(sp.samples, sil.samples)

    # E — DROP-OUT (aggregate of both captures)
    report["dropout"] = {
        "overflows": sil.overflows + sp.overflows,
        "underflows": sil.underflows + sp.underflows,
        "timing_gaps": len(sil.gaps) + len(sp.gaps),
        "errors": sil.errors + sp.errors}

    # F — LOAD while local STT runs (proves whether local STT saturates)
    print("\n== TEST F: LOCAL STT LOAD ==")
    seg, stt_path = make_stt_input(sp.samples, sp.sr, outdir=d)
    report["vad"] = {"found": seg.found, "preroll_s": round(seg.preroll, 3),
                     "postroll_s": round(seg.postroll, 3),
                     "speech_s": round(seg.speech_dur, 3),
                     "start_ts": round(seg.start_ts, 3),
                     "stop_ts": round(seg.stop_ts, 3)}
    print("  running the on-device recogniser on stt_input.wav ...")
    cpu()  # reset window
    tl, sl, el = transcribe_local(stt_path)
    report["load_local_stt"] = load_snapshot("local STT", cpu)
    report["local_stt"] = {"text": tl, "secs": round(sl, 3), "error": el}
    _print_mic_report(report)
    return report


def _print_mic_report(r):
    print("\n== MIC TEST SUMMARY ==")
    sil, sp = r.get("silence", {}), r.get("speech", {})
    print("  A silence   RMS %-7s peak %-6s  %s dBFS"
          % (sil.get("rms"), sil.get("peak"), sil.get("dbfs_rms")))
    print("  B speech    RMS %-7s peak %-6s  %s dBFS"
          % (sp.get("rms"), sp.get("peak"), sp.get("dbfs_rms")))
    clip = r.get("clipping_pct", 0)
    print("  C clipping  %.3f%% of samples near full-scale%s"
          % (clip, "   <-- REPEATED CLIPPING" if clip > 1.0 else ""))
    snr = r.get("snr_db", 0)
    verdict = ("healthy" if snr > 20 else
               "warning" if snr >= 12 else "POOR for STT")
    print("  D SNR       %.1f dB  (%s)" % (snr, verdict))
    dz = r.get("dropout", {})
    print("  E drop-out  overruns %d underruns %d gaps %d errors %d"
          % (dz.get("overflows", 0), dz.get("underflows", 0),
             dz.get("timing_gaps", 0), len(dz.get("errors", []))))
    for lab in ("load_idle", "load_recording", "load_local_stt"):
        s = r.get(lab)
        if s:
            print("  F %-13s CPU %5s%%  RAM %5s%%  swap %4s%%  %sC"
                  % (s["label"], s["cpu_pct"], s["ram_pct"],
                     s["swap_pct"], s["temp_c"]))
    v = r.get("vad", {})
    if v:
        print("  VAD  pre-roll %.0fms  post-roll %.0fms  speech %.2fs"
              % (v.get("preroll_s", 0) * 1000, v.get("postroll_s", 0) * 1000,
                 v.get("speech_s", 0)))
    ls = r.get("local_stt", {})
    if ls:
        print("  local STT -> %r  (%.2fs)%s"
              % (ls.get("text"), ls.get("secs", 0),
                 "  ERROR: " + ls["error"] if ls.get("error") else ""))


def first_last_word_test(n=10, device=None, outdir=None,
                         phrase="what medication do I take today"):
    """Repeat one sentence n times. For each, keep the WAV and transcript
    and decide whether a miss is missing audio at the START, the END, or
    NEITHER — which separates CAPTURE/VAD faults from STT faults."""
    d = _ensure_outdir(os.path.join(outdir or OUTDIR, "firstlast"))
    print("== FIRST/LAST WORD TEST ==")
    print("  Say this each time, the SAME way: %r" % phrase)
    words = phrase.lower().split()
    first_w, last_w = words[0], words[-1]
    rows = []
    for i in range(n):
        _countdown("Repeat %d/%d." % (i + 1, n), 2)
        cap = capture(4.0, device=device)
        raw_p = os.path.join(d, "raw_%02d.wav" % i)
        write_wav(raw_p, cap.samples, cap.sr)
        seg, _ = make_stt_input(cap.samples, cap.sr, outdir=d)
        seg_p = os.path.join(d, "stt_%02d.wav" % i)
        write_wav(seg_p, seg.samples if seg.found else cap.samples, cap.sr)
        tl, sl, el = transcribe_local(seg_p)
        with open(os.path.join(d, "stt_%02d.txt" % i), "w") as f:
            f.write(tl)
        heard = tl.lower().split()
        got_first = bool(heard) and _fuzzy_in(first_w, heard[:2])
        got_last = bool(heard) and _fuzzy_in(last_w, heard[-2:])
        # was the RAW audio even long enough to contain the edges?
        raw_has_head = _has_energy_head(cap.samples, cap.sr)
        raw_has_tail = _has_energy_tail(cap.samples, cap.sr)
        fault = _first_last_classify(got_first, got_last,
                                     raw_has_head, raw_has_tail)
        rows.append({"i": i, "text": tl, "got_first": got_first,
                     "got_last": got_last, "raw_head": raw_has_head,
                     "raw_tail": raw_has_tail, "fault": fault,
                     "secs": round(sl, 3), "error": el})
        print("   %2d: %r  first=%s last=%s -> %s"
              % (i, tl, got_first, got_last, fault))
    _summarize_first_last(rows)
    return rows


def _fuzzy_in(word, candidates):
    try:
        from rapidfuzz import fuzz
        return any(fuzz.ratio(word, c) >= 75 for c in candidates)
    except Exception:
        return any(word == c or word in c or c in word for c in candidates)


def _has_energy_head(samples, sr, ms=300):
    head = samples[:int(sr * ms / 1000)]
    return rms(head) > 150.0


def _has_energy_tail(samples, sr, ms=300):
    tail = samples[-int(sr * ms / 1000):]
    return rms(tail) > 150.0


def _first_last_classify(got_first, got_last, raw_head, raw_tail):
    if got_first and got_last:
        return "OK"
    # a word is missing from the transcript. Is it missing from the AUDIO?
    if not got_first and raw_head:
        return "STT dropped the FIRST word (audio was present)"
    if not got_last and raw_tail:
        return "STT dropped the LAST word (audio was present)"
    if not got_first and not raw_head:
        return "CAPTURE/VAD: FIRST word missing from the audio itself"
    if not got_last and not raw_tail:
        return "CAPTURE/VAD: LAST word missing from the audio itself"
    return "mismatch (neither edge)"


def _summarize_first_last(rows):
    n = len(rows)
    okc = sum(1 for r in rows if r["fault"] == "OK")
    capvad = sum(1 for r in rows if r["fault"].startswith("CAPTURE"))
    stt = sum(1 for r in rows if r["fault"].startswith("STT"))
    print("\n  --- first/last summary ---")
    print("  clean            %d/%d" % (okc, n))
    print("  CAPTURE/VAD miss %d  (fix the mic or the pre-roll/hangover)"
          % capvad)
    print("  STT miss         %d  (audio was fine, model dropped a word)"
          % stt)


# ══════════════════════════════════════════════════════════════════════
#  full report + verdict
# ══════════════════════════════════════════════════════════════════════
def full(device=None, outdir=None):
    d = _ensure_outdir(outdir)
    print("###########  DOSE VOICE DIAGNOSTICS  ###########\n")
    chosen = list_devices(device)
    mic = mic_test(device=device, outdir=d)
    # A/B on the very clip the mic test produced
    stt_path = os.path.join(d, "stt_input.wav")
    ab_rows, online = ([], False)
    if os.path.exists(stt_path):
        print("\n== A/B STT (same stt_input.wav to every engine) ==")
        ab_rows, online = ab_stt(stt_path)
        for r in ab_rows:
            print("  %-8s %6.2fs  %r%s"
                  % (r["engine"], r["secs"], r["text"],
                     "  ERROR: " + r["error"] if r["error"] else ""))
        print(diff_report(ab_rows))
    report_text = _compose_report(chosen, mic, ab_rows, online)
    print("\n" + report_text)
    path = os.path.join(d, "diagnostic_report.txt")
    with open(path, "w") as f:
        f.write(report_text)
    print("\n  full report saved ->", path)
    print("  artifacts in", d, "(raw_test.wav, stt_input.wav, ...)")
    return report_text


def _compose_report(chosen, mic, ab_rows, online):
    sil = mic.get("silence", {})
    sp = mic.get("speech", {})
    vad = mic.get("vad", {})
    li = mic.get("load_idle", {})
    lr = mic.get("load_recording", {})
    ll = mic.get("load_local_stt", {})
    ls = mic.get("local_stt", {})
    cloud_rows = [r for r in ab_rows if r["engine"] not in ("local",)]
    cloud_txt = next((r["text"] for r in cloud_rows if r["text"]), "")
    cloud_lat = next((r["secs"] for r in cloud_rows if r["text"]), 0.0)
    L = []
    L.append("================ DIAGNOSTIC REPORT ================")
    L.append("Microphone device:   %s" % (chosen if chosen is not None
                                           else "system default"))
    L.append("Sample rate:         %d Hz" % SAMPLE_RATE)
    L.append("Channels:            %d" % CHANNELS)
    L.append("Audio format:        signed 16-bit PCM")
    L.append("Noise floor:         %s dBFS (RMS %s)"
             % (sil.get("dbfs_rms"), sil.get("rms")))
    L.append("Voice RMS:           %s (%s dBFS)"
             % (sp.get("rms"), sp.get("dbfs_rms")))
    L.append("Voice peak:          %s" % sp.get("peak"))
    L.append("Estimated SNR:       %s dB" % mic.get("snr_db"))
    L.append("Clipped samples:     %s%%" % mic.get("clipping_pct"))
    dz = mic.get("dropout", {})
    L.append("Dropped frames:      overruns %d / underruns %d / gaps %d"
             % (dz.get("overflows", 0), dz.get("underflows", 0),
                dz.get("timing_gaps", 0)))
    L.append("VAD pre-roll:        %.0f ms" % (vad.get("preroll_s", 0) * 1000))
    L.append("VAD post-roll:       %.0f ms" % (vad.get("postroll_s", 0) * 1000))
    L.append("Recording CPU:       %s%%" % lr.get("cpu_pct"))
    L.append("Local STT CPU:       %s%%" % ll.get("cpu_pct"))
    L.append("Local STT RAM:       %s%% (%s MB free)"
             % (ll.get("ram_pct"), ll.get("ram_free_mb")))
    L.append("Idle temp / STT temp:%sC / %sC"
             % (li.get("temp_c"), ll.get("temp_c")))
    L.append("Cloud STT latency:   %s s%s"
             % (cloud_lat, "" if online else "  (offline)"))
    L.append("Local transcript:    %r" % ls.get("text"))
    L.append("Cloud transcript:    %r" % cloud_txt)
    L.append("")
    L.append("VERDICT: " + _classify(mic, ab_rows, online))
    L.append("==================================================")
    return "\n".join(L)


def _classify(mic, ab_rows, online):
    """One likely cause, chosen from the evidence. Deliberately checks
    the stages in the order a signal travels: capture -> VAD ->
    compute -> model -> network."""
    sil = mic.get("silence", {})
    sp = mic.get("speech", {})
    dz = mic.get("dropout", {})
    vad = mic.get("vad", {})
    ll = mic.get("load_local_stt", {})
    ls = mic.get("local_stt", {})
    snr = mic.get("snr_db", 0)
    clip = mic.get("clipping_pct", 0)

    # 1) capture — the signal never arrived cleanly
    if dz.get("overflows", 0) + dz.get("underflows", 0) > 2 \
            or dz.get("timing_gaps", 0) > 2 or dz.get("errors"):
        return ("MICROPHONE/CAPTURE PROBLEM — the audio stack reported "
                "overruns/underruns/gaps; fix capture before anything "
                "else.")
    if sp.get("rms", 0) < 200:
        return ("MICROPHONE/CAPTURE PROBLEM — almost no signal on the "
                "speech recording; wrong input device, unplugged, or "
                "muted.")
    if clip > 1.0:
        return ("MICROPHONE/CAPTURE PROBLEM — repeated clipping (%.2f%%); "
                "input level is too hot, a clipped waveform loses "
                "information." % clip)

    # 2) VAD — the signal was fine but the clip is wrong
    if vad and not vad.get("found"):
        return ("VAD PROBLEM — speech was recorded but the segmenter "
                "found no utterance; it is gating out the voice.")
    if vad and (vad.get("preroll_s", 0) < 0.1
                or vad.get("speech_s", 0) < 0.2):
        return ("VAD PROBLEM — the segment is suspiciously short or has "
                "no pre-roll; it is likely clipping the first word.")

    # 3) compute — the Pi can't keep up
    if ll.get("cpu_pct", 0) > 90 or ll.get("swap_pct", 0) > 10 \
            or ls.get("secs", 0) > 2.5:
        return ("LOCAL COMPUTE BOTTLENECK — local STT saturates the Pi "
                "(CPU %.0f%%, %.2fs). Use cloud STT as primary; keep "
                "local only for offline." % (ll.get("cpu_pct", 0),
                                             ls.get("secs", 0)))

    # 4) model — audio good, compute fine, still wrong
    local_txt = (ls.get("text") or "").strip()
    cloud_rows = [r for r in ab_rows
                  if r["engine"] not in ("local",) and r.get("text")]
    if cloud_rows and local_txt:
        import difflib
        best = max(cloud_rows,
                   key=lambda r: len(r["text"]))
        ratio = difflib.SequenceMatcher(
            a=local_txt.split(), b=best["text"].split()).ratio()
        if ratio < 0.8:
            return ("STT MODEL ACCURACY PROBLEM — the audio is clean but "
                    "local and cloud transcripts disagree (%.0f%% match). "
                    "The local model is the limit; prefer cloud." %
                    (100 * ratio))

    # 5) network — cloud wanted but unreachable
    if not online:
        return ("NETWORK PROBLEM (or offline) — cloud STT is unreachable, "
                "so the station is on the slower local model. Capture, "
                "VAD and compute look acceptable.")
    if snr < 12:
        return ("MICROPHONE/CAPTURE PROBLEM — SNR %.1f dB is poor; move "
                "the mic closer or reduce room noise." % snr)

    return ("NO OBVIOUS PROBLEM — capture, VAD, compute and transcripts "
            "all look acceptable in this run.")


# ══════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", nargs="?", default="full",
                    choices=["devices", "raw", "mic", "firstlast",
                             "ab", "full"])
    ap.add_argument("arg", nargs="?", default=None,
                    help="seconds for raw, or wav path for ab")
    ap.add_argument("--device", type=int, default=None)
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--preroll-ms", type=int, default=500)
    ap.add_argument("--hangover-ms", type=int, default=800)
    ap.add_argument("--repeats", type=int, default=10)
    a = ap.parse_args(argv)
    dev = a.device if a.device is not None else _pick_input_device()

    if a.mode == "devices":
        list_devices(a.device)
    elif a.mode == "raw":
        capture_raw(float(a.arg or 5.0), device=dev, outdir=a.outdir)
    elif a.mode == "mic":
        mic_test(device=dev, outdir=a.outdir)
    elif a.mode == "firstlast":
        first_last_word_test(a.repeats, device=dev, outdir=a.outdir)
    elif a.mode == "ab":
        wav = a.arg or os.path.join(OUTDIR, "stt_input.wav")
        if not os.path.exists(wav):
            print("No wav at %s — run 'mic' or 'raw' first, or pass a "
                  "path." % wav)
            return 2
        rows, online = ab_stt(wav)
        print("== A/B STT on %s (online=%s) ==" % (wav, online))
        for r in rows:
            print("  %-8s %6.2fs  %r%s"
                  % (r["engine"], r["secs"], r["text"],
                     "  ERROR: " + r["error"] if r["error"] else ""))
        print(diff_report(rows))
    else:
        full(device=dev, outdir=a.outdir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
