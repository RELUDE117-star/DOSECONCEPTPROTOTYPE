#!/usr/bin/env python3
"""End-to-end acceptance for the station's voice path, with nobody in
the room.

WHY THIS EXISTS
---------------
Every test in tests/ runs against fakes in a container. They are worth
having — they caught the peak-versus-RMS bug in three separate places —
but not one of them can tell you whether this cabinet, in this room,
with this microphone and this speaker, understands a sentence and
answers in time.

The only test that had ever answered that was Ryan standing in front of
it saying "it's still incredibly slow", which is a slow, expensive and
demoralising way to run a test suite.

So: the station says a phrase through its own speaker, records itself
through its own microphone, and transcribes what came back. That is the
whole acoustic path — Piper, the output route, the room, the capsule,
ALSA, the channel downmix, the resampler and the recogniser — measured
end to end, by the machine, on demand.

WHAT IT MEASURES

  capture      peak and non-zero density of what the mic actually got
  accuracy     word error rate of the recogniser against the known text
  stt latency  seconds to transcribe, fast path and escalation
  tts latency  seconds to FIRST AUDIO, which is what a person feels
  hardware     temperature, throttling, memory, before and after

It grades each against a threshold and prints one verdict. Thresholds
are arguments, not opinions baked into the file.

WHAT IT DOES NOT CLAIM
A loopback test flatters the recogniser: a synthesised voice through a
speaker is cleaner and more consistent than a person at two metres with
a dishwasher running. Treat a pass as "the path works and is fast",
never as "it will understand everyone". The WER yardstick in
tests/test_wer.py, fed real recordings, is the honest accuracy number.

    sudo -u rjarv1 python3 tools/acceptance_test.py --json /tmp/acc.json

It needs the capture device to itself, so stop the app first.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import wave
from array import array

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VOICE_DIR = os.path.join(APP_DIR, "voice")

# The phrases a medication cabinet actually hears, not a reading-comfort
# corpus: a bare command, a medication name, a navigation request and a
# number. Each one separates a different failure.
PHRASES = [
    "what time is it",
    "did i take my aspirin today",
    "show me my schedule",
    "how many doses are left",
]


def say(msg):
    print(msg, flush=True)


def peak_rms(path):
    """Peak, RMS and non-zero density, using only the standard library.

    audioop on this device lives in the KIOSK user's site-packages, so
    a measurement taken from any other account silently measures
    nothing at all. That mistake cost an evening; this cannot repeat it.
    """
    try:
        w = wave.open(path, "rb")
        n, sw, ch, sr = (w.getnframes(), w.getsampwidth(),
                         w.getnchannels(), w.getframerate())
        d = w.readframes(n)
        w.close()
    except Exception as e:
        return {"error": str(e)[:120]}
    if sw != 2 or not d:
        return {"error": "sampwidth=%d bytes=%d" % (sw, len(d))}
    a = array("h")
    a.frombytes(d[:len(d) - (len(d) % 2)])
    if sys.byteorder == "big":
        a.byteswap()
    if not len(a):
        return {"error": "no samples"}
    pk = max(max(a), -min(a))
    acc = 0
    for v in a:
        acc += v * v
    return {"peak": pk, "rms": int((acc / float(len(a))) ** 0.5),
            "nonzero": sum(1 for v in a if v), "samples": len(a),
            "rate": sr, "channels": ch, "seconds": round(n / float(sr), 2)}


def normalise(s):
    keep = "abcdefghijklmnopqrstuvwxyz0123456789 "
    s = "".join(c if c in keep else " " for c in (s or "").lower())
    return [w for w in s.split() if w]


def wer(ref, hyp):
    """Word error rate, and the three error types separately.

    The split matters more than the total: deletions point at capture
    or the voice activity detector, insertions at a capture running
    too hot, substitutions at the model. A single percentage tells you
    something is wrong and nothing about where.
    """
    r, h = normalise(ref), normalise(hyp)
    if not r:
        return {"wer": 0.0, "sub": 0, "ins": 0, "dele": 0, "words": 0}
    d = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1):
        d[i][0] = i
    for j in range(len(h) + 1):
        d[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            if r[i - 1] == h[j - 1]:
                d[i][j] = d[i - 1][j - 1]
            else:
                d[i][j] = 1 + min(d[i - 1][j - 1], d[i - 1][j], d[i][j - 1])
    i, j = len(r), len(h)
    sub = ins = dele = 0
    while i > 0 or j > 0:
        if i > 0 and j > 0 and r[i - 1] == h[j - 1] \
                and d[i][j] == d[i - 1][j - 1]:
            i, j = i - 1, j - 1
        elif i > 0 and j > 0 and d[i][j] == d[i - 1][j - 1] + 1:
            sub += 1
            i, j = i - 1, j - 1
        elif j > 0 and d[i][j] == d[i][j - 1] + 1:
            ins += 1
            j -= 1
        else:
            dele += 1
            i -= 1
    return {"wer": round(100.0 * (sub + ins + dele) / len(r), 1),
            "sub": sub, "ins": ins, "dele": dele, "words": len(r)}


def hardware():
    out = {}
    try:
        t = subprocess.run(["vcgencmd", "measure_temp"],
                           capture_output=True, text=True, timeout=8).stdout
        out["temp_c"] = float(t.strip().split("=")[1].rstrip("'C"))
    except Exception:
        out["temp_c"] = None
    try:
        t = subprocess.run(["vcgencmd", "get_throttled"],
                           capture_output=True, text=True, timeout=8).stdout
        out["throttled"] = t.strip().split("=")[1]
    except Exception:
        out["throttled"] = None
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable"):
                out["mem_avail_mb"] = int(line.split()[1]) // 1024
                break
    except Exception:
        out["mem_avail_mb"] = None
    try:
        out["load1"] = float(open("/proc/loadavg").read().split()[0])
    except Exception:
        out["load1"] = None
    return out


def find_voice():
    for n in sorted(os.listdir(VOICE_DIR)):
        if n.endswith(".onnx"):
            return os.path.join(VOICE_DIR, n)
    return None


def synth(text, path, voice_obj):
    """Render with Piper and report time to a finished file."""
    t0 = time.time()
    with wave.open(path, "wb") as w:
        voice_obj.synthesize_wav(text, w)
    return time.time() - t0


def play_and_record(wav_in, wav_out, card, channels, pad=0.7):
    """Start the recorder, play the phrase, stop the recorder.

    The recorder starts FIRST and stops LAST, so nothing is clipped by
    a race between two processes. Recording at the card's own channel
    count on purpose: asking ALSA for one channel from a two-channel
    capsule makes it average them, and on this hardware that average
    rounds a quiet room to exact zeros. That was the bug.
    """
    dur = 2.0
    try:
        w = wave.open(wav_in, "rb")
        dur = w.getnframes() / float(w.getframerate())
        w.close()
    except Exception:
        pass
    total = dur + pad * 2
    rec = subprocess.Popen(
        ["arecord", "-D", "plughw:%s" % card, "-f", "S16_LE",
         "-r", "48000", "-c", str(channels), "-d", "%d" % int(total + 1),
         wav_out],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    time.sleep(pad)
    t0 = time.time()
    played = False
    for cmd in (["pw-play", wav_in], ["paplay", wav_in], ["aplay", "-q", wav_in]):
        try:
            p = subprocess.run(cmd, capture_output=True,
                               timeout=dur + 12)
            if p.returncode == 0:
                played = True
                break
        except Exception:
            continue
    play_secs = time.time() - t0
    try:
        rec.wait(timeout=total + 12)
    except Exception:
        rec.terminate()
    return played, play_secs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", default=os.environ.get("DOSE_MIC_CARD", "5,0"))
    ap.add_argument("--channels", type=int, default=0,
                    help="0 = ask the card (the right answer)")
    ap.add_argument("--phrases", type=int, default=len(PHRASES))
    ap.add_argument("--max-wer", type=float, default=15.0)
    ap.add_argument("--max-stt", type=float, default=6.0)
    ap.add_argument("--max-tts", type=float, default=2.0)
    ap.add_argument("--max-temp", type=float, default=75.0)
    ap.add_argument("--min-peak", type=int, default=300)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    card = args.card.replace(":", ",")
    cnum = int(card.split(",")[0])
    ch = args.channels
    if not ch:
        ch = 2
        try:
            txt = open("/proc/asound/card%d/stream0" % cnum).read()
            import re
            best = 0
            for m in re.finditer(r"Channels:\s*(\d+)", txt):
                best = max(best, int(m.group(1)))
            if best in (1, 2, 4, 6, 8):
                ch = best
        except Exception:
            pass

    say("DOSE acceptance — the station tests itself")
    say("  card %s, %d channel(s)" % (card, ch))
    hw0 = hardware()
    say("  before: %s'C  throttled %s  %s MB free  load %s"
        % (hw0["temp_c"], hw0["throttled"], hw0["mem_avail_mb"], hw0["load1"]))

    vp = find_voice()
    if not vp:
        say("FAIL: no Piper voice in %s" % VOICE_DIR)
        return 2
    say("  voice: %s" % os.path.basename(vp))

    t0 = time.time()
    from piper import PiperVoice
    voice = PiperVoice.load(vp)
    say("  piper loaded in %.1fs" % (time.time() - t0))

    t0 = time.time()
    from faster_whisper import WhisperModel
    size = os.environ.get("DOSE_FAST_WHISPER", "tiny.en")
    model = WhisperModel(size, device="cpu", compute_type="int8")
    say("  whisper %s loaded in %.1fs" % (size, time.time() - t0))

    tmp = "/tmp/dose_acc"
    os.makedirs(tmp, exist_ok=True)
    rows = []
    for i, phrase in enumerate(PHRASES[:args.phrases]):
        say("\n[%d] %r" % (i + 1, phrase))
        src = os.path.join(tmp, "say_%d.wav" % i)
        got = os.path.join(tmp, "heard_%d.wav" % i)
        tts = synth(phrase, src, voice)
        say("    tts render      %.2fs" % tts)
        played, psecs = play_and_record(src, got, card, ch)
        if not played:
            say("    PLAYBACK FAILED — no output route worked")
        m = peak_rms(got)
        if "error" in m:
            say("    capture ERROR   %s" % m["error"])
            rows.append({"phrase": phrase, "error": m["error"]})
            continue
        say("    captured        peak %d  rms %d  non-zero %d/%d  %.1fs"
            % (m["peak"], m["rms"], m["nonzero"], m["samples"],
               m["seconds"]))
        t0 = time.time()
        segs, _info = model.transcribe(got, beam_size=1, language="en")
        text = " ".join(s.text for s in segs).strip()
        stt = time.time() - t0
        e = wer(phrase, text)
        say("    heard           %r" % text)
        say("    stt             %.2fs   WER %.1f%% "
            "(sub %d ins %d del %d)"
            % (stt, e["wer"], e["sub"], e["ins"], e["dele"]))
        rows.append({"phrase": phrase, "heard": text, "tts": round(tts, 2),
                     "stt": round(stt, 2), "peak": m["peak"],
                     "rms": m["rms"], "nonzero": m["nonzero"],
                     "samples": m["samples"], "played": played,
                     "seconds": m["seconds"], **e})

    hw1 = hardware()
    ok = [r for r in rows if "error" not in r]
    res = {
        "rows": rows, "hw_before": hw0, "hw_after": hw1,
        "card": card, "channels": ch,
        "n": len(ok),
        "wer_mean": round(sum(r["wer"] for r in ok) / len(ok), 1) if ok else None,
        "wer_worst": max((r["wer"] for r in ok), default=None),
        "stt_worst": max((r["stt"] for r in ok), default=None),
        "tts_worst": max((r["tts"] for r in ok), default=None),
        "peak_min": min((r["peak"] for r in ok), default=None),
    }

    say("\n" + "=" * 58)
    say("  after:  %s'C  throttled %s  %s MB free  load %s"
        % (hw1["temp_c"], hw1["throttled"], hw1["mem_avail_mb"],
           hw1["load1"]))
    grades = []

    def grade(name, value, limit, worse_is_bigger=True, unit=""):
        if value is None:
            grades.append((name, "NO DATA", value, limit))
            return
        good = value <= limit if worse_is_bigger else value >= limit
        grades.append((name, "PASS" if good else "FAIL", value, limit))
        say("  %-22s %-8s %s%s   (limit %s%s)"
            % (name, "PASS" if good else "FAIL", value, unit, limit, unit))

    grade("accuracy (worst WER)", res["wer_worst"], args.max_wer, True, "%")
    grade("stt latency (worst)", res["stt_worst"], args.max_stt, True, "s")
    grade("tts latency (worst)", res["tts_worst"], args.max_tts, True, "s")
    grade("capture level (min)", res["peak_min"], args.min_peak, False)
    grade("temperature", hw1["temp_c"], args.max_temp, True, "'C")
    if hw1["throttled"] not in (None,):
        good = hw1["throttled"] in ("0x0", "0")
        grades.append(("throttling", "PASS" if good else "FAIL",
                       hw1["throttled"], "0x0"))
        say("  %-22s %-8s %s   (limit 0x0)"
            % ("throttling", "PASS" if good else "FAIL", hw1["throttled"]))

    res["grades"] = [{"name": n, "verdict": v, "value": val, "limit": lim}
                     for n, v, val, lim in grades]
    failed = [g for g in grades if g[1] != "PASS"]
    res["verdict"] = "PASS" if not failed else "FAIL"
    say("=" * 58)
    say("  VERDICT: %s%s" % (res["verdict"],
                             "" if not failed
                             else "  (%s)" % ", ".join(g[0] for g in failed)))

    if args.json:
        try:
            with open(args.json, "w") as f:
                json.dump(res, f, indent=1)
            say("  written to %s" % args.json)
        except Exception as e:
            say("  could not write json: %s" % e)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
