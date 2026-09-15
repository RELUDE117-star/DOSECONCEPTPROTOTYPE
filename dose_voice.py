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
BLOCK_SIZE = 2000          # 0.125 s per block — snappy wake response
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
        self.mic_card = None
        self.mic_rms = 0
        self._ack_files = []
        self._level_probe = None
        self._force_reopen = False
        self._ptt_requested = False   # push-to-talk (hold Dose logo)
        self._pause_capture = False   # full self-test holds the devices
        self._paused_ack = False      # capture loop released the device
        self._forced_card = None      # (card, device) the self-test found
        self._forced_sink = None      # user-picked speaker output
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

        # A microphone counts if ANY layer can see one: PortAudio,
        # the PipeWire/Pulse source list, or the kernel's own card
        # list. (PortAudio alone is not enough — when PipeWire owns
        # the hardware, PortAudio can show nothing while pw-record
        # captures perfectly.)
        has_mic = False
        try:
            dev = self._sd.query_devices(kind="input")
            has_mic = bool(dev) and dev.get("max_input_channels",
                                            0) >= 1
        except Exception:
            pass
        if not has_mic:
            try:
                has_mic = bool(self._list_sources())
            except Exception:
                pass
        if not has_mic:
            try:
                with open("/proc/asound/cards") as f:
                    has_mic = "[" in f.read()
            except Exception:
                pass
        if not has_mic:
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

    def _unmute_alsa_inputs(self):
        """USB microphones AND speakers frequently arrive with their
        ALSA capture volume at zero or the capture switch OFF — which
        makes arecord record pure digital silence (the exact 'mic
        never hears anything' failure). Brute-force EVERY control on
        EVERY card to full, enabling capture, with several amixer
        forms so a differently-named C-Media control can't be
        missed. Harmless if already fine."""
        for card in range(6):
            self._max_capture(card)

    def _max_capture(self, card):
        """Force every control on one card to full & capturing."""
        try:
            out = subprocess.run(
                ["amixer", "-c", str(card), "scontrols"],
                capture_output=True, text=True, timeout=5,
                env=self._audio_env()).stdout
        except Exception:
            return
        if not out:
            return
        for line in out.splitlines():
            m = re.search(r"'([^']+)'", line)
            if not m:
                continue
            name = m.group(1)
            low = name.lower()
            capish = any(k in low for k in ("capture", "mic", "input",
                                            "adc"))
            playbackish = (not capish) and any(k in low for k in (
                "speaker", "master", "headphone", "pcm", "output"))
            sourceish = any(k in low for k in ("source", "mux",
                                               "input source"))
            attempts = []
            if not playbackish:
                attempts += [
                    ["100%", "cap", "unmute"],
                    ["100%", "on", "cap"],
                    ["cap"],
                    ["100%", "unmute"],
                ]
            else:
                attempts += [["90%", "unmute", "on"]]
            # An input-source/mux enum: try selecting a mic/line item
            # (PCM2902 'PCM Capture Source' often defaults to the wrong
            # input). Setting an enum to a name it doesn't have is a
            # harmless error.
            if sourceish:
                for item in ("Mic", "Microphone", "Line", "Line In",
                             "Input", "Capture"):
                    attempts.append([item])
            for args in attempts:
                try:
                    subprocess.run(
                        ["amixer", "-c", str(card), "sset", name]
                        + args, capture_output=True, timeout=5,
                        env=self._audio_env())
                except Exception:
                    pass
        # Second pass by numid via 'amixer contents' — catches capture
        # switches/volumes that name-based sset misses (the surest way
        # to turn a capture control ON).
        self._max_capture_by_numid(card)

    def _max_capture_by_numid(self, card):
        """Enable every CAPTURE-capable control by numid with cset."""
        try:
            out = subprocess.run(
                ["amixer", "-c", str(card), "contents"],
                capture_output=True, text=True, timeout=6,
                env=self._audio_env()).stdout
        except Exception:
            return
        numid = None
        is_cap = False
        is_bool = False
        for line in (out or "").splitlines():
            m = re.match(r"numid=(\d+)", line)
            if m:
                numid = m.group(1)
                is_cap = False
                is_bool = False
                low = line.lower()
                # capture controls are marked access=...capture or
                # named with CAPTURE/Mic in the same numid line
                if ("capture" in low or "'mic" in low
                        or "input" in low):
                    is_cap = True
                if "type=boolean" in low:
                    is_bool = True
                continue
            if numid is None:
                continue
            low = line.lower()
            if "capture" in low or "mic" in low or "input" in low:
                is_cap = True
            if "type=boolean" in low:
                is_bool = True
            # once we hit the values line, act
            if line.strip().startswith(": values=") and is_cap:
                try:
                    if is_bool:
                        subprocess.run(
                            ["amixer", "-c", str(card), "cset",
                             "numid=" + numid, "on"],
                            capture_output=True, timeout=5,
                            env=self._audio_env())
                    else:
                        subprocess.run(
                            ["amixer", "-c", str(card), "cset",
                             "numid=" + numid, "100%"],
                            capture_output=True, timeout=5,
                            env=self._audio_env())
                except Exception:
                    pass
                numid = None

    @staticmethod
    def _is_usb_name(name):
        """Does this device name look like a plugged-in USB unit?
        Cheap Pi mics (SunFounder mini and friends) enumerate as
        C-Media 'USB PnP Sound Device' — sometimes without the word
        USB in the name PortAudio shows."""
        n = (name or "").lower()
        return any(k in n for k in ("usb", "pnp", "c-media", "cmedia",
                                    "cm108", "cm106", "audio device"))

    @staticmethod
    def _looks_like_speaker(desc):
        """Does this card look like a pure OUTPUT device (a USB
        speaker) that might expose a dead capture endpoint? Used to
        deprioritize it so a real mic wins. Jieli 'UACDemo' /
        'Advanced Audio Device' boards are the common cheap USB
        speaker the user has alongside the mic."""
        d = (desc or "").lower()
        return any(k in d for k in ("jieli", "uacdemo", "uac demo",
                                    "advanced audio", "speaker",
                                    "headphone", "output", "playback"))

    @staticmethod
    def _looks_like_mic(desc):
        """Does this card look like an actual MICROPHONE (as opposed
        to a speaker's capture endpoint)? Known USB mic chips:
        C-Media 'USB PnP Sound Device' (SunFounder), and the Texas
        Instruments / Burr-Brown PCM2902 'USB Audio CODEC' used by
        many cheap USB mics. Prefer these so we point at the mic,
        never the speaker's input side."""
        d = (desc or "").lower()
        if DoseVoice._looks_like_speaker(desc):
            return False
        return any(k in d for k in (
            "c-media", "cmedia", "cm108", "cm106", "pnp",
            "sound device", "microphone", " mic", "webcam",
            "sunfounder", "pcm2902", "texas instrument",
            "burr-brown", "burr brown", "audio codec", "codec"))

    @staticmethod
    def _arecord_capture_cards():
        """Authoritative list of RECORDABLE devices from `arecord -l`
        — the exact tool the Raspberry Pi mic guides use. It lists
        ONLY capture-capable hardware, with real card AND device
        numbers, e.g.:
            card 2: CODEC [USB Audio CODEC], device 0: USB Audio ...
        Returns [(card, device, "name longname"), ...]. Empty if
        arecord isn't installed (caller falls back to /proc/asound)."""
        try:
            out = subprocess.run(["arecord", "-l"],
                                 capture_output=True, text=True,
                                 timeout=6).stdout
        except Exception:
            return []
        found = []
        for line in (out or "").splitlines():
            m = re.match(
                r"\s*card\s+(\d+):\s*([^\[]*)\[([^\]]*)\].*?"
                r"device\s+(\d+):\s*([^\[]*)\[([^\]]*)\]", line)
            if m:
                card = int(m.group(1))
                dev = int(m.group(4))
                name = " ".join(x.strip() for x in
                                (m.group(2), m.group(3),
                                 m.group(5), m.group(6)) if x.strip())
                found.append((card, dev, name))
        return found

    @staticmethod
    def _card_has_playback(card):
        """True if this ALSA card also exposes a PLAYBACK device — i.e.
        it's a speaker/headset (its capture side is likely a phantom
        endpoint), not a pure microphone. A cheap USB speaker (HONKYOB
        etc.) has playback; a real USB mic is capture-only."""
        try:
            for entry in os.listdir("/proc/asound/card%d" % card):
                if entry.startswith("pcm") and entry.endswith("p"):
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _rank_capture(desc, has_playback=False):
        """Rank capture devices so the REAL mic wins:
          0  capture-only device with a mic-ish name (best)
          1  capture-only device (pure input = almost certainly a mic)
          2  other USB capture device
          3  device that ALSO plays back (a speaker/headset — its
             'capture' is probably a phantom endpoint)
          4  a device whose name looks like a pure speaker
        A USB SPEAKER'S fake input can no longer be mistaken for the
        mic."""
        if DoseVoice._looks_like_speaker(desc):
            return 4
        if has_playback:
            return 3
        if DoseVoice._looks_like_mic(desc):
            return 0
        return 1

    @staticmethod
    def _alsa_capture_cards():
        """Recordable devices as (card, device, desc), the actual
        microphone first. Primary source is `arecord -l` (only lists
        capture hardware); falls back to parsing /proc/asound/cards
        if arecord is unavailable. This is how we LOCATE the real mic
        device (e.g. the PCM2902 USB Audio CODEC) directly, and target
        arecord -D plughw:<card>,<device> at exactly it."""
        rec = DoseVoice._arecord_capture_cards()
        if rec:
            rec.sort(key=lambda c: (
                DoseVoice._rank_capture(
                    c[2], DoseVoice._card_has_playback(c[0])),
                c[0]))
            return rec
        # fallback: kernel card list, assume device 0
        cards = {}
        try:
            with open("/proc/asound/cards") as f:
                text = f.read()
        except Exception:
            return []
        cur = None
        for line in text.splitlines():
            m = re.match(r"\s*(\d+)\s+\[", line)
            if m:
                cur = int(m.group(1))
                cards[cur] = line
            elif cur is not None and cards.get(cur):
                cards[cur] += " " + line.strip()
        capture = []
        for num, desc in cards.items():
            has_cap = False
            try:
                for entry in os.listdir("/proc/asound/card%d" % num):
                    if entry.startswith("pcm") and entry.endswith("c"):
                        has_cap = True
                        break
            except Exception:
                has_cap = True
            if has_cap:
                capture.append((num, 0, desc))
        capture.sort(key=lambda c: (
            DoseVoice._rank_capture(
                c[2], DoseVoice._card_has_playback(c[0])),
            c[0]))
        return capture

    def _pa_refresh(self):
        """Re-scan PortAudio's device list. PortAudio snapshots the
        hardware once at startup, so a USB mic plugged in AFTER launch
        stays invisible until this runs. Only safe to call when no
        capture stream is open — open_capture calls it right after
        closing the old stream."""
        try:
            self._sd._terminate()
            self._sd._initialize()
        except Exception:
            pass

    def _audio_sig(self):
        """Fingerprint of the machine's audio devices. It changes the
        moment a USB or Bluetooth mic/speaker is plugged in or pulled,
        which is how the engine notices hot-plugs. The kernel's own
        card list (/proc/asound/cards) is the primary source — it
        needs no tools installed and every USB audio device appears
        there instantly. Names only — state columns flip constantly
        and would cause spurious reopens."""
        names = []
        try:
            with open("/proc/asound/cards") as f:
                cur = None
                for ln in f:
                    m = re.match(r"\s*(\d+)\s+\[", ln)
                    if m:
                        cur = m.group(1)
                    if "[" in ln and "]" in ln:
                        # include the CARD NUMBER so a USB port swap
                        # (which reassigns card numbers) changes the
                        # fingerprint and triggers re-selection
                        nm = ln.split("[")[1].split("]")[0].strip()
                        names.append("%s:%s" % (cur, nm))
        except Exception:
            pass
        env = self._audio_env()
        for what in ("sources", "sinks"):
            try:
                r = subprocess.run(["pactl", "list", "short", what],
                                   capture_output=True, text=True,
                                   timeout=5, env=env)
                if r.returncode == 0:
                    for ln in (r.stdout or "").splitlines():
                        parts = ln.split()
                        if len(parts) > 1:
                            names.append(parts[1])
            except Exception:
                pass
        return "|".join(sorted(names)) if names else None

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
                if self._is_usb_name(n):
                    pri = 0
                elif n in ("default", "pipewire", "pulse",
                           "sysdefault"):
                    pri = 1
                elif any(k in n for k in ("airpod", "bluez", "headset",
                                          "hands-free", "hfp")):
                    pri = 3          # Bluetooth last — plugged-in wins
                else:
                    pri = 2
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

    def _pw_dump(self):
        try:
            r = subprocess.run(["pw-dump"], capture_output=True,
                               text=True, timeout=10,
                               env=self._audio_env())
            return json.loads(r.stdout) if r.stdout else []
        except Exception:
            return []

    @staticmethod
    def _parse_pw_dump(dump):
        """(bt_devices, sources): Bluetooth devices with their
        available profiles, and live audio input sources."""
        devices, sources = [], []
        for obj in dump:
            try:
                info = obj.get("info") or {}
                props = info.get("props") or {}
                if obj.get("type", "").endswith("Interface:Device") \
                        and props.get("device.api") == "bluez5":
                    profs = []
                    for p in (info.get("params") or {}).get(
                            "EnumProfile") or []:
                        profs.append({"index": p.get("index"),
                                      "name": p.get("name", "")})
                    devices.append({
                        "id": obj.get("id"),
                        "name": props.get("device.description",
                                          props.get("device.name",
                                                    "?")),
                        "profiles": profs})
                elif obj.get("type", "").endswith("Interface:Node") \
                        and (props.get("media.class")
                             == "Audio/Source"):
                    sources.append(props.get(
                        "node.description",
                        props.get("node.name", "?")))
            except Exception:
                continue
        return devices, sources

    def _list_sinks(self):
        """Names of the system's current audio OUTPUTS (sinks)."""
        try:
            out = subprocess.run(["pactl", "list", "short", "sinks"],
                                 capture_output=True, text=True,
                                 timeout=8,
                                 env=self._audio_env()).stdout
            sinks = [ln.split()[1] for ln in out.splitlines()
                     if len(ln.split()) > 1]
            if sinks:
                return sinks
        except Exception:
            pass
        # PipeWire-native fallback when pulseaudio-utils is absent
        sinks = []
        for obj in self._pw_dump():
            try:
                props = (obj.get("info") or {}).get("props") or {}
                if props.get("media.class") == "Audio/Sink":
                    n = props.get("node.name", "")
                    if n:
                        sinks.append(n)
            except Exception:
                continue
        return sinks

    def _pick_output_target(self):
        """The sink Dose speaks through: a USB speaker the moment
        it's plugged in, else any physical output that isn't the
        Pi's (usually silent) HDMI port. Chosen fresh so a speaker
        plugged in mid-session is used on the very next sentence.
        None = trust the system default. Fully independent of the
        microphone choice — separate USB units are fine."""
        now = time.time()
        cached = getattr(self, "_out_cache", None)
        if cached and now - cached[0] < 5:
            return cached[1]
        sinks = self._list_sinks()
        # user's explicit pick wins, if it's still present
        forced = getattr(self, "_forced_sink", None)
        if forced and forced in sinks:
            self._make_default(forced, "Audio/Sink", 0.9)
            self._out_cache = (now, forced)
            return forced
        target = None
        for s in sinks:
            if self._is_usb_name(s):
                target = s
                break
        if target is None:
            physical = [s for s in sinks
                        if "hdmi" not in s.lower()
                        and "bluez" not in s.lower()]
            if physical and len(physical) < len(sinks):
                target = physical[0]
        if target:
            # make it the system default too, unmuted and audible
            self._make_default(target, "Audio/Sink", 0.9)
        self._out_cache = (now, target)
        return target

    def _list_sources(self):
        """Names of real capture sources (speaker monitors excluded)."""
        try:
            out = subprocess.run(["pactl", "list", "short", "sources"],
                                 capture_output=True, text=True,
                                 timeout=8,
                                 env=self._audio_env()).stdout
            srcs = [ln.split()[1] for ln in out.splitlines()
                    if len(ln.split()) > 1
                    and ".monitor" not in ln.split()[1]]
            if srcs:
                return srcs
        except Exception:
            pass
        srcs = []
        for obj in self._pw_dump():
            try:
                props = (obj.get("info") or {}).get("props") or {}
                if props.get("media.class") == "Audio/Source":
                    n = props.get("node.name", "")
                    if n:
                        srcs.append(n)
            except Exception:
                continue
        return srcs

    def _pw_node_id(self, name, media_class):
        """PipeWire node id for a node name, via pw-dump."""
        for obj in self._pw_dump():
            try:
                props = (obj.get("info") or {}).get("props") or {}
                if (props.get("media.class") == media_class
                        and props.get("node.name") == name):
                    return obj.get("id")
            except Exception:
                continue
        return None

    def _make_default(self, target, media_class, boost):
        """Make a node the system default, unmuted, at the given
        gain — via pactl when present, else wpctl (ships with
        WirePlumber on every Pi OS install, so one of the two is
        always there)."""
        kind = ("source" if media_class == "Audio/Source" else "sink")
        env = self._audio_env()
        got = False
        for cmd in (["pactl", "set-default-" + kind, target],
                    ["pactl", "set-%s-mute" % kind, target, "0"],
                    ["pactl", "set-%s-volume" % kind, target,
                     "%d%%" % int(boost * 100)]):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=5,
                                   env=env)
                got = got or r.returncode == 0
            except Exception:
                pass
        if not got:
            nid = self._pw_node_id(target, media_class)
            if nid is not None:
                for cmd in (["wpctl", "set-default", str(nid)],
                            ["wpctl", "set-mute", str(nid), "0"],
                            ["wpctl", "set-volume", str(nid),
                             "%.2f" % boost]):
                    try:
                        subprocess.run(cmd, capture_output=True,
                                       timeout=5, env=env)
                    except Exception:
                        pass

    def _pick_input_target(self):
        """The capture source Dose listens through: the plugged-in
        USB mic, else any other physical (non-Bluetooth) mic. Made
        the system default, unmuted, gain-boosted — so every capture
        route hears the right microphone with zero setup. Fully
        independent of the speaker choice."""
        srcs = self._list_sources()
        target = None
        # 1) a real microphone source (never the speaker's input side)
        for s in srcs:
            if self._looks_like_mic(s):
                target = s
                break
        # 2) a USB source that isn't obviously the speaker
        if target is None:
            for s in srcs:
                if self._is_usb_name(s) and not self._looks_like_speaker(s):
                    target = s
                    break
        # 3) any USB source
        if target is None:
            for s in srcs:
                if self._is_usb_name(s):
                    target = s
                    break
        # 4) any physical (non-Bluetooth) source
        if target is None:
            physical = [s for s in srcs if "bluez" not in s.lower()]
            target = physical[0] if physical else None
        if target:
            # C-Media USB mini mics (SunFounder etc.) are very quiet
            # at stock gain — boost them well past unity
            self._make_default(target, "Audio/Source",
                               1.5 if self._is_usb_name(target)
                               else 1.0)
        return target

    def _engage_bt_mic(self):
        """Force Bluetooth cards into their headset (mic-capable)
        profile — AirPods stay in playback-only A2DP until asked."""
        try:
            out = subprocess.run(["pactl", "list", "cards", "short"],
                                 capture_output=True, text=True,
                                 timeout=8,
                                 env=self._audio_env()).stdout
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
                        capture_output=True, timeout=8,
                        env=self._audio_env())
                    if r.returncode == 0:
                        time.sleep(0.8)   # let the source appear
                        return
                except Exception:
                    continue

    def _engage_bt_mic_pw(self):
        """Profile switch using PipeWire's own tools (pw-dump +
        pw-cli) — these ship with PipeWire itself, so this works
        even when pulseaudio-utils was never installed."""
        devices, _ = self._parse_pw_dump(self._pw_dump())
        for dev in devices:
            head = [p for p in dev["profiles"]
                    if "head" in (p["name"] or "").lower()]
            if not head or dev["id"] is None:
                continue
            idx = head[0]["index"]
            try:
                subprocess.run(
                    ["pw-cli", "set-param", str(dev["id"]),
                     "Profile",
                     '{ "index": %d, "save": true }' % idx],
                    capture_output=True, timeout=10,
                    env=self._audio_env())
                time.sleep(1.0)
                return True
            except Exception:
                continue
        return False

    def list_inputs(self):
        """Names of all input-capable devices for the mic selector,
        USB microphones first (they're the ones people plug in on
        purpose), then Bluetooth, then everything else."""
        out = []
        try:
            for d in self._sd.query_devices():
                if d.get("max_input_channels", 0) > 0:
                    n = d.get("name", "")
                    if n and n not in out:
                        out.append(n)
        except Exception:
            pass
        out.sort(key=lambda n: (
            0 if self._is_usb_name(n) else
            1 if any(k in n.lower() for k in
                     ("airpod", "bluez", "headset")) else 2,
            n.lower()))
        return out

    def request_reopen(self):
        """Ask the capture loop to redo device selection now (used
        when the user picks a different microphone)."""
        self._force_reopen = True

    def mixer_summary(self):
        """One-line state of the active mic card's capture controls:
        e.g. 'Mic 100%[on] · Capture 0%[off]'. Reveals whether a
        control is muted or at zero (software fix) vs. the mixer being
        fine while the device is silent (hardware). '' if unknown."""
        card = getattr(self, "mic_card", None)
        if card is None:
            return ""
        cache = getattr(self, "_mix_cache", None)
        if cache and cache[0] == card and time.time() - cache[1] < 1.0:
            return cache[2]
        try:
            out = subprocess.run(["amixer", "-c", str(card)],
                                 capture_output=True, text=True,
                                 timeout=6, env=self._audio_env()).stdout
        except Exception:
            return ""
        parts = []
        name = None
        for ln in (out or "").splitlines():
            s = ln.strip()
            m = re.match(r"Simple mixer control '([^']+)'", s)
            if m:
                name = m.group(1)
                continue
            if name and ("Capture" in s and "%" in s):
                pm = re.search(r"\[(\d+)%\].*?\[(on|off)\]", s)
                if pm and any(k in name.lower() for k in
                              ("mic", "capture", "input", "adc")):
                    parts.append("%s %s%%[%s]"
                                 % (name, pm.group(1), pm.group(2)))
                    name = None
        summ = " · ".join(parts[:4])
        self._mix_cache = (card, time.time(), summ)
        return summ

    def request_listen(self):
        """Start a listening session right now without the wake word —
        wired to holding the Dose logo. Returns True if the engine is
        running and will listen, False if voice isn't available."""
        if not self.available:
            return False
        self._ptt_requested = True
        return True

    def list_capture_devices(self):
        """All recordable devices as [(card, device, short_name,
        is_mic)], for the on-screen mic picker."""
        out = []
        try:
            for card, dev, desc in self._alsa_capture_cards():
                short = desc.split("[")[0].strip() or desc[:28]
                out.append((card, dev, short[:34],
                            self._looks_like_mic(desc)
                            and not self._card_has_playback(card)))
        except Exception:
            pass
        return out

    def force_card(self, card, device):
        """User picked a specific mic in Settings — use exactly it and
        reopen capture now."""
        self._forced_card = (int(card), int(device))
        self.request_reopen()

    def list_output_devices(self):
        """All audio OUTPUTS as [(sink_name, short_label)], for the
        on-screen speaker picker."""
        out = []
        for s in self._list_sinks():
            short = s
            for p in ("alsa_output.", "bluez_output."):
                if short.startswith(p):
                    short = short[len(p):]
            out.append((s, short[:40]))
        return out

    def force_sink(self, sink):
        """User picked a specific speaker — make it the output and
        play everything through it from now on."""
        self._forced_sink = sink
        self._out_cache = None
        try:
            self._make_default(sink, "Audio/Sink", 0.9)
        except Exception:
            pass

    def _arecord_probe(self, card, device, seconds=2.5):
        """Record DIRECTLY from one capture device and return
        (rms, note). rms>0 = real audio, 0 = silence, -1 = every open
        failed. Tries several device spellings AND rates, because on a
        PipeWire Pi raw plughw can be busy/silent while the ALSA
        'default'/'sysdefault' paths (which go through PipeWire) work."""
        note = "no capture"
        devs = ["plughw:%d,%d" % (card, device),
                "hw:%d,%d" % (card, device),
                "sysdefault:CARD=%d" % card,
                "default"]
        import audioop
        for dev in devs:
            for rate in (48000, 44100, 16000):
                fd, path = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                try:
                    r = subprocess.run(
                        ["arecord", "-D", dev, "-f", "S16_LE",
                         "-r", str(rate), "-c", "1",
                         "-d", str(int(seconds)), path],
                        capture_output=True, text=True,
                        timeout=seconds + 6, env=self._audio_env())
                    if r.returncode != 0:
                        e = (r.stderr or "").strip().splitlines()
                        note = e[-1][:80] if e else "arecord failed"
                        continue
                    with wave.open(path) as w:
                        data = w.readframes(w.getnframes())
                    rms = audioop.rms(data, 2) if data else 0
                    return (rms, "%s @%dHz" % (dev, rate))
                except Exception as e:
                    note = str(e)[:80]
                finally:
                    try:
                        os.unlink(path)
                    except Exception:
                        pass
        return (-1, note)

    def full_mic_test(self, seconds=2.5):
        """THE definitive test: pause our capture, then directly
        arecord from EVERY capture device the system exposes, measure
        the real signal on each, and pick the one that actually hears.
        Writes a full report and, if a device produced sound, forces
        it as the mic. Returns (best_summary, report_lines)."""
        report = ["DOSE full mic test", time.ctime(), ""]
        # 1) full device inventory
        for cmd in (["lsusb"], ["arecord", "-l"]):
            try:
                out = subprocess.run(cmd, capture_output=True,
                                     text=True, timeout=8,
                                     env=self._audio_env())
                report.append("$ " + " ".join(cmd))
                report.append((out.stdout or out.stderr).strip()[:600])
                report.append("")
            except FileNotFoundError:
                report.append("$ %s -> not installed" % " ".join(cmd))
            except Exception as e:
                report.append("$ %s -> %r" % (" ".join(cmd), e))
        # 2) pause the live capture so devices are free to test
        self._pause_capture = True
        # wait until the capture loop confirms it released the device
        # (so the direct probe doesn't hit 'device busy')
        for _ in range(30):
            if getattr(self, "_paused_ack", False):
                break
            time.sleep(0.1)
        time.sleep(0.4)
        best = None      # (rms, card, device, desc)
        try:
            cards = self._alsa_capture_cards()
            report.append("Testing each capture device (speak now!):")
            for card, device, desc in cards:
                self._max_capture(card)
                rms, note = self._arecord_probe(card, device, seconds)
                kind = ("MIC" if self._looks_like_mic(desc) else
                        "speaker-in" if self._looks_like_speaker(desc)
                        else "capture")
                report.append(
                    "  card %d,%d [%s] %s -> level %s (%s)"
                    % (card, device, kind, desc.split("[")[0][:34],
                       rms, note))
                if rms is not None and rms >= 0:
                    if best is None or rms > best[0]:
                        best = (rms, card, device, desc)
        finally:
            self._pause_capture = False
        # 3) act on the result
        if best and best[0] > 8:
            self._forced_card = (best[1], best[2])
            self.request_reopen()
            summary = ("Found the working mic: card %d,%d (level %d). "
                       "Using it now — tap MIC LEVEL and speak."
                       % (best[1], best[2], best[0]))
        elif best is not None:
            # a device opened but was silent
            self._forced_card = (best[1], best[2])
            self.request_reopen()
            summary = ("Every mic opened but stayed silent (best card "
                       "%d,%d level %d). The mic isn't sending audio — "
                       "check it's a MIC not line-in, reseat it, or try "
                       "another USB port." % (best[1], best[2], best[0]))
        else:
            summary = ("No capture device could even be opened — see "
                       "the report; arecord may be missing or the mic "
                       "isn't detected.")
        report.insert(3, "VERDICT: " + summary)
        report.insert(4, "")
        try:
            os.makedirs(VOICE_DIR, exist_ok=True)
            with open(os.path.join(VOICE_DIR, "full_mic_test.txt"),
                      "w") as f:
                f.write("\n".join(report))
        except Exception:
            pass
        return summary, report

    def _mic_pref(self):
        # Selection is fully automatic now — whatever is physically
        # plugged in wins; stale saved choices are ignored.
        return "auto"

    def speaker_test(self):
        """Play a short spoken line on the current speaker. Returns
        the playback path used, or False if nothing could play."""
        try:
            voice = self._load_piper()
            fd, path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            with wave.open(path, "wb") as w:
                self._synth(
                    voice,
                    "Speaker test. If you can hear me, Pilot, this "
                    "speaker is working.", w)
            method = self._play_wav(path)
            os.unlink(path)
            return method
        except Exception:
            return self._play_wav(
                "/usr/share/sounds/alsa/Front_Center.wav")

    def mic_report(self):
        """Write a full microphone diagnostic to voice/mic_report.txt
        and return a one-line human verdict. Called when a mic test
        reads silence, so we can see exactly what the audio system
        is exposing."""
        self._kick_audio_services()
        self._unmute_alsa_inputs()
        lines = ["DOSE mic report", time.ctime(), ""]
        lines.append("chosen backend: %s (rms %s)"
                     % (self.mic_name, self.mic_rms))
        for tline in getattr(self, "mic_trail", []):
            lines.append("route: " + tline)
        try:
            for i, d in enumerate(self._sd.query_devices()):
                if d.get("max_input_channels", 0) > 0:
                    lines.append("portaudio input %d: %s (%s Hz)"
                                 % (i, d.get("name"),
                                    d.get("default_samplerate")))
        except Exception as e:
            lines.append("portaudio query failed: %r" % (e,))
        env = self._audio_env()
        lines.append("uid=%s XDG_RUNTIME_DIR=%s"
                     % (os.getuid(), env.get("XDG_RUNTIME_DIR")))
        # ALSA capture cards straight from the kernel — the arecord
        # path depends only on these, not on PipeWire
        for num, devn, desc in self._alsa_capture_cards():
            tag = ("  <-- MIC" if self._looks_like_mic(desc) else
                   "  (speaker input)" if self._looks_like_speaker(desc)
                   else "")
            lines.append("recordable card %d,%d: %s%s"
                         % (num, devn, desc[:100], tag))
            # the actual mixer controls + levels on this card, so we
            # can see if a capture control is muted or at zero
            try:
                mx = subprocess.run(["amixer", "-c", str(num)],
                                    capture_output=True, text=True,
                                    timeout=6, env=env).stdout
                for ml in (mx or "").splitlines():
                    s = ml.strip()
                    if (s.startswith("Simple mixer control")
                            or "Capture" in s or "Mono:" in s
                            or "Front Left:" in s or "Limits" in s):
                        lines.append("   " + s[:110])
            except Exception:
                pass
        for cmd in (["systemctl", "--user", "is-active", "pipewire",
                     "pipewire-pulse", "wireplumber"],
                    ["arecord", "-l"],
                    ["arecord", "--version"],
                    ["pactl", "info"],
                    ["pactl", "list", "cards", "short"],
                    ["pactl", "list", "sources", "short"],
                    ["pw-record", "--version"],
                    ["parec", "--version"]):
            try:
                r = subprocess.run(cmd, capture_output=True,
                                   text=True, timeout=8, env=env)
                lines.append("$ " + " ".join(cmd))
                lines.append((r.stdout or r.stderr).strip()[:800])
            except FileNotFoundError:
                lines.append("$ %s -> (not installed)"
                             % " ".join(cmd))
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

        # Decisive facts straight from PipeWire itself
        devices, sources = self._parse_pw_dump(self._pw_dump())
        lines2 = ["", "pw-dump summary:"]
        for d in devices:
            lines2.append("  bt device: %s profiles=%s" % (
                d["name"], [p["name"] for p in d["profiles"]]))
        for s in sources:
            lines2.append("  audio source: %s" % s)
        for s in self._list_sinks():
            lines2.append("  audio output: %s" % s)
        lines2.append("  speaker target: %s"
                      % (self._pick_output_target() or "system default"))
        try:
            with open(os.path.join(VOICE_DIR, "mic_report.txt"),
                      "a") as f:
                f.write("\n".join(lines2))
        except Exception:
            pass

        # Every silent-mic verdict forces a full USB-first
        # reselection and has the user retest. Verdicts are kept
        # SHORT so they always fit on screen without truncating —
        # the long story lives in the DETAILS report.
        usb_src = [s for s in self._list_sources()
                   if self._is_usb_name(s)]
        self._pick_input_target()
        self.request_reopen()
        if self._is_usb_name(self.mic_name):
            return "USB mic silent — gain boosted, RETEST + speak"
        if usb_src:
            return "reconnecting USB mic — RETEST in 5 sec"
        try:
            has_inputs = any(
                d.get("max_input_channels", 0) > 0
                and self._is_usb_name(d.get("name"))
                for d in self._sd.query_devices())
        except Exception:
            has_inputs = False
        if has_inputs:
            return "reconnecting USB mic — RETEST in 5 sec"
        return "no USB mic found — reseat the plug, RETEST"

    def mic_meter_sample(self, seconds=0.3):
        """One short sample for the LIVE level meter: returns
        (raw, boosted) peak RMS from the running capture. raw = what
        the microphone physically delivers (0 = truly nothing);
        boosted = what Vosk receives after auto-gain. Cheap and safe
        to call repeatedly for a moving bar."""
        if not self.available:
            return (0, 0)
        self._level_probe = {"until": time.time() + seconds,
                             "max": 0, "raw": 0}
        time.sleep(seconds + 0.1)
        p = self._level_probe
        self._level_probe = None
        if not p:
            return (0, 0)
        return (int(p.get("raw", 0)), int(p.get("max", 0)))

    def mic_level(self, seconds=2.0):
        """Live mic test for the Settings screen: taps the RUNNING
        capture backend (whatever is actually feeding recognition)
        and returns the peak level heard. 0 = dead mic."""
        if self.available:
            self._level_probe = {"until": time.time() + seconds,
                                 "max": 0, "raw": 0}
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
    # Comforting, warm, protective — a gentle guardian, softly reassuring.
    ACKS = ("I'm right here with you.", "I'm listening, take your time.",
            "Go ahead — I've got you.", "I'm here for you, always.")

    def _prime_speech(self):
        """Load Piper up front and pre-render the short acknowledgment
        lines to wav files, so the reply to 'Hey Dose' starts as fast
        as a person would answer. Cache files are keyed by a hash of
        the line text, so changing a line regenerates its audio (no
        stale wav played for new words)."""
        try:
            import hashlib
            voice = self._load_piper()
            cache = os.path.join(VOICE_DIR, "cache")
            os.makedirs(cache, exist_ok=True)
            self._ack_files = []
            for line in self.ACKS:
                key = hashlib.md5(line.encode("utf-8")).hexdigest()[:10]
                path = os.path.join(cache, "ack_%s.wav" % key)
                if not os.path.exists(path):
                    with wave.open(path, "wb") as w:
                        self._synth(voice, line, w)
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
        self._gain = 1.0
        self._max_gain = 20.0    # cap so noise never explodes
        self._nfloor = 50.0      # learned ambient noise floor (RMS)

        def ingest(data):
            """Common path for every capture backend: gate, resample
            to 16 kHz, apply auto-gain, feed the queue, service the
            live level meter."""
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
            # ADAPTIVE AGC with a self-calibrating noise gate: cheap USB
            # mics (C-Media/CM108) capture so quietly that raw speech
            # sits near the noise floor where Vosk hears nothing. We
            # continuously learn the mic's own ambient floor (fast down,
            # very slow up), leave ambient hiss untouched so amplified
            # noise never confuses recognition, and boost only blocks
            # that rise clearly above that floor — toward a healthy RMS
            # for Vosk. This adapts to ANY mic quietness with no fixed
            # threshold a faint mic could never cross.
            rms_raw = 0
            try:
                import audioop
                rms = rms_raw = audioop.rms(data, 2)
                nf = self._nfloor
                if rms < nf:
                    nf = rms                       # track quietest fast
                else:
                    nf = nf * 0.9995 + rms * 0.0005  # rise very slowly
                self._nfloor = max(1.0, nf)
                gate = max(40.0, self._nfloor * 4.0)
                if rms > gate:                     # real signal, not hiss
                    g = max(1.0, min(3000.0 / rms, self._max_gain))
                    # rise quickly toward target, no pumping
                    self._gain = self._gain * 0.5 + g * 0.5
                else:
                    self._gain = 1.0               # ambient: leave clean
                if self._gain > 1.05:
                    data = audioop.mul(data, 2, self._gain)
            except Exception:
                pass
            lp = self._level_probe
            if lp and time.time() < lp["until"]:
                try:
                    import audioop
                    lp["max"] = max(lp["max"], audioop.rms(data, 2))
                    lp["raw"] = max(lp.get("raw", 0), rms_raw)
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

        def open_pipe_cmd(cmd, name, native_rate=SAMPLE_RATE,
                          channels=1):
            """One recorder subprocess (arecord / pw-record / parec)
            as a capture. native_rate tells ingest() what to resample
            from; channels=2 means the reader downmixes stereo to mono
            by taking the LOUDER channel per block, so a USB mic wired
            to only one channel is still captured at full level."""
            try:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL,
                                     env=self._audio_env())
            except Exception:
                return None
            time.sleep(0.3)
            if p.poll() is not None:
                return None
            self._native_rate = native_rate
            self._ratecv_state = None

            def reader(proc=p):
                import audioop
                while (proc.poll() is None
                       and not self._stop.is_set()):
                    try:
                        n = BLOCK_SIZE * 2 * (2 if channels == 2 else 1)
                        data = proc.stdout.read(n)
                    except Exception:
                        break
                    if not data:
                        break
                    if channels == 2:
                        try:
                            left = audioop.tomono(data, 2, 1, 0)
                            right = audioop.tomono(data, 2, 0, 1)
                            data = (left if audioop.rms(left, 2)
                                    >= audioop.rms(right, 2) else right)
                        except Exception:
                            pass
                    ingest(data)
            threading.Thread(target=reader, daemon=True).start()
            self.mic_name = name
            return ("pipe", p)

        def open_arecord(card, device=0):
            """Record straight off an ALSA capture device with arecord.
            Tries, in order, every combination that fixes the common
            'records silence' cases on cheap USB mics (C-Media CM108,
            PCM2902):
              • plughw (rate/format converted) AND raw hw (some devices
                deliver zeros through the plug layer at a wrong rate)
              • native 48 k / 44.1 k / 16 k
              • mono AND stereo (mic wired to one channel only)
            The first combination that opens is used. Does NOT go
            through PipeWire. Ships in alsa-utils."""
            self.mic_card = card      # remember for the mixer readout
            self._max_capture(card)   # unmute + max this card's capture
            for base in ("plughw", "hw"):
                dev = "%s:%d,%d" % (base, card, device)
                for rate in (48000, 44100, 16000):
                    for ch in (1, 2):
                        cmd = ["arecord", "-D", dev, "-f", "S16_LE",
                               "-r", str(rate), "-c", str(ch),
                               "-t", "raw", "-q", "-"]
                        cap = open_pipe_cmd(
                            cmd,
                            "mic arecord %s @%d %dch" % (dev, rate, ch),
                            native_rate=rate, channels=ch)
                        if cap:
                            return cap
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

        def open_named(pref_name):
            """User picked a specific device by name: honor it."""
            try:
                for i, d in enumerate(self._sd.query_devices()):
                    if (d.get("max_input_channels", 0) > 0
                            and d.get("name") == pref_name):
                        for rate in (SAMPLE_RATE,
                                     int(d.get("default_samplerate")
                                         or 48000), 48000, 8000):
                            try:
                                s = self._sd.RawInputStream(
                                    device=i, samplerate=rate,
                                    blocksize=int(BLOCK_SIZE * rate
                                                  / SAMPLE_RATE),
                                    dtype="int16", channels=1,
                                    callback=callback)
                                s.start()
                                self._native_rate = rate
                                self._ratecv_state = None
                                self.mic_name = pref_name
                                return ("portaudio", s)
                            except Exception:
                                continue
            except Exception:
                pass
            return None

        def open_usb_portaudio():
            """Open the plugged-in USB microphone's hardware directly
            by name. Fails when PipeWire has the device claimed —
            which is why the PipeWire route runs first."""
            try:
                devs = self._sd.query_devices()
            except Exception:
                return None
            for i, d in enumerate(devs):
                if d.get("max_input_channels", 0) < 1:
                    continue
                name = d.get("name", "")
                if not self._is_usb_name(name):
                    continue
                for rate in (int(d.get("default_samplerate") or 48000),
                             48000, 44100, SAMPLE_RATE, 8000):
                    try:
                        s = self._sd.RawInputStream(
                            device=i, samplerate=rate,
                            blocksize=int(BLOCK_SIZE * rate
                                          / SAMPLE_RATE),
                            dtype="int16", channels=1,
                            callback=callback)
                        s.start()
                        self._native_rate = rate
                        self._ratecv_state = None
                        self.mic_name = name
                        self.mic_index = i
                        return ("portaudio", s)
                    except Exception:
                        continue
            return None

        def route_floor(seconds=1.6):
            """Peak level from the just-opened route. A real
            microphone ALWAYS has an analog noise floor above zero;
            a wrong or dead route delivers perfect digital silence.
            This tells them apart with nobody speaking."""
            try:
                import audioop
            except Exception:
                return 999
            try:
                while True:
                    self._audio_q.get_nowait()
            except queue.Empty:
                pass
            end = time.time() + seconds
            peak = 0
            while time.time() < end:
                try:
                    data = self._audio_q.get(timeout=0.4)
                    peak = max(peak, audioop.rms(data, 2))
                except queue.Empty:
                    continue
            return peak

        def open_capture():
            """WHAT THE PI HAS SET UP, first: the system-default
            capture route, exactly what the OS's own tools use. Then
            every other route in turn — and the FIRST one that shows
            a real noise floor is kept. No guessing: a live mic is
            never digitally silent; a wrong route always is. The
            full trail of what was tried and what each route heard
            goes into the mic report."""
            self._kick_audio_services()
            self._unmute_alsa_inputs()
            self._pa_refresh()   # see USB devices plugged in after launch
            target = self._pick_input_target()   # unmute + boost USB
            # ARECORD FIRST: straight to the USB mic's ALSA card,
            # zero PipeWire in the path — lowest latency, and the
            # method the mic's own vendor documents. Every capture
            # card the kernel sees, USB ahead of the built-ins.
            # Each route: (label, opener, is_speakerish). A card whose
            # name looks like a pure OUTPUT device (a USB speaker such
            # as the Jieli UAC demo) may also expose a dead capture
            # endpoint — it is deprioritized so a real microphone on
            # another card always wins the tie.
            routes = []
            cap_cards = self._alsa_capture_cards()
            # The full self-test may have found the exact card that
            # hears — try it FIRST, but ONLY if it's still a present
            # capture device (a USB port swap changes card numbers, so
            # a stale pin must never be trusted).
            fc = self._forced_card
            if fc and any(c[0] == fc[0] and c[1] == fc[1]
                          for c in cap_cards):
                routes.append(
                    ("arecord FORCED card %d,%d" % (fc[0], fc[1]),
                     (lambda c=fc[0], d=fc[1]: open_arecord(c, d)),
                     False))
            else:
                self._forced_card = None
            for card_num, dev_num, desc in cap_cards:
                short = desc.split("[")[0].strip() or desc[:20]
                tag = "card %d,%d %s" % (card_num, dev_num, short)
                routes.append(
                    ("arecord %s" % tag.strip(),
                     (lambda c=card_num, d=dev_num: open_arecord(c, d)),
                     self._looks_like_speaker(desc)))
            # TRUE SYSTEM DEFAULTS — route through whatever the Pi is
            # configured to use (these go via ALSA's 'default'/PipeWire
            # plugin, which on modern Pi OS is often the ONLY path that
            # actually delivers audio when raw plughw fights PipeWire).
            def arec(dev, name, ch=1):
                return open_pipe_cmd(
                    ["arecord", "-D", dev, "-f", "S16_LE", "-r",
                     "48000", "-c", str(ch), "-t", "raw", "-q", "-"],
                    name, native_rate=48000, channels=ch)
            default_routes = [
                ("arecord default", lambda: arec("default",
                                                 "arecord default"),
                 False),
                ("arecord plughw default", lambda: arec(
                    "plughw:CARD=default", "arecord plughw default"),
                 False),
            ]
            for card_num, dev_num, desc in cap_cards:
                default_routes.append(
                    ("arecord sysdefault card %d" % card_num,
                     (lambda cn=card_num: arec(
                         "sysdefault:CARD=%d" % cn,
                         "arecord sysdefault card %d" % cn)),
                     self._looks_like_speaker(desc)))
            routes += default_routes
            routes += [
                ("system default (pw-record)", lambda: open_pipe_cmd(
                    ["pw-record", "--rate", "16000", "--channels",
                     "1", "--format", "s16", "-"],
                    "system default (pw-record)"), False),
                ("system default (parec)", lambda: open_pipe_cmd(
                    ["parec", "--rate=16000", "--format=s16le",
                     "--channels=1", "--latency-msec=20"],
                    "system default (parec)"), False),
                ("USB hardware direct", open_usb_portaudio, False),
                ("portaudio default", open_portaudio, False),
            ]
            if target:
                routes += [
                    ("targeted %s (pw-record)" % target,
                     (lambda: open_pipe_cmd(
                         ["pw-record", "--target", target, "--rate",
                          "16000", "--channels", "1", "--format",
                          "s16", "-"], "targeted " + target)), False),
                    ("targeted %s (parec)" % target,
                     (lambda: open_pipe_cmd(
                         ["parec", "-d", target, "--rate=16000",
                          "--format=s16le", "--channels=1",
                          "--latency-msec=50"],
                         "targeted " + target)), False),
                ]
            self.mic_trail = []
            live = None          # best real-signal route (non-speaker)
            live_speaker = None  # live but looks like a speaker's endpoint
            first_openable = None
            for label, opener, speakerish in routes:
                cap = opener()
                if not cap:
                    self.mic_trail.append(label + ": could not open")
                    continue
                floor = route_floor()
                close_capture(cap)
                self.mic_trail.append(
                    "%s: opened, floor %d%s" % (
                        label, floor,
                        " (output device?)" if speakerish else ""))
                if first_openable is None:
                    first_openable = (label, opener)
                if floor > 1:
                    if speakerish:
                        if live_speaker is None:
                            live_speaker = (label, opener)
                    else:
                        live = (label, opener)
                        break   # a real mic with signal — take it
            choice = live or live_speaker or first_openable
            is_live = bool(live or live_speaker)
            if not choice:
                self.mic_trail.append("no capture route opened at all")
                return None
            cap = choice[1]()
            if cap:
                self.mic_name = choice[0] + (
                    " · hearing OK" if is_live else " · SILENT")
                if not is_live:
                    self.mic_trail.append(
                        "every route was digitally silent — kept "
                        + choice[0])
            return cap

        stream = open_capture()
        if stream is None:
            self.available = False
            self.reason = "microphone failed to open"
            return

        last_audio = time.time()
        last_devscan = 0.0
        last_reselect = time.time()
        dev_sig = None
        while not self._stop.is_set():
            # Pause: the full self-test needs exclusive access to every
            # capture device, so it closes our stream and idles here
            # until the test is done, then reopens.
            if self._pause_capture:
                if stream is not None:
                    close_capture(stream)
                    stream = None
                self._paused_ack = True   # device is now released
                time.sleep(0.2)
                continue
            self._paused_ack = False
            if stream is None:
                stream = open_capture()
                last_audio = time.time()
                if stream is None:
                    time.sleep(1)
                    continue
            # Hot-plug watch: a USB/Bluetooth mic or speaker appearing
            # (or vanishing) changes the device fingerprint — redo
            # selection immediately so new hardware just works.
            now = time.time()
            if now - last_devscan > 8 and self.state == "idle":
                last_devscan = now
                sig = self._audio_sig()
                if (sig is not None and dev_sig is not None
                        and sig != dev_sig):
                    # devices changed (plug/unplug OR a USB port swap
                    # that reassigns card numbers) — drop any pinned
                    # card and re-find the mic wherever it now lives
                    self._out_cache = None
                    self._forced_card = None
                    self._force_reopen = True
                if sig is not None:
                    dev_sig = sig
                # a silent mic: keep re-trying — the live one may
                # have just been plugged in
                if (any(k in (self.mic_name or "")
                        for k in ("(no signal", "SILENT"))
                        and now - last_reselect > 20):
                    self._force_reopen = True
            if self._force_reopen:
                self._force_reopen = False
                close_capture(stream)
                stream = open_capture()
                last_audio = time.time()
                last_reselect = time.time()
                if stream is None:
                    time.sleep(3)
                    continue
            # PUSH-TO-TALK: holding the Dose logo starts a listening
            # session directly, no wake word needed — the reliable way
            # in when "Hey Dose" isn't being detected.
            if self._ptt_requested and self.state == "idle":
                self._ptt_requested = False
                self._drain(rec)
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
                    self._speak("I didn't catch that, Pilot. "
                                "Hold the logo and try again.")
                    self._set_ui_state("idle")
                self._drain(rec)
                continue
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

    def _synth(self, voice, text, wav):
        """Synthesize with an EXTREMELY COMFORTING delivery — a soft
        female guardian: calm and unhurried, smooth and even, gentle
        and reassuring, with a little extra space between phrases so it
        feels soothing rather than rushed. This is an ORIGINAL voice
        character, not a copy of any specific game/film character or
        its voice actor. Falls back to the plain call on any Piper API
        difference."""
        # Newer piper-tts: SynthesisConfig(length_scale, noise_scale,...)
        try:
            from piper import SynthesisConfig
            cfg = SynthesisConfig(length_scale=1.16,   # calm, unhurried
                                  noise_scale=0.62,     # smooth, soft
                                  noise_w_scale=0.75)
            try:
                cfg.sentence_silence = 0.35   # gentle pauses (if used)
            except Exception:
                pass
            voice.synthesize_wav(text, wav, syn_config=cfg)
            return
        except Exception:
            pass
        # Older piper-tts: keyword args
        try:
            voice.synthesize_wav(text, wav, length_scale=1.16,
                                 noise_scale=0.62, noise_w=0.75,
                                 sentence_silence=0.35)
            return
        except Exception:
            pass
        try:
            voice.synthesize_wav(text, wav, length_scale=1.16,
                                 noise_scale=0.62, noise_w=0.75)
            return
        except Exception:
            pass
        voice.synthesize_wav(text, wav)   # plain fallback

    @staticmethod
    def _audio_env():
        """Environment that reaches the user's PipeWire session —
        the same one desktop apps (YouTube) use. XDG_RUNTIME_DIR is
        the key: without it pw-play/parec/pactl silently fail."""
        env = dict(os.environ)
        if not env.get("XDG_RUNTIME_DIR"):
            env["XDG_RUNTIME_DIR"] = "/run/user/%d" % os.getuid()
        return env

    def _kick_audio_services(self):
        """Make sure the user audio services are actually running —
        a dead pipewire-pulse means silence in BOTH directions."""
        try:
            subprocess.run(["systemctl", "--user", "start",
                            "pipewire", "pipewire-pulse",
                            "wireplumber"],
                           capture_output=True, timeout=15,
                           env=self._audio_env())
        except Exception:
            pass

    def _play_wav(self, path):
        """Play a wav on whatever the system's ACTIVE output is.
        Order matters: pw-play/paplay follow PipeWire's current sink
        (AirPods, USB — the same route YouTube uses). aplay goes to
        the legacy ALSA default, which on a Pi is often the silent
        HDMI port while still reporting success — so it is only a
        fallback. PortAudio last. A concrete speaker (USB first) is
        targeted explicitly when one exists, so plugging in a USB
        speaker works instantly regardless of the mic."""
        target = self._pick_output_target()
        routes = []
        if target:
            routes += [(["pw-play", "--target", target, path],
                        "PipeWire → " + target),
                       (["paplay", "-d", target, path],
                        "Pulse → " + target)]
        routes += [(["pw-play", path], "PipeWire (pw-play)"),
                   (["paplay", path], "Pulse (paplay)")]
        for cmd, label in routes:
            try:
                r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   timeout=120, env=self._audio_env())
                if r.returncode == 0:
                    return label
            except Exception:
                continue
        try:
            r = subprocess.run(["aplay", "-q", path],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=120,
                               env=self._audio_env())
            if r.returncode == 0:
                return "legacy ALSA (aplay) — may be routed to HDMI"
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
            return "PortAudio default output"
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
                self._synth(voice, text, w)
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
