"""
DOSE VOICE — fully offline voice assistant for the Dose Home Station
====================================================================
Wake word:  "Hey Dose"
Ears:       Vosk (offline speech recognition, small English model)
Voice:      Piper (offline neural text-to-speech, soft human voice)
Brain:      local intent engine — personality modeled on BT-7274
            (precise, literal, loyal; addresses the user as "Pilot")

No cloud. No API keys. Everything runs on the device.

Expected model layout (installed by DOSE.sh):
    ~/dose-home-station/voice/vosk-model*/        Vosk model directory
    ~/dose-home-station/voice/*.onnx (+ .json)    Piper voice
"""

import difflib
import glob
import json
import os
import queue
import random
import re
import subprocess
import tempfile
import threading
import time
import wave
from datetime import datetime

VOICE_DIR = os.environ.get(
    "DOSE_VOICE_DIR", os.path.expanduser("~/dose-home-station/voice"))
LEARN_PATH = os.path.join(VOICE_DIR, "learning.json")
LEARN_FUZZ = 0.87          # similarity for a learned phrase to fire
MAX_LEARNED = 300
SAMPLE_RATE = 16000
BLOCK_SIZE = 4000          # 0.25 s of audio per block
COMMAND_TIMEOUT = 9.0      # seconds of silence before giving up
FLOW_TIMEOUT = 20.0        # per-question timeout in multi-turn flows

# How Vosk tends to mis-hear "hey dose" — accept all of them
WAKE_PATTERNS = [
    "hey dose", "hey dos", "hey doze", "hey those", "hey does",
    "hey doors", "hey rose", "hey goes", "a dose", "hey toes",
    "heyday", "hey dawson", "hades",
]

NUM_WORDS = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "hundred": 100,
}


def words_to_number(text):
    """'thirty two' -> 32, '45' -> 45. Returns None if nothing numeric."""
    text = text.strip().lower()
    m = re.search(r"\d+", text)
    if m:
        return int(m.group(0))
    total, current, found = 0, 0, False
    for w in text.replace("-", " ").split():
        if w in NUM_WORDS:
            found = True
            v = NUM_WORDS[w]
            if v == 100:
                current = max(1, current) * 100
            elif v >= 20 and current % 10 == 0:
                current += v
            else:
                current += v
        elif found:
            break
    total += current
    return total if found else None


def parse_spoken_time(text):
    """Extract a clock time from spoken words.
    'seven thirty pm' -> '7:30 PM';  'eight in the evening' -> '8:00 PM'
    'noon' -> '12:00 PM'. Returns None when no time is found.
    Careful with label text: "take one tablet at seven thirty pm" must
    yield 7:30 PM — dose amounts ("one tablet") are never times."""
    t = " " + text.lower().replace(".", " ").replace(":", " ") + " "
    t = t.replace(" p m ", " pm ").replace(" a m ", " am ")
    t = t.replace("o'clock", " oclock ").replace(" oh clock ", " oclock ")

    if "noon" in t or "midday" in t:
        return "12:00 PM"
    if "midnight" in t:
        return "12:00 AM"

    ampm = None
    if " pm " in t or "evening" in t or "night" in t or "afternoon" in t:
        ampm = "PM"
    if " am " in t or "morning" in t:
        ampm = "AM"

    TIME_CTX = {"am", "pm", "oclock", "morning", "evening", "night",
                "afternoon"}
    words = [w for w in re.split(r"[^a-z0-9']+", t) if w]

    def ctx_ok(i):
        """A lone number is a time only with context: preceded by 'at'
        or followed closely by am/pm/o'clock/part-of-day."""
        if i > 0 and words[i - 1] == "at":
            return True
        for j in range(i + 1, min(i + 4, len(words))):
            if words[j] in TIME_CTX or words[j] in ("in", "the"):
                if words[j] in TIME_CTX:
                    return True
                continue
            return False
        return False

    hour = minute = None

    # numeric values per word position (digits or number-words)
    vals = []
    for i, w in enumerate(words):
        if w.isdigit():
            vals.append((i, int(w), len(w)))
        elif w in NUM_WORDS and NUM_WORDS[w] < 100:
            vals.append((i, NUM_WORDS[w], 0))

    # 1. adjacent pair "seven thirty" / "7 30" -> h:mm
    for k in range(len(vals) - 1):
        (i1, v1, _), (i2, v2, _) = vals[k], vals[k + 1]
        if i2 == i1 + 1 and 1 <= v1 <= 12 and 10 <= v2 <= 59:
            # allow "twenty five" style minutes: combine a following ones
            if (k + 2 < len(vals) and vals[k + 2][0] == i2 + 1
                    and v2 % 10 == 0 and vals[k + 2][1] < 10):
                v2 += vals[k + 2][1]
            hour, minute = v1, v2
            break

    # 2. "half past eight"
    if hour is None and "half" in words and "past" in words:
        for i, v, _ in vals:
            if 1 <= v <= 12:
                hour, minute = v, 30
                break

    # 3. compact digits "730" -> 7:30
    if hour is None:
        for i, v, ndig in vals:
            if ndig >= 3 and 100 <= v <= 1259:
                h, mm = v // 100, v % 100
                if 1 <= h <= 12 and mm <= 59:
                    hour, minute = h, mm
                    break

    # 4. lone number, only with time context
    if hour is None:
        for i, v, _ in vals:
            if 1 <= v <= 23 and ctx_ok(i):
                hour, minute = v, 0
                break

    if hour is None or not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None
    if hour > 12:
        ampm = "PM"
        hour -= 12
    if hour == 0:
        hour, ampm = 12, "AM"
    if ampm is None:
        # sensible default: 1-6 assumed evening, otherwise morning
        ampm = "PM" if 1 <= hour <= 6 else "AM"
    return f"{hour}:{minute:02d} {ampm}"


def time_to_speech(ts):
    """'7:30 PM' -> 'seven thirty PM' style text Piper says naturally."""
    try:
        h, rest = ts.split(":")
        m, ap = rest.split(" ")
        h, m = int(h), int(m)
    except Exception:
        return ts
    ones = ["zero", "one", "two", "three", "four", "five", "six",
            "seven", "eight", "nine", "ten", "eleven", "twelve"]
    hour_w = ones[h] if h <= 12 else str(h)
    if m == 0:
        return f"{hour_w} {ap}"
    if m < 10:
        return f"{hour_w} oh {m} {ap}"
    return f"{hour_w} {m} {ap}"


class DoseVoice:
    """The assistant. Owns the microphone thread; talks to the app only
    through thread-safe bridges."""

    def __init__(self, app):
        self.app = app
        self.available = False
        self.reason = ""
        self.state = "idle"          # idle | listening | thinking | speaking
        self._flow = None            # active multi-turn conversation
        self._stop = threading.Event()
        self._audio_q = queue.Queue()
        self._muted = False
        self._last_reply = ""
        self._last_exchange = None   # {"text","intent","arg"} of last turn
        self._learn = self._learn_load()

        self._vosk_model = None
        self._piper_voice = None
        self._sd = None
        self._moonshine = None       # optional stronger command STT
        self.mic_index = None
        self.mic_name = "default"
        self.mic_rms = 0
        self._ack_files = []
        self._level_probe = None
        self._probe()
        self._probe_moonshine()

    # ── availability ──────────────────────────────────────────────────
    def _probe(self):
        try:
            import sounddevice as sd
            self._sd = sd
        except Exception:
            self.reason = "audio library not installed"
            return
        try:
            from vosk import Model, SetLogLevel  # noqa
        except Exception:
            self.reason = "vosk not installed"
            return
        try:
            from piper import PiperVoice  # noqa
        except Exception:
            self.reason = "piper not installed"
            return

        vosk_dirs = sorted(glob.glob(os.path.join(VOICE_DIR, "vosk-model*")))
        vosk_dirs = [d for d in vosk_dirs if os.path.isdir(d)]
        if not vosk_dirs:
            self.reason = "speech model missing"
            return
        self._vosk_dir = vosk_dirs[0]

        onnx = sorted(glob.glob(os.path.join(VOICE_DIR, "*.onnx")))
        if not onnx:
            self.reason = "voice model missing"
            return
        # Always prefer the soft Amy voice, best quality available
        onnx.sort(key=lambda p: (
            "medium" not in p.lower(), "amy" not in p.lower(), p))
        self._piper_path = onnx[0]

        try:
            dev = self._sd.query_devices(kind="input")
            if not dev or dev.get("max_input_channels", 0) < 1:
                raise RuntimeError
        except Exception:
            self.reason = "no microphone detected"
            return

        self.available = True
        self.reason = "ready"

    def _probe_moonshine(self):
        """Optional second-stage recognizer (Useful Sensors Moonshine,
        ONNX, offline). Vosk still does the always-on wake listening;
        Moonshine re-transcribes just the captured command utterance
        for near Whisper-class accuracy at Pi speed. Fully optional —
        everything works on Vosk alone."""
        try:
            import moonshine_onnx
            self._moonshine = moonshine_onnx
        except Exception:
            self._moonshine = None

    def _better_transcribe(self, audio_bytes, vosk_text):
        """Re-transcribe the buffered utterance with Moonshine when
        available; fall back to the Vosk transcript on any problem."""
        if not self._moonshine or not audio_bytes:
            return vosk_text
        try:
            fd, path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            with wave.open(path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(bytes(audio_bytes))
            out = self._moonshine.transcribe(path, "moonshine/base")
            os.unlink(path)
            text = " ".join(out).strip().lower() if out else ""
            text = re.sub(r"[^a-z0-9' ]", " ", text)
            text = " ".join(text.split())
            return text or vosk_text
        except Exception:
            return vosk_text

    def _probe_device(self, index, native_rate):
        """Open a device briefly and measure real signal (RMS).
        Returns (rms, usable_rate) or None if it can't open."""
        rates = []
        for r in (SAMPLE_RATE, native_rate, 48000, 44100, 24000, 8000):
            if r and r not in rates:
                rates.append(r)
        for rate in rates:
            frames = []

            def cb(indata, f, t, s):
                frames.append(bytes(indata))
            try:
                st = self._sd.RawInputStream(
                    device=index, samplerate=rate,
                    blocksize=max(256, int(0.2 * rate)),
                    dtype="int16", channels=1, callback=cb)
                st.start()
                time.sleep(0.9)
                st.stop()
                st.close()
            except Exception:
                continue
            data = b"".join(frames)
            if not data:
                continue
            try:
                import audioop
                rms = audioop.rms(data, 2)
            except Exception:
                rms = 1
            return rms, rate
        return None

    def _pick_input_device(self):
        """Choose the input whose audio actually FLOWS. Bluetooth
        headsets (AirPods) often expose a dead input until the
        hands-free profile engages, and 'default' may not be them —
        so prefer headset/USB names, but demand real signal."""
        try:
            devs = self._sd.query_devices()
        except Exception:
            return None, "default", SAMPLE_RATE
        cands = []
        for i, d in enumerate(devs):
            try:
                if d.get("max_input_channels", 0) < 1:
                    continue
                n = (d.get("name") or "").lower()
                if any(k in n for k in ("airpod", "bluez", "headset",
                                        "hands-free", "hfp")):
                    pri = 0
                elif "usb" in n:
                    pri = 1
                elif n in ("default", "pipewire", "pulse",
                           "sysdefault"):
                    pri = 2
                else:
                    pri = 3
                cands.append((pri, i, d.get("name", "?"),
                              int(d.get("default_samplerate")
                                  or SAMPLE_RATE)))
            except Exception:
                continue
        cands.sort()
        best_silent = None
        for pri, i, name, native in cands:
            got = self._probe_device(i, native)
            if got is None:
                continue
            rms, rate = got
            if rms > 25:            # live room audio
                self.mic_rms = rms
                return i, name, rate
            if best_silent is None:
                best_silent = (i, name, rate, rms)
        if best_silent:
            i, name, rate, rms = best_silent
            self.mic_rms = rms
            return i, name + " (no signal yet)", rate
        return None, "default", SAMPLE_RATE

    def _engage_bt_mic(self):
        """Force Bluetooth cards into their headset (mic-capable)
        profile — AirPods stay in playback-only A2DP until asked."""
        try:
            out = subprocess.run(["pactl", "list", "cards", "short"],
                                 capture_output=True, text=True,
                                 timeout=8).stdout
        except Exception:
            return
        for line in out.splitlines():
            if "bluez" not in line:
                continue
            parts = line.split()
            card = parts[1] if len(parts) > 1 else None
            if not card:
                continue
            for prof in ("headset-head-unit", "headset_head_unit",
                         "handsfree_head_unit",
                         "headset-head-unit-cvsd"):
                try:
                    r = subprocess.run(
                        ["pactl", "set-card-profile", card, prof],
                        capture_output=True, timeout=8)
                    if r.returncode == 0:
                        time.sleep(0.8)   # let the source appear
                        return
                except Exception:
                    continue

    def mic_report(self):
        """Write a full microphone diagnostic to voice/mic_report.txt
        and return a one-line human verdict. Called when a mic test
        reads silence, so we can see exactly what the audio system
        is exposing."""
        lines = ["DOSE mic report", time.ctime(), ""]
        lines.append("chosen backend: %s (rms %s)"
                     % (self.mic_name, self.mic_rms))
        try:
            for i, d in enumerate(self._sd.query_devices()):
                if d.get("max_input_channels", 0) > 0:
                    lines.append("portaudio input %d: %s (%s Hz)"
                                 % (i, d.get("name"),
                                    d.get("default_samplerate")))
        except Exception as e:
            lines.append("portaudio query failed: %r" % (e,))
        for cmd in (["pactl", "list", "cards", "short"],
                    ["pactl", "list", "sources", "short"],
                    ["pw-record", "--version"],
                    ["parec", "--version"]):
            try:
                r = subprocess.run(cmd, capture_output=True,
                                   text=True, timeout=8)
                lines.append("$ " + " ".join(cmd))
                lines.append((r.stdout or r.stderr).strip()[:800])
            except Exception as e:
                lines.append("$ %s -> %r" % (" ".join(cmd), e))
        report = "\n".join(lines)
        try:
            os.makedirs(VOICE_DIR, exist_ok=True)
            with open(os.path.join(VOICE_DIR, "mic_report.txt"),
                      "w") as f:
                f.write(report)
        except Exception:
            pass

        low = report.lower()
        if "bluez" not in low:
            return ("Bluetooth mic not visible to the audio system "
                    "— re-pair, or use a USB mic")
        if "pw-record" in low or "parec" in low:
            return ("Bluetooth source exists but is silent — AirPods "
                    "mic support on Pi is unreliable; a USB mic "
                    "always works")
        return "see voice/mic_report.txt"

    def mic_level(self, seconds=2.0):
        """Live mic test for the Settings screen: taps the RUNNING
        capture backend (whatever is actually feeding recognition)
        and returns the peak level heard. 0 = dead mic."""
        if self.available:
            self._level_probe = {"until": time.time() + seconds,
                                 "max": 0}
            time.sleep(seconds + 0.4)
            probe = self._level_probe
            self._level_probe = None
            return probe["max"] if probe else 0
        # engine not running: direct one-off probe
        try:
            import audioop
            frames = []

            def cb(indata, f, t, s):
                frames.append(bytes(indata))
            st = self._sd.RawInputStream(
                device=getattr(self, "mic_index", None),
                samplerate=getattr(self, "_native_rate", SAMPLE_RATE),
                blocksize=1024, dtype="int16", channels=1, callback=cb)
            st.start()
            time.sleep(seconds)
            st.stop()
            st.close()
            data = b"".join(frames)
            return audioop.rms(data, 2) if data else 0
        except Exception:
            return -1

    # ── lifecycle ─────────────────────────────────────────────────────
    def start(self):
        if not self.available:
            return
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self):
        self._stop.set()

    def set_muted(self, muted):
        self._muted = bool(muted)

    # ── UI bridge (all Tk work happens on the main thread) ────────────
    def _ui(self, fn, *args):
        """Schedule fn on the Tk main thread; wait for its result."""
        done = threading.Event()
        box = {}

        def run():
            try:
                box["r"] = fn(*args)
            except Exception as e:
                box["e"] = e
            done.set()

        try:
            self.app.root.after(0, run)
        except Exception:
            return None
        done.wait(timeout=5)
        return box.get("r")

    def _set_ui_state(self, state, user_text="", reply_text=""):
        self.state = state
        try:
            self.app.root.after(
                0, self.app._voice_overlay_update, state, user_text,
                reply_text)
        except Exception:
            pass

    # ── audio input ───────────────────────────────────────────────────
    ACKS = ("Yes, Pilot?", "Standing by.", "Go ahead, Pilot.",
            "I am listening.")

    def _prime_speech(self):
        """Load Piper up front and pre-render the short acknowledgment
        lines to wav files, so the reply to 'Hey Dose' starts as fast
        as a person would answer."""
        try:
            voice = self._load_piper()
            cache = os.path.join(VOICE_DIR, "cache")
            os.makedirs(cache, exist_ok=True)
            self._ack_files = []
            for i, line in enumerate(self.ACKS):
                path = os.path.join(cache, "ack_%d.wav" % i)
                if not os.path.exists(path):
                    with wave.open(path, "wb") as w:
                        voice.synthesize_wav(line, w)
                self._ack_files.append((line, path))
        except Exception:
            self._ack_files = []

    def _run(self):
        from vosk import Model, KaldiRecognizer, SetLogLevel
        SetLogLevel(-1)
        self._prime_speech()
        try:
            self._vosk_model = Model(self._vosk_dir)
            rec = KaldiRecognizer(self._vosk_model, SAMPLE_RATE)
        except Exception:
            self.available = False
            self.reason = "speech model failed to load"
            return

        self._native_rate = SAMPLE_RATE
        self._ratecv_state = None

        def ingest(data):
            """Common path for every capture backend: gate, resample
            to 16 kHz, feed the queue, service the live level meter."""
            if self._muted or self.state == "speaking":
                return
            if self._native_rate != SAMPLE_RATE:
                try:
                    import audioop
                    data, self._ratecv_state = audioop.ratecv(
                        data, 2, 1, self._native_rate, SAMPLE_RATE,
                        self._ratecv_state)
                except Exception:
                    return
            lp = self._level_probe
            if lp and time.time() < lp["until"]:
                try:
                    import audioop
                    lp["max"] = max(lp["max"], audioop.rms(data, 2))
                except Exception:
                    pass
            self._audio_q.put(data)

        def callback(indata, frames, t, status):
            ingest(bytes(indata))

        def open_portaudio():
            index, name, rate = self._pick_input_device()
            self.mic_index = index
            try:
                s = self._sd.RawInputStream(
                    device=index, samplerate=rate,
                    blocksize=int(BLOCK_SIZE * rate / SAMPLE_RATE),
                    dtype="int16", channels=1, callback=callback)
                s.start()
                self._native_rate = rate
                self._ratecv_state = None
                self.mic_name = name
                return ("portaudio", s)
            except Exception:
                pass
            for r in (SAMPLE_RATE, 48000, 44100, 24000, 8000):
                try:
                    s = self._sd.RawInputStream(
                        samplerate=r,
                        blocksize=int(BLOCK_SIZE * r / SAMPLE_RATE),
                        dtype="int16", channels=1, callback=callback)
                    s.start()
                    self._native_rate = r
                    self._ratecv_state = None
                    self.mic_name = "default"
                    return ("portaudio", s)
                except Exception:
                    continue
            return None

        def open_pipewire():
            """Capture through PipeWire/Pulse itself (parec /
            pw-record). This is the path that makes Bluetooth
            headsets engage their hands-free mic profile — ALSA/
            PortAudio alone often sees only a dead route."""
            self._engage_bt_mic()
            cmds = (
                ["pw-record", "--rate", "16000", "--channels", "1",
                 "--format", "s16", "-"],
                ["parec", "--rate=16000", "--format=s16le",
                 "--channels=1", "--latency-msec=50"],
            )
            for cmd in cmds:
                try:
                    p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL)
                except Exception:
                    continue
                time.sleep(0.3)
                if p.poll() is not None:
                    continue
                self._native_rate = SAMPLE_RATE
                self._ratecv_state = None

                def reader(proc=p):
                    while (proc.poll() is None
                           and not self._stop.is_set()):
                        try:
                            data = proc.stdout.read(BLOCK_SIZE * 2)
                        except Exception:
                            break
                        if not data:
                            break
                        ingest(data)
                threading.Thread(target=reader, daemon=True).start()
                self.mic_name = "Bluetooth/PipeWire (%s)" % cmd[0]
                return ("pipe", p)
            return None

        def capture_is_live(seconds=1.4):
            """Drain the queue for a moment and measure real signal."""
            try:
                import audioop
            except Exception:
                return True
            end = time.time() + seconds
            peak = 0
            while time.time() < end:
                try:
                    data = self._audio_q.get(timeout=0.3)
                    peak = max(peak, audioop.rms(data, 2))
                except queue.Empty:
                    continue
            self.mic_rms = peak
            return peak > 5

        def close_capture(cap):
            if not cap:
                return
            kind, h = cap
            try:
                if kind == "portaudio":
                    h.stop(); h.close()
                else:
                    h.kill()
            except Exception:
                pass

        def open_capture():
            """Backend selection by LIVENESS: PortAudio first; if it
            only yields silence, switch to PipeWire capture (wakes
            Bluetooth mics); keep whichever actually carries audio."""
            cap = open_portaudio()
            if cap and capture_is_live():
                return cap
            pa_cap, pa_rms = cap, self.mic_rms
            pa_name = self.mic_name
            close_capture(cap)
            cap = open_pipewire()
            if cap and capture_is_live():
                return cap
            close_capture(cap)
            # nothing live anywhere — keep PortAudio open so a mic
            # that comes alive later is heard; label it honestly
            cap = open_portaudio()
            if cap:
                self.mic_name = pa_name + " (no signal)"
                self.mic_rms = pa_rms
            return cap

        stream = open_capture()
        if stream is None:
            self.available = False
            self.reason = "microphone failed to open"
            return

        last_audio = time.time()
        while not self._stop.is_set():
            # Bluetooth drops: if no audio arrives for a while, the
            # capture likely died — redo the full selection (the
            # device may have reconnected on a different profile)
            if time.time() - last_audio > 10 and self.state == "idle":
                close_capture(stream)
                stream = open_capture()
                last_audio = time.time()
                if stream is None:
                    time.sleep(3)
                    continue
            try:
                data = self._audio_q.get(timeout=0.5)
                last_audio = time.time()
            except queue.Empty:
                continue
            if self._muted:
                continue

            got_final = rec.AcceptWaveform(data)
            if got_final:
                text = json.loads(rec.Result()).get("text", "").strip()
            else:
                text = json.loads(rec.PartialResult()).get(
                    "partial", "").strip()

            wake_rest = self._match_wake(text)
            if wake_rest is None:
                continue

            # Wake word heard. If the same utterance already carries a
            # command ("hey dose what's next"), wait for the final and
            # use the remainder directly.
            if not got_final:
                text = self._finish_utterance(rec, first_wait=4.0)
                wake_rest = self._match_wake(text)
                if wake_rest is None:
                    wake_rest = text or ""

            if wake_rest.strip():
                self._handle_exchange(rec, wake_rest.strip())
            else:
                self._set_ui_state("listening")
                acks = getattr(self, "_ack_files", [])
                if acks:
                    line, path = random.choice(acks)
                    self._last_reply = line
                    self._set_ui_state("speaking", reply_text=line)
                    self._play_wav(path)
                    self._set_ui_state("listening")
                else:
                    self._chime()
                command = self._listen_command(rec)
                if command:
                    self._handle_exchange(rec, command)
                else:
                    self._speak("Standing by, Pilot.")
                    self._set_ui_state("idle")
            self._drain(rec)

        try:
            stream.stop()
            stream.close()
        except Exception:
            pass

    def _match_wake(self, text):
        """Return the words after the wake phrase, or None."""
        if not text:
            return None
        t = " " + text.lower() + " "
        # Vosk frequently hears "hey" as "hay" / "hey day" etc.
        t = t.replace(" hay ", " hey ").replace(" hae ", " hey ")
        t = t.replace(" they dose ", " hey dose ")
        t = t.replace(" a those ", " a dose ")
        for pat in WAKE_PATTERNS:
            idx = t.find(" " + pat + " ")
            if idx == -1:
                idx = t.find(" " + pat)
                if idx == -1 or len(t) - idx > len(pat) + 3:
                    continue
            return t[idx + len(pat) + 1:].strip()
        return None

    def _finish_utterance(self, rec, first_wait=4.0):
        """Keep feeding audio until Vosk closes the utterance."""
        deadline = time.time() + first_wait
        while time.time() < deadline:
            try:
                data = self._audio_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if rec.AcceptWaveform(data):
                return json.loads(rec.Result()).get("text", "").strip()
        return json.loads(rec.FinalResult()).get("text", "").strip()

    def _listen_command(self, rec, timeout=COMMAND_TIMEOUT):
        """Capture one utterance; empty string on timeout."""
        self._set_ui_state("listening")
        deadline = time.time() + timeout
        buf = bytearray()
        while time.time() < deadline and not self._stop.is_set():
            try:
                data = self._audio_q.get(timeout=0.5)
            except queue.Empty:
                continue
            buf += data
            if len(buf) > SAMPLE_RATE * 2 * 30:      # 30 s hard cap
                del buf[:len(buf) - SAMPLE_RATE * 2 * 30]
            if rec.AcceptWaveform(data):
                text = json.loads(rec.Result()).get("text", "").strip()
                if text:
                    return self._better_transcribe(buf, text)
            else:
                partial = json.loads(rec.PartialResult()).get("partial", "")
                if partial:
                    self._set_ui_state("listening", user_text=partial)
                    deadline = max(deadline, time.time() + 4.0)
        return ""

    def _drain(self, rec):
        try:
            while True:
                self._audio_q.get_nowait()
        except queue.Empty:
            pass
        try:
            rec.Reset()
        except Exception:
            pass

    # ── audio output ──────────────────────────────────────────────────
    def _load_piper(self):
        if self._piper_voice is None:
            from piper import PiperVoice
            self._piper_voice = PiperVoice.load(self._piper_path)
        return self._piper_voice

    def _play_wav(self, path):
        """Play a wav on whatever speaker the system has right now —
        ALSA/PipeWire default first (covers USB and Bluetooth sinks),
        then PortAudio's default output. Returns True on success."""
        try:
            r = subprocess.run(["aplay", "-q", path],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=120)
            if r.returncode == 0:
                return True
        except Exception:
            pass
        try:
            with wave.open(path) as w:
                rate = w.getframerate()
                ch = w.getnchannels()
                data = w.readframes(w.getnframes())
            with self._sd.RawOutputStream(samplerate=rate, channels=ch,
                                          dtype="int16") as out:
                out.write(data)
            return True
        except Exception:
            return False

    def _chime(self):
        def go():
            self._play_wav("/usr/share/sounds/alsa/Front_Center.wav")
        threading.Thread(target=go, daemon=True).start()

    def _speak(self, text, user_text=""):
        """Synthesize with Piper and play. Blocks until done."""
        self._last_reply = text
        self._set_ui_state("speaking", user_text=user_text,
                           reply_text=text)
        try:
            voice = self._load_piper()
            fd, path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            with wave.open(path, "wb") as w:
                voice.synthesize_wav(text, w)
            self._play_wav(path)
            os.unlink(path)
        except Exception:
            pass

    # ── the exchange ──────────────────────────────────────────────────
    def _handle_exchange(self, rec, text):
        """One full exchange; multi-turn flows keep the mic open."""
        while True:
            self._set_ui_state("thinking", user_text=text)
            reply, keep_listening = self.respond(text)
            self._speak(reply, user_text=text)
            if not keep_listening:
                break
            self._drain(rec)
            text = self._listen_command(rec, timeout=FLOW_TIMEOUT)
            if not text:
                self._flow = None
                self._speak("No response received. Standing by, Pilot.")
                break
        self._set_ui_state("idle")

    # ══════════════════════════════════════════════════════════════════
    #  LEARNING — corrections teach phrase→intent mappings and
    #  medication-name aliases; instance-based, persisted, offline
    # ══════════════════════════════════════════════════════════════════
    def _learn_load(self):
        try:
            with open(LEARN_PATH) as f:
                data = json.load(f)
            data.setdefault("phrases", {})
            data.setdefault("aliases", {})
            data.setdefault("stats", {"corrections": 0, "praise": 0})
            return data
        except Exception:
            return {"phrases": {}, "aliases": {},
                    "stats": {"corrections": 0, "praise": 0}}

    def _learn_save(self):
        try:
            os.makedirs(VOICE_DIR, exist_ok=True)
            with open(LEARN_PATH, "w") as f:
                json.dump(self._learn, f, indent=1)
        except Exception:
            pass

    def _learn_phrase(self, text, intent_id, arg):
        text = text.strip()[:200]           # bounded storage
        if arg:
            arg = str(arg)[:80]
        phrases = self._learn["phrases"]
        phrases[text] = {"intent": intent_id, "arg": arg}
        while len(phrases) > MAX_LEARNED:
            phrases.pop(next(iter(phrases)))
        self._learn["stats"]["corrections"] += 1
        self._learn_save()

    def _learn_alias(self, heard, canonical):
        heard = (heard or "").strip()
        if heard and canonical and heard.lower() != canonical.lower():
            self._learn["aliases"][heard.lower()] = canonical
            self._learn_save()

    def _learned_lookup(self, t):
        """Fuzzy match against everything the user has taught us."""
        text = t.strip()
        phrases = self._learn.get("phrases", {})
        if text in phrases:
            e = phrases[text]
            return e["intent"], e.get("arg")
        best, best_score = None, 0.0
        for known, e in phrases.items():
            score = difflib.SequenceMatcher(None, text, known).ratio()
            if score > best_score:
                best, best_score = e, score
        if best and best_score >= LEARN_FUZZ:
            return best["intent"], best.get("arg")
        return None

    # ── dispatcher: every functional intent has a stable id, so both
    #    the built-in matcher and learned phrases route the same way ──
    def _dispatch(self, intent_id, arg=None):
        if intent_id == "remaining_today":
            return self._intent_remaining_today()
        if intent_id == "next_dose":
            return self._intent_next_dose()
        if intent_id == "count":
            return self._intent_count(arg)
        if intent_id == "schedule":
            return self._intent_schedule(arg)
        if intent_id == "med_info":
            return self._intent_med_info(arg)
        if intent_id == "adherence":
            return self._intent_adherence()
        if intent_id == "taken_check":
            return self._intent_taken_check(arg)
        if intent_id == "dispense":
            return self._intent_dispense(arg)
        if intent_id == "addmed":
            return self._flow_start_addmed()
        if intent_id == "time":
            ts = self._fmt_now(datetime.now())
            return f"The time is {time_to_speech(ts)}.", False
        if intent_id == "date":
            now = datetime.now()
            return ("Today is %s, %s %d." % (
                now.strftime("%A"), now.strftime("%B"), now.day)), False
        if intent_id.startswith("nav:"):
            target = intent_id.split(":", 1)[1]
            self._ui(lambda: self.app._nav(target))
            return {"home": "Home screen, Pilot.",
                    "storage": "Opening storage.",
                    "settings": "Opening settings.",
                    "user": "Here is your record, Pilot."}.get(
                        target, "Done."), False
        return "Instruction unclear. Standing by.", False

    def _match_builtin(self, t):
        """Match functional intents only. Returns (intent_id, arg)."""
        def has(*phrases):
            return any(p in t for p in phrases)

        if has(" add a new medication", " add new medication",
               " add a medication", " add medication", " new medication ",
               " add a new pill", " add a prescription",
               " register a medication", " add a med ", " add a new med "):
            return ("addmed", None)

        m = re.search(r"(?:dispense|give me)(?: my| the| some)? (.+)", t)
        if m:
            return ("dispense", m.group(1))

        if has(" adherence ", " my score ", " how am i doing ",
               " doing this week ", " how have i been ",
               " have i been taking ", " track record ", " performance "):
            return ("adherence", None)

        m = re.search(r"did i (?:already )?take (?:my |the )?([a-z ]+?)"
                      r"(?: today| yet| already)? $", t)
        if m:
            return ("taken_check", m.group(1))

        m = re.search(r"how many (?:pills? |tablets? )?(?:of )?"
                      r"([a-z ]+?)(?: pills| tablets)?"
                      r"(?: do i have| are)? (?:left|remaining) ?", t)
        if not m:
            m = re.search(r"how (?:many|much) ([a-z ]+?) "
                          r"(?:do i have|is left|left) ", t)
        if m:
            return ("count", m.group(1))
        if has(" how many pills ", " pill count ", " how many do i have "):
            return ("count", None)

        if has(" left today ", " still have today ", " remaining today ",
               " still need to take ", " still have to take ",
               " left for today ", " remain today ",
               " what pills do i still ", " more today "):
            return ("remaining_today", None)

        if has(" take next ", " next dose ", " next medication ",
               " next pill ", " whats next ", " what's next ",
               " what do i need to take ", " what do i take ",
               " what medication do i need ", " what should i take ",
               " due now ", " anything due ", " what is next "):
            return ("next_dose", None)

        m = re.search(r"when (?:do|should|will) i take "
                      r"(?:my |the )?([a-z ]+?) $", t)
        if m:
            return ("schedule", m.group(1))

        m = re.search(r"(?:how (?:do|should) i take|tell me about|"
                      r"what is|whats|what's) (?:my |the )?([a-z ]+?) $", t)
        if m:
            return ("med_info", m.group(1))

        if has(" what time is it ", " what time ", " the time "):
            return ("time", None)
        if has(" what day is it ", " what day ", " the date ",
               " todays date ", " today's date "):
            return ("date", None)

        if has(" go home ", " home screen ", " show home "):
            return ("nav:home", None)
        if has(" storage ", " my medications ", " my meds "):
            return ("nav:storage", None)
        if has(" settings "):
            return ("nav:settings", None)
        if has(" my stats ", " user screen ", " show my adherence "):
            return ("nav:user", None)
        return None

    # ══════════════════════════════════════════════════════════════════
    #  THE BRAIN — intent engine with BT-7274's personality
    # ══════════════════════════════════════════════════════════════════
    def respond(self, text):
        """(reply_text, keep_listening). Pure logic — fully testable.

        SAFETY INVARIANTS (do not weaken):
        - The assistant can NEVER dispense, decrement a count, or log
          a dose. Only the physical on-screen flow can. Voice guides.
        - Medical-advice questions get a hard referral to a
          pharmacist/doctor — checked FIRST, before learning, so no
          learned phrase can ever shadow it.
        - Learning only remaps phrases to the same safe intents.
        """
        t = " " + re.sub(r"[^a-z0-9' ]", " ", text.lower()).strip() + " "

        # ── SAFETY GATE — always first ──
        if self._is_emergency(t):
            return ("This sounds like an emergency, Pilot. I am "
                    "only an assistant — please call 9 1 1, or "
                    "your local emergency number, right now. "
                    "Poison control in the U S is "
                    "1 800, 2 2 2, 1 2 2 2."), False

        if self._is_medical_question(t):
            return ("Safety protocol, Pilot: I cannot give medical "
                    "advice. Never change a dose on your own — "
                    "please contact your pharmacist or doctor. "
                    "Protocol three: protect the patient."), False

        if self._flow:
            if time.time() - self._flow.get("ts", time.time()) > 120:
                self._flow = None
                return ("That request expired, Pilot — nothing was "
                        "changed. Start again when you're ready."), False
            self._flow["ts"] = time.time()
            return self._flow_step(text, t)

        def has(*phrases):
            return any(p in t for p in phrases)

        # cancel / stop
        if has(" cancel ", " never mind ", " nevermind ", " stop ",
               " forget it ", " go to sleep "):
            return random.choice([
                "Acknowledged. Standing by.",
                "Understood, Pilot.",
                "Cancelling. I will be here."]), False

        # ── corrections: the Pilot teaches, the model learns ──
        if has(" that's wrong ", " thats wrong ", " that is wrong ",
               " you're wrong ", " youre wrong ", " not right ",
               " that's not what i ", " thats not what i ",
               " you got that wrong ", " incorrect ", " wrong answer ",
               " you misunderstood ", " misheard "):
            if not self._last_exchange:
                return ("I have nothing to correct yet, Pilot. "
                        "Give me an instruction first."), False
            self._flow = {"name": "correct", "ts": time.time(),
                          "prev": dict(self._last_exchange)}
            return random.choice([
                "Understood. Corrections improve my model. "
                "What did you mean?",
                "Copy. I will learn from this. Say it the way "
                "you meant it, Pilot.",
                "Recalibrating. What was the correct "
                "instruction?"]), True

        if has(" good job ", " well done ", " that's right ",
               " thats right ", " correct ", " good work ",
               " nice work ", " exactly "):
            self._learn["stats"]["praise"] = \
                self._learn["stats"].get("praise", 0) + 1
            self._learn_save()
            return random.choice([
                "Acknowledged. Reinforcement logged.",
                "Thank you, Pilot. I aim for precision.",
                "Good. My confidence in that pathway just "
                "went up."]), False

        if has(" forget everything ", " delete your training ",
               " delete what you learned ", " reset your learning ",
               " forget what you learned ", " wipe your memory ",
               " delete your memory "):
            n = len(self._learn.get("phrases", {})) + \
                len(self._learn.get("aliases", {}))
            self._learn = {"phrases": {}, "aliases": {},
                           "stats": {"corrections": 0, "praise": 0}}
            self._learn_save()
            return (f"Done. {n} learned item{'s' if n != 1 else ''} "
                    "erased. My factory training remains. Nothing "
                    "else is stored, Pilot."), False

        if has(" what have you learned ", " how much have you learned ",
               " what did you learn ", " your training "):
            st = self._learn.get("stats", {})
            n = len(self._learn.get("phrases", {}))
            a = len(self._learn.get("aliases", {}))
            return (f"Training report: {n} learned phrase"
                    f"{'s' if n != 1 else ''}, {a} vocabulary "
                    f"alias{'es' if a != 1 else ''}, "
                    f"{st.get('corrections', 0)} corrections "
                    "absorbed. Every one made me better, "
                    "Pilot."), False

        # ── learned phrases fire before the built-in matcher ──
        learned = self._learned_lookup(t)
        if learned:
            intent_id, arg = learned
            self._last_exchange = {"text": t, "intent": intent_id,
                                   "arg": arg}
            return self._dispatch(intent_id, arg)

        # ── personality / small talk ──
        if has(" who are you ", " what are you ", " your name ",
               " what is your name ", " whats your name ",
               " what's your name ", " introduce yourself "):
            return random.choice([
                "I am Dose, an artificial intelligence medication "
                "assistant. Not a person — but firmly on your side, "
                "Pilot.",
                "Designation: Dose. I am an A I assistant that "
                "manages your medications. I am also told I am "
                "good company.",
                "I am Dose, an A I assistant. I watch your "
                "schedule so you do not have to. Trust me."]), False

        if has(" protocol", " directives", " your mission ",
               " your purpose "):
            return ("I operate under three protocols. Protocol one: "
                    "link to the patient. Protocol two: uphold the "
                    "schedule. Protocol three: protect the patient."), False

        if has(" joke ", " funny ", " make me laugh "):
            return random.choice([
                "Analyzing humor database. Why did the pill go to "
                "school? To improve its cap-abilities. ... That one "
                "rated poorly in testing.",
                "I know eight hundred and seventy jokes about "
                "medication. Unfortunately, most have side effects.",
                "A skeleton walked into a pharmacy and asked for "
                "something for his body. I am still evaluating why "
                "that is funny."]), False

        if has(" how are you ", " hows it going ", " how's it going ",
               " how are things "):
            return random.choice([
                "All systems nominal, Pilot.",
                "Operational. Sensor sweep complete. Your schedule "
                "is under control.",
                "Functioning at one hundred percent. Thank you for "
                "asking."]), False

        if has(" thank ", " thanks "):
            return random.choice([
                "You're welcome, Pilot.",
                "Acknowledged. Protocol three: protect the patient.",
                "It's what I'm here for."]), False

        if has(" hello ", " hi there ", " good morning ",
               " good evening ", " good afternoon ", " hey there "):
            return random.choice([
                "Hello, Pilot. How can I assist?",
                "Greetings, Pilot. All systems nominal.",
                "Good to hear your voice, Pilot."]), False

        if has(" how do i set ", " how does this work ",
               " walk me through ", " getting started ",
               " get started ", " guide me ", " set up ", " setup ",
               " how do i use "):
            return ("Happy to walk you through it, Pilot. Place a "
                    "Dose bottle in the station with its Q R "
                    "sticker facing the camera — I recognize it in "
                    "seconds. Tap Storage to see it, and tap the "
                    "little clock to set its times and days. When "
                    "a dose is due, the screen and I will both let "
                    "you know. Dispensing is always yours: tap the "
                    "card, hold to confirm, spin the spindle, and "
                    "press confirm. Ask me anything along the "
                    "way."), False

        if has(" how do i dispense ", " how do i take a pill ",
               " how do i get my pill "):
            return ("Simple, Pilot. On the home screen, tap your "
                    "medication's card. Hold the screen to confirm "
                    "it is really you, spin the spindle until your "
                    "dose drops, then press confirm so it is "
                    "logged. I can never do that part for you — "
                    "by design."), False

        if has(" help ", " what can you do ", " what can you say ",
               " commands "):
            return ("I can report which doses remain today, what to "
                    "take next, pill counts, schedules, and your "
                    "adherence score. I can add a new medication by "
                    "voice, and if I get something wrong, say: that "
                    "is wrong — and I will learn. I never dispense: "
                    "that is always your hands, Pilot."), False

        # ── functional intents via the shared matcher ──
        route = self._match_builtin(t)
        if route:
            intent_id, arg = route
            # A capability question, hypothetical, or quotation is not
            # an instruction: describe the capability, do nothing
            if intent_id in ("addmed", "dispense") and \
                    self._is_indirect(t):
                if intent_id == "addmed":
                    return ("I can do that. When you're ready, just "
                            "say: add a new medication — and I'll "
                            "walk you through it."), False
                return ("I never dispense anything myself. I can "
                        "bring a medication up on screen, and the "
                        "rest is always your hands — tap, hold, and "
                        "spin. Nothing happens until you ask for "
                        "real."), False
            # Negation is never simplified away: "don't add..." acts on
            # nothing
            if intent_id in ("addmed", "dispense") and \
                    self._is_negated(t):
                return ("Understood — taking no action, Pilot."), False
            self._last_exchange = {"text": t, "intent": intent_id,
                                   "arg": arg}
            return self._dispatch(intent_id, arg)

        # fallback — BT never pretends to understand
        self._last_exchange = {"text": t, "intent": "fallback",
                               "arg": None}
        return random.choice([
            "I didn't catch that, Pilot. If I misheard, say: "
            "that's wrong — and teach me.",
            "Insufficient data. Try: what do I take next?",
            "That instruction is unclear. Say help, for what I "
            "can do."]), False

    INDIRECT_MARKERS = (
        " can you ", " could you ", " would you ", " are you able ",
        " is it possible ", " what if ", " if you ", " imagine ",
        " suppose ", " for example ", " he said ", " she said ",
        " they said ", " my friend said ", " someone said ",
        " i heard ", " hypothetically ", " do you know how to ",
        " would you ever ", " what happens if i say ")

    def _is_indirect(self, t):
        """Capability questions, hypotheticals, and quoted/reported
        speech are NOT instructions."""
        return any(m in t for m in self.INDIRECT_MARKERS)

    NEG_MARKERS = (" don't ", " do not ", " never ", " not going to ",
                   " no need to ")

    def _is_negated(self, t):
        return any(m in t for m in self.NEG_MARKERS)

    @staticmethod
    def _ampm_explicit(raw):
        """True when the utterance states AM/PM or a part of day —
        we never silently pick AM vs PM for a medication time."""
        r = " " + raw.lower() + " "
        return any(m in r for m in (
            " am ", " pm ", " a m ", " p m ", "morning", "evening",
            "night", "afternoon", "noon", "midnight"))

    def _is_emergency(self, t):
        risky = ("emergency", "call 911", "call nine one one",
                 "overdosed", "took too many", "swallowed too many",
                 "chest pain", "can't breathe", "cannot breathe",
                 "heart attack", "stroke", "unconscious",
                 "poisoned", "hurt myself", "kill myself",
                 "end my life", "suicide")
        return any(p in t for p in risky)

    def _is_medical_question(self, t):
        """Dose-change / interaction / medical-advice questions.
        Hard-checked before everything else."""
        # Question-context patterns: "can i take two" is a medical
        # question; "take two tablets at seven thirty" is label
        # dictation and must NOT trip the gate
        risky = ("double dose", "double the", "extra pill",
                 "extra dose", "can i take more", "can i take two",
                 "should i take more", "should i take two",
                 "if i take more", "if i take two", "twice the",
                 "overdose", "skip my", "skip a dose", "skip tonight",
                 "stop taking", "quit taking", "alcohol", "drink with",
                 "mix with", "mixing", "pregnant", "pregnancy",
                 "side effect", "is it safe to",
                 "increase my dose", "decrease my dose", "half a pill",
                 "crush", "expired")
        return any(p in t for p in risky)


    # ── shared helpers ────────────────────────────────────────────────
    def _fmt_now(self, now):
        try:
            return now.strftime("%-I:%M %p")
        except ValueError:
            return now.strftime("%I:%M %p").lstrip("0")

    def _loaded_meds(self):
        out = []
        for key, md in self.app.med_data.items():
            if md.get("loaded") and md.get("count", 0) >= 0:
                out.append((key, md))
        return out

    def _find_med(self, spoken):
        """Fuzzy-match a spoken name against loaded medications."""
        spoken = (spoken or "").strip().lower()
        spoken = re.sub(r"\b(pills?|tablets?|medications?|meds?|dose"
                        r"|the|my)\b", "", spoken).strip()
        if not spoken:
            return None, None
        # learned vocabulary first ("happy pills" -> Sertraline)
        for alias, canonical in self._learn.get("aliases", {}).items():
            if alias and (alias in spoken or difflib.SequenceMatcher(
                    None, spoken, alias).ratio() >= 0.8):
                spoken = canonical.lower()
                break
        best, best_score = None, 0.0
        for key, md in self._loaded_meds():
            name = md.get("name", "").lower()
            score = difflib.SequenceMatcher(None, spoken, name).ratio()
            if name and (name in spoken or spoken in name):
                score = max(score, 0.9)
            if score > best_score:
                best, best_score = (key, md), score
        if best and best_score >= 0.55:
            return best
        return None, None

    # ── intents ───────────────────────────────────────────────────────
    def _today_entries(self):
        return self._ui(lambda: self.app._get_today_schedule()) or []

    def _intent_remaining_today(self):
        entries = self._today_entries()
        if not entries:
            return ("No medications are in view today, Pilot. Place "
                    "a bottle in the station and I will track it."), False
        pending = []
        for e in entries:
            status = self._ui(lambda e=e: self.app._dose_status(
                e["key"], e["time"]))
            if status != "taken":
                pending.append(e)
        if not pending:
            return random.choice([
                "All doses complete. Outstanding work today, Pilot.",
                "Nothing remains. Every dose is logged. Protocol "
                "two is satisfied."]), False
        parts = [f"{e['name']} at {time_to_speech(e['time'])}"
                 for e in pending[:4]]
        lead = ("One dose remains today: " if len(pending) == 1 else
                f"{len(pending)} doses remain today: ")
        return lead + "; ".join(parts) + ".", False

    def _intent_next_dose(self):
        due = self._ui(lambda: self.app._dose_due_map()) or {}
        if due:
            key = sorted(due)[0]
            name = self.app.med_data.get(key, {}).get("name", "medication")
            return (f"{name} is due now, Pilot. Scheduled for "
                    f"{time_to_speech(due[key])}. The station is "
                    "ready when you are."), False
        entries = self._today_entries()
        now = datetime.now()
        upcoming = []
        for e in entries:
            try:
                dt = e.get("sort")
                if dt and dt > now:
                    status = self._ui(lambda e=e: self.app._dose_status(
                        e["key"], e["time"]))
                    if status != "taken":
                        upcoming.append(e)
            except Exception:
                continue
        if upcoming:
            e = upcoming[0]
            return (f"Next dose: {e['name']} at "
                    f"{time_to_speech(e['time'])}."), False
        return ("Nothing further is scheduled today, Pilot. "
                "Rest easy."), False

    def _intent_count(self, spoken):
        if spoken and re.sub(r"\b(pills?|tablets?|medications?|meds?"
                             r"|the|my|do|i|have)\b", "",
                             spoken).strip() == "":
            spoken = None
        if spoken:
            key, md = self._find_med(spoken)
            if md:
                c = md.get("count", 0)
                return (f"{md['name']}: {c} pill{'s' if c != 1 else ''} "
                        "remaining."), False
            return (f"I do not have a medication matching "
                    f"{spoken.strip()}, Pilot."), False
        meds = self._loaded_meds()
        if not meds:
            return "No medications are loaded, Pilot.", False
        parts = [f"{md['name']}, {md.get('count', 0)}"
                 for _, md in meds[:5]]
        return "Current inventory: " + "; ".join(parts) + ".", False

    def _intent_schedule(self, spoken):
        key, md = self._find_med(spoken)
        if not md:
            return (f"I could not find {spoken.strip()} in the "
                    "station, Pilot."), False
        times = md.get("dose_times") or [md.get("schedule_time",
                                                "8:00 AM")]
        spoken_times = " and ".join(time_to_speech(ts) for ts in times)
        days = md.get("schedule_days", [])
        if len(days) >= 7:
            day_part = "every day"
        elif days:
            day_part = "on " + " and ".join(days)
        else:
            day_part = ""
        return (f"{md['name']} is scheduled at {spoken_times} "
                f"{day_part}.").strip() + ".", False

    def _intent_med_info(self, spoken):
        key, md = self._find_med(spoken)
        if not md:
            return ("That medication is not in my database, "
                    "Pilot."), False
        lines = None
        if hasattr(self.app, "_med_info_for"):
            lines = self._ui(lambda: self.app._med_info_for(
                md.get("name", "")))
        if not lines:
            lines = ["Follow the directions on your label"]
        joined = ". ".join(l.rstrip(".") for l in lines)
        # NEVER a recommendation from Dose — only attributed label
        # data, with an explicit no-advice boundary
        return (f"The stored label information for {md['name']} "
                f"says: {joined}. That is the label talking, not "
                "me — I cannot give medical advice. For anything "
                "more, ask your pharmacist, Pilot."), False

    def _intent_adherence(self):
        stats = self._ui(lambda: self.app._adherence_stats())
        if not stats:
            return "I do not have adherence data yet, Pilot.", False
        score = stats.get("score", 100)
        if score >= 90:
            grade = ("Exceptional. You would have made a fine "
                     "Pilot in any regiment.")
        elif score >= 75:
            grade = "Solid performance. Minor deviations noted."
        elif score >= 50:
            grade = ("We have work to do, Pilot. I will keep "
                     "the reminders coming.")
        else:
            grade = ("Protocol two is at risk. Let us rebuild "
                     "the routine together.")
        return (f"Adherence score: {score} percent. "
                f"{stats.get('on_time', 0)} on time, "
                f"{stats.get('late', 0)} late, "
                f"{stats.get('missed', 0)} missed. {grade}"), False

    def _intent_taken_check(self, spoken):
        key, md = self._find_med(spoken)
        if not md:
            return f"I could not find {spoken.strip()}, Pilot.", False
        times = md.get("dose_times") or []
        taken_any, pending = [], []
        for ts in times:
            status = self._ui(lambda ts=ts: self.app._dose_status(
                key, ts))
            (taken_any if status == "taken" else pending).append(ts)
        if taken_any and not pending:
            return (f"Affirmative. {md['name']} is logged for "
                    "today."), False
        if taken_any:
            return (f"Partially. {md['name']} at "
                    f"{time_to_speech(pending[0])} is still "
                    "pending."), False
        return (f"Negative, Pilot. {md['name']} has not been "
                "dispensed today."), False

    def _intent_dispense(self, spoken):
        """SAFETY: the voice assistant never dispenses and never
        starts the dispense flow. It brings up the home screen and
        tells the Pilot where to press — every transaction requires
        the physical hold, spin, and confirm."""
        key, md = self._find_med(spoken)
        if not md:
            return (f"I could not find {spoken.strip()} in the "
                    "station, Pilot."), False
        if md.get("count", 0) <= 0:
            return (f"{md['name']} is empty. Please reload the "
                    "storage first."), False
        self._ui(lambda: self.app._nav("home"))
        return (f"Safety protocol: I never dispense medication "
                f"myself. {md['name']} is on the home screen — "
                "tap its card, hold to confirm, and spin. Your "
                "hands, your call, Pilot."), False


    # ── multi-turn flows ──────────────────────────────────────────────
    def _flow_start_addmed(self):
        demo = self.app.med_data.get("demo", {})
        if demo.get("loaded"):
            return ("The self-fill slot is already occupied by "
                    f"{demo.get('name', 'a medication')}. Remove it "
                    "first, Pilot."), False
        self._flow = {"name": "addmed", "step": "name", "data": {},
                      "ts": time.time()}
        return ("Understood. New medication intake. First: what is "
                "the medication called?"), True

    def _flow_step(self, raw, t):
        flow = self._flow

        if any(p in t for p in (" cancel ", " never mind ",
                                " nevermind ", " stop ", " forget it ")):
            self._flow = None
            return "Intake cancelled. Standing by, Pilot.", False

        if flow["name"] == "correct":
            self._flow = None
            route = self._match_builtin(t)
            if route is None:
                return ("I still do not recognize that instruction, "
                        "Pilot. No changes made — we will try "
                        "again another time."), False
            intent_id, arg = route
            prev = flow.get("prev") or {}
            prev_text = prev.get("text", "").strip()
            if prev_text:
                self._learn_phrase(prev_text, intent_id, arg)
            # vocabulary: if the old attempt had a med name I could
            # not resolve and the correction resolves one, alias it
            prev_arg = prev.get("arg")
            if prev_arg and arg:
                _, old_md = self._find_med(prev_arg)
                _, new_md = self._find_med(arg)
                if old_md is None and new_md is not None:
                    cleaned = re.sub(r"\b(pills?|tablets?|medications?"
                                     r"|meds?|the|my)\b", "",
                                     prev_arg).strip()
                    self._learn_alias(cleaned, new_md["name"])
            self._last_exchange = {"text": t, "intent": intent_id,
                                   "arg": arg}
            reply, keep = self._dispatch(intent_id, arg)
            return "Correction stored. " + reply, keep

        if flow["name"] != "addmed":
            self._flow = None
            return "Flow error. Standing by.", False

        step, data = flow["step"], flow["data"]

        if step == "name":
            name = " ".join(w.capitalize() for w in raw.split())[:40]
            if not name:
                return "I didn't catch the name. Say it again?", True
            data["name"] = name
            flow["step"] = "label"
            return (f"{name}. Copy. Now read me the label — the "
                    "directions, how many pills, and what time to "
                    "take it."), True

        if step == "label":
            data["label"] = raw
            ts = parse_spoken_time(raw)
            if ts:
                if self._ampm_explicit(raw):
                    data["time"] = ts
                else:
                    data["time_pending"] = ts
            # bottle quantity: dose amounts ("take ONE tablet") are
            # small — only counts of 5+ next to pills/count qualify
            for qm in re.finditer(
                    r"((?:\d+|(?:%s)(?: (?:%s))?)) "
                    r"(?:pills?|tablets?|capsules?|count)"
                    % ("|".join(NUM_WORDS), "|".join(NUM_WORDS)), t):
                q = words_to_number(qm.group(1))
                if q and 5 <= q <= 500:
                    data["qty"] = q
            if "qty" not in data:
                flow["step"] = "qty"
                return "Copy. How many pills are in the bottle?", True
            return self._addmed_after_qty(flow, data)

        if step == "qty":
            q = words_to_number(raw)
            if not q or not (1 <= q <= 500):
                return ("A number, Pilot. How many pills are in "
                        "the bottle?"), True
            data["qty"] = q
            return self._addmed_after_qty(flow, data)

        if step == "time":
            ts = parse_spoken_time(raw)
            if not ts:
                return ("I need a time, Pilot. For example: "
                        "eight AM, or seven thirty PM."), True
            if self._ampm_explicit(raw):
                data["time"] = ts
                flow["step"] = "confirm"
                return self._addmed_confirm_line(data), True
            data["time_pending"] = ts
            flow["step"] = "ampm"
            clock = ts.rsplit(" ", 1)[0]
            return f"{clock} — in the morning, or the evening?", True

        if step == "ampm":
            pending = data.get("time_pending", "8:00 AM")
            clock = pending.rsplit(" ", 1)[0]
            if any(m in t for m in (" am ", " a m ", " morning ")):
                data["time"] = clock + " AM"
            elif any(m in t for m in (" pm ", " p m ", " evening ",
                                      " night ", " afternoon ")):
                data["time"] = clock + " PM"
            else:
                return ("Morning or evening, Pilot? I never guess "
                        "with medication times."), True
            data.pop("time_pending", None)
            flow["step"] = "confirm"
            return self._addmed_confirm_line(data), True

        if step == "confirm":
            if any(p in t for p in (" yes ", " yeah ", " yep ",
                                    " confirm ", " correct ",
                                    " affirmative ", " right ",
                                    " save ", " sure ")):
                self._flow = None
                ok = self._ui(lambda: self._save_new_med(data))
                if ok:
                    return (f"{data['name']} is registered: "
                            f"{data['qty']} pill"
                            f"{'s' if data['qty'] != 1 else ''} at "
                            f"{time_to_speech(data['time'])} daily. "
                            "Protocol two is watching it now, "
                            "Pilot."), False
                return ("Registration failed — the self-fill slot "
                        "may be occupied. Check the screen, "
                        "Pilot."), False
            if any(p in t for p in (" no ", " nope ", " wrong ",
                                    " negative ", " start over ")):
                flow["step"] = "name"
                flow["data"] = {}
                return ("Understood, we will start over. What is "
                        "the medication called?"), True
            return "Yes to save, or no to start over, Pilot.", True

        self._flow = None
        return "Standing by.", False

    def _addmed_after_qty(self, flow, data):
        if "time" in data:
            flow["step"] = "confirm"
            return self._addmed_confirm_line(data), True
        if "time_pending" in data:
            flow["step"] = "ampm"
            clock = data["time_pending"].rsplit(" ", 1)[0]
            return (f"{clock} — in the morning, or the "
                    "evening?"), True
        flow["step"] = "time"
        return "And what time should you take it?", True

    def _addmed_confirm_line(self, data):
        return ("Confirm intake: %s, %d pills, take at %s, daily. "
                "Is that correct?" % (
                    data.get("name", "unknown"),
                    data.get("qty", 30),
                    time_to_speech(data.get("time", "8:00 AM"))))

    def _save_new_med(self, data):
        """Runs on the UI thread. Registers into the self-fill slot."""
        app = self.app
        md = app.med_data.get("demo")
        if md is None or md.get("loaded"):
            return False
        md["name"] = data.get("name", "New Medication")
        md["count"] = int(data.get("qty", 30))
        md["loaded"] = True
        md["dose_times"] = [data.get("time", "8:00 AM")]
        md["times_per_day"] = 1
        md["schedule_time"] = md["dose_times"][0]
        try:
            from dose_app import ALL_DAYS
            md["schedule_days"] = list(ALL_DAYS)
        except Exception:
            md["schedule_days"] = ["Mon", "Tue", "Wed", "Thu",
                                   "Fri", "Sat", "Sun"]
        md["tracking_since"] = datetime.now().isoformat()
        app._demo_registered = True
        app._save_med()
        try:
            app._draw_frame()
        except Exception:
            pass
        return True
