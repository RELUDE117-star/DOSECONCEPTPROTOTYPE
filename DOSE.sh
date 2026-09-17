#!/bin/bash
# ══════════════════════════════════════════════════════════════
#  DOSE Home Station
#  Double-click → "Execute in Terminal" → app runs
# ══════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/dose-home-station"
RAW_URL="https://raw.githubusercontent.com/relude117-star/doseconceptprototype/claude/quirky-brown-vkHwi"

trap 'echo ""; echo "Something went wrong (see above)."; echo "Press any key to close..."; read -n 1 -s; exit 1' ERR

clear
echo "  DOSE Home Station"
echo ""

# ── Preflight: EVERY launch, verify every component the station
#    needs and install whatever is missing. Libraries are crucial on
#    this device — nothing is trusted to a one-time flag. ──
probe() { python3 -c "import $1" >/dev/null 2>&1; }

echo "  Checking components..."
APT_PKGS=""
PIP_PKGS=""

probe "tkinter"            || APT_PKGS="$APT_PKGS python3-tk"
probe "PIL, PIL.ImageTk"   || APT_PKGS="$APT_PKGS python3-pil python3-pil.imagetk"
probe "pyzbar.pyzbar"      || { APT_PKGS="$APT_PKGS libzbar0"; PIP_PKGS="$PIP_PKGS pyzbar"; }
probe "qrcode"             || PIP_PKGS="$PIP_PKGS qrcode[pil]"
probe "picamera2"          || APT_PKGS="$APT_PKGS python3-picamera2"
probe "sounddevice"        || { APT_PKGS="$APT_PKGS libportaudio2 alsa-utils"; PIP_PKGS="$PIP_PKGS sounddevice"; }
probe "vosk"               || { APT_PKGS="$APT_PKGS python3-srt"; PIP_PKGS="$PIP_PKGS vosk"; }
probe "piper"              || PIP_PKGS="$PIP_PKGS piper-tts"
probe "rapidfuzz"          || PIP_PKGS="$PIP_PKGS rapidfuzz"
probe "jellyfish"          || PIP_PKGS="$PIP_PKGS jellyfish"
probe "moonshine_voice"    || PIP_PKGS="$PIP_PKGS moonshine-voice"
probe "silero_vad"         || PIP_PKGS="$PIP_PKGS silero-vad"
# Python 3.13 removed stdlib audioop; audioop-lts restores it
python3 -c "import audioop" 2>/dev/null || PIP_PKGS="$PIP_PKGS audioop-lts"
command -v pip3 >/dev/null 2>&1 || APT_PKGS="$APT_PKGS python3-pip"
command -v arecord >/dev/null 2>&1 || APT_PKGS="$APT_PKGS alsa-utils"
command -v pactl >/dev/null 2>&1 || APT_PKGS="$APT_PKGS pulseaudio-utils pipewire-pulse"
command -v pw-record >/dev/null 2>&1 || APT_PKGS="$APT_PKGS pipewire"

if [ -n "$APT_PKGS$PIP_PKGS" ]; then
    echo "  Installing missing components:$APT_PKGS$PIP_PKGS"
    echo "  (you may be asked for your password)"
    sudo apt update -y 2>/dev/null || true
    # one package at a time: apt is all-or-nothing per command, so a
    # single unavailable package must never abort the rest
    for PKG in $APT_PKGS fonts-inter fonts-nunito curl; do
        sudo apt install -y "$PKG" 2>/dev/null \
            || echo "  (could not install $PKG — continuing)"
    done
    if [ -n "$PIP_PKGS" ]; then
        python3 -m pip install --break-system-packages $PIP_PKGS \
            || python3 -m pip install $PIP_PKGS \
            || SETUPTOOLS_USE_DISTUTILS=stdlib python3 -m pip install --break-system-packages $PIP_PKGS \
            || true
    fi
fi

# Status table — the truth of what the station has right now
status() { if probe "$1"; then echo "  [ OK ] $2"; else echo "  [MISS] $2  <- $3"; fi; }
status "tkinter"          "Display (Tk)"        "sudo apt install python3-tk"
status "PIL, PIL.ImageTk" "Imaging (Pillow)"    "sudo apt install python3-pil python3-pil.imagetk"
status "pyzbar.pyzbar"    "QR scanning"         "sudo apt install libzbar0; python3 -m pip install pyzbar"
status "picamera2"        "Camera"              "sudo apt install python3-picamera2"
status "sounddevice"      "Voice: audio"        "sudo apt install libportaudio2; python3 -m pip install sounddevice"
status "vosk"             "Voice: recognition"  "python3 -m pip install vosk"
status "piper"            "Voice: speech"       "python3 -m pip install piper-tts"
if command -v pactl >/dev/null 2>&1; then echo "  [ OK ] Bluetooth audio tools"; else echo "  [MISS] Bluetooth audio tools  <- sudo apt install pulseaudio-utils pipewire-pulse"; fi

# Hard requirements to run at all
if ! probe "tkinter" || ! probe "PIL, PIL.ImageTk"; then
    echo ""
    echo "  Core display components are missing — cannot start."
    echo "  Run the commands shown above, then run DOSE.sh again."
    echo "  Press any key to close..."
    read -n 1 -s
    exit 1
fi

# ── Touch sensor (MPR121) — set to true to re-enable I2C setup ──
TOUCH_SENSOR_ENABLED=false

if [ "$TOUCH_SENSOR_ENABLED" = "true" ]; then

# ── Auto-enable I2C for MPR121 touch sensor ──
I2C_NEEDS_REBOOT=false

# Enable I2C in config.txt (works on Pi 4B / Pi 5 / Bookworm)
if ! grep -q "^dtparam=i2c_arm=on" /boot/config.txt 2>/dev/null && \
   ! grep -q "^dtparam=i2c_arm=on" /boot/firmware/config.txt 2>/dev/null; then
    echo "  Enabling I2C for touch sensor..."
    sudo raspi-config nonint do_i2c 0 2>/dev/null || true
    I2C_NEEDS_REBOOT=true
fi

# Ensure i2c-dev loads on boot
if ! grep -q "^i2c-dev" /etc/modules 2>/dev/null; then
    echo "i2c-dev" | sudo tee -a /etc/modules >/dev/null 2>&1 || true
fi

# Load i2c module now
sudo modprobe i2c-dev 2>/dev/null || true
sudo modprobe i2c-bcm2835 2>/dev/null || true

# Add current user to i2c group so we don't need sudo
if ! groups | grep -q i2c 2>/dev/null; then
    sudo usermod -aG i2c "$USER" 2>/dev/null || true
fi

# Check if /dev/i2c-1 exists — if not, a reboot is needed
if [ ! -e /dev/i2c-1 ]; then
    I2C_NEEDS_REBOOT=true
fi

if [ "$I2C_NEEDS_REBOOT" = "true" ]; then
    echo ""
    echo "  ┌──────────────────────────────────────────┐"
    echo "  │  I2C was just enabled for touch sensor.   │"
    echo "  │  Please REBOOT your Pi, then run again.   │"
    echo "  └──────────────────────────────────────────┘"
    echo ""
    echo "  Press any key to close..."
    read -n 1 -s
    exit 0
fi

# Quick I2C scan — show if MPR121 is detected
echo "  Checking touch sensor..."
if command -v i2cdetect >/dev/null 2>&1; then
    if i2cdetect -y 1 2>/dev/null | grep -q "5a"; then
        echo "  MPR121 detected at 0x5A ✓"
    else
        echo "  MPR121 NOT detected on I2C bus 1"
        echo "  Check wiring: VCC→Pin1, SDA→Pin3, SCL→Pin5, GND→Pin9"
    fi
fi

fi  # end TOUCH_SENSOR_ENABLED

# ── Copy app files ──
mkdir -p "$APP_DIR"
cp "$SCRIPT_DIR/dose_app.py" "$APP_DIR/dose_app.py" 2>/dev/null || true
cp "$SCRIPT_DIR/dose_voice.py" "$APP_DIR/dose_voice.py" 2>/dev/null || true
cp "$SCRIPT_DIR/dose_nlu.py" "$APP_DIR/dose_nlu.py" 2>/dev/null || true
cp "$SCRIPT_DIR/DOSE.sh" "$APP_DIR/DOSE.sh" 2>/dev/null || true
cp "$SCRIPT_DIR/dose_logo.png" "$APP_DIR/dose_logo.png" 2>/dev/null || true
cp "$SCRIPT_DIR/demo_qr.png" "$APP_DIR/demo_qr.png" 2>/dev/null || true
chmod +x "$APP_DIR"/*.py "$APP_DIR"/*.sh 2>/dev/null || true
touch "$APP_DIR/.ready"

# Self-heal: if a companion module is missing (e.g. a brand-new file on
# a device that updated before it existed), fetch it from the repo.
for MOD in dose_voice.py dose_nlu.py; do
    if [ ! -f "$APP_DIR/$MOD" ]; then
        curl -fsSL "$RAW_URL/$MOD?nocache=$(date +%s)" -o "$APP_DIR/$MOD" \
            2>/dev/null && echo "  fetched missing $MOD" || true
    fi
done

# ── Voice assistant ("Hey Dose") — offline models, one-time setup ──
VOICE_DIR="$APP_DIR/voice"
mkdir -p "$VOICE_DIR"

# Probe with the SAME python the app runs on — a ready-flag alone
# proved unreliable (models could download while pip silently failed,
# and setup was then never retried)
# Bluetooth audio packages + optional Moonshine — once
if [ ! -f "$VOICE_DIR/.bt_ready3" ]; then
    sudo apt update -y 2>/dev/null || true
    for PKG in pipewire pipewire-alsa pipewire-pulse wireplumber \
            libspa-0.2-bluez5 bluez pulseaudio-utils; do
        sudo apt install -y "$PKG" 2>/dev/null \
            || echo "  (could not install $PKG — continuing)"
    done
    # Optional stronger command recognizer (Moonshine, offline ONNX)
    # openWakeWord: --no-deps skips tflite-runtime (we use onnxruntime)
    python3 -m pip install --break-system-packages --no-deps openwakeword==0.6.0 2>/dev/null \
        || python3 -m pip install --no-deps openwakeword==0.6.0 2>/dev/null || true
    python3 -m pip install --break-system-packages scipy scikit-learn tqdm 2>/dev/null || true
    # The stronger recogniser, used when the fast one cannot make out
    # what was said. Accuracy matters more than the extra download.
    python3 -m pip install --break-system-packages faster-whisper 2>/dev/null \
        || python3 -m pip install faster-whisper 2>/dev/null || true
    # Silero VAD — a real speech/not-speech model. 1.3 MB, and it is
    # what lets the station tell your voice from a running tap.
    python3 -m pip install --break-system-packages silero-vad 2>/dev/null \
        || python3 -m pip install silero-vad 2>/dev/null || true
    python3 -m pip install --break-system-packages useful-moonshine-onnx 2>/dev/null \
        || python3 -m pip install useful-moonshine-onnx 2>/dev/null || true
    touch "$VOICE_DIR/.bt_ready3"
fi

# Bluetooth MIC config: idempotent, checked by FILE on every launch —
# if it's already configured this is a no-op; if not, it gets written
WP_LUA="$HOME/.config/wireplumber/bluetooth.lua.d/50-dose-bluez.lua"
WP_CONF="$HOME/.config/wireplumber/wireplumber.conf.d/50-dose-bluez.conf"
if [ ! -f "$WP_LUA" ] || [ ! -f "$WP_CONF" ]; then
    # ── The critical piece for Bluetooth MICROPHONES (AirPods): ──
    # WirePlumber only offers the hands-free (mic) profile when the
    # headset roles + mSBC codec are enabled. Without this config the
    # AirPods pair as playback-only and their mic is invisible.
    # WirePlumber 0.4 (Pi OS Bookworm) — Lua config:
    mkdir -p "$HOME/.config/wireplumber/bluetooth.lua.d"
    cat > "$HOME/.config/wireplumber/bluetooth.lua.d/50-dose-bluez.lua" <<'WPEOF'
bluez_monitor.properties = {
  ["bluez5.enable-sbc-xq"] = true,
  ["bluez5.enable-msbc"] = true,
  ["bluez5.enable-hw-volume"] = true,
  ["bluez5.headset-roles"] = "[ hsp_hs hsp_ag hfp_hf hfp_ag ]",
  ["bluez5.hfphsp-backend"] = "native",
  ["bluez5.roles"] = "[ a2dp_sink a2dp_source hsp_hs hsp_ag hfp_hf hfp_ag ]",
}
WPEOF
    # WirePlumber 0.5+ — SPA-JSON config (harmless on 0.4):
    mkdir -p "$HOME/.config/wireplumber/wireplumber.conf.d"
    cat > "$HOME/.config/wireplumber/wireplumber.conf.d/50-dose-bluez.conf" <<'WPEOF'
monitor.bluez.properties = {
  bluez5.enable-sbc-xq = true
  bluez5.enable-msbc = true
  bluez5.enable-hw-volume = true
  bluez5.headset-roles = [ hsp_hs hsp_ag hfp_hf hfp_ag ]
  bluez5.hfphsp-backend = "native"
  bluez5.roles = [ a2dp_sink a2dp_source hsp_hs hsp_ag hfp_hf hfp_ag ]
}
WPEOF
    systemctl --user enable --now pipewire pipewire-pulse wireplumber 2>/dev/null || true
    systemctl --user restart wireplumber pipewire pipewire-pulse 2>/dev/null || true
    wpctl settings --save bluetooth.autoswitch-to-headset-profile true 2>/dev/null || true
    echo "  Bluetooth microphone support configured."
fi

# If a Bluetooth device is connected but offers no headset (mic)
# profile, the pairing predates the config above and must be redone
if pactl list cards short 2>/dev/null | grep -q bluez; then
    if ! pactl list cards 2>/dev/null | grep -qiE "headset.head.unit|handsfree"; then
        echo ""
        echo "  ┌──────────────────────────────────────────────────┐"
        echo "  │  Bluetooth headset found, but its MICROPHONE      │"
        echo "  │  profile is missing (pairing predates mic setup). │"
        echo "  │  Fix: Bluetooth menu -> Forget the AirPods, then  │"
        echo "  │  pair them again. Then tap the mic test in        │"
        echo "  │  Settings -> Voice Assistant.                     │"
        echo "  └──────────────────────────────────────────────────┘"
        echo ""
    fi
fi

# Voice models: keyed on the actual files, never on a flag
if ! ls -d "$VOICE_DIR"/vosk-model* >/dev/null 2>&1 \
        || ! ls "$VOICE_DIR"/*.onnx >/dev/null 2>&1; then
    echo "  Downloading voice models (one time, ~120 MB)..."
    python3 - <<'PYEOF2' 2>/dev/null || true
try:
    import moonshine_onnx, numpy as np, tempfile, wave, os
    fd, p = tempfile.mkstemp(suffix=".wav"); os.close(fd)
    with wave.open(p, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    moonshine_onnx.transcribe(p, "moonshine/base")   # warms the model
    os.unlink(p)
    print("  Moonshine command recognizer ready.")
except Exception:
    pass
PYEOF2

    # Speech recognition model (Vosk small English, ~40 MB)
    if ! ls -d "$VOICE_DIR"/vosk-model* >/dev/null 2>&1; then
        echo "  Downloading speech recognition model..."
        TMPZ=$(mktemp --suffix=.zip)
        if curl -sSL -o "$TMPZ" "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip" \
                && [ "$(stat -c%s "$TMPZ" 2>/dev/null || echo 0)" -gt 10000000 ]; then
            (cd "$VOICE_DIR" && unzip -oq "$TMPZ")
        else
            echo "  (speech model download failed — voice will retry next launch)"
        fi
        rm -f "$TMPZ"
    fi

    # HER VOICE — hfc_female. One file, fetched by name. It runs
    # about 3x faster than real time on a Pi 4, which is what keeps
    # replies conversational. Two refs are tried so a single bad path
    # at one mirror can't leave the station mute.
    V_NAME="en_US-hfc_female-medium"
    V_SUB="en/en_US/hfc_female/medium"
    if [ ! -f "$VOICE_DIR/$V_NAME.onnx" ]; then
        echo "  Downloading the voice..."
        for V_REF in v1.0.0 main; do
            V_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/$V_REF/$V_SUB"
            if curl -sSL -o "$VOICE_DIR/$V_NAME.onnx" "$V_BASE/$V_NAME.onnx" \
                && curl -sSL -o "$VOICE_DIR/$V_NAME.onnx.json" "$V_BASE/$V_NAME.onnx.json" \
                && [ "$(stat -c%s "$VOICE_DIR/$V_NAME.onnx" 2>/dev/null || echo 0)" -gt 10000000 ]; then
                echo "  Voice installed: $V_NAME."
                break
            fi
            rm -f "$VOICE_DIR/$V_NAME.onnx" "$VOICE_DIR/$V_NAME.onnx.json"
        done
        [ -f "$VOICE_DIR/$V_NAME.onnx" ] || \
            echo "  (voice download failed — will retry next launch)"
    fi

    # Mark ready only when both models are in place
    if ls -d "$VOICE_DIR"/vosk-model* >/dev/null 2>&1 && ls "$VOICE_DIR"/*.onnx >/dev/null 2>&1; then
        touch "$VOICE_DIR/.voice_ready"
        echo "  Voice assistant ready. Say: Hey Dose."
        echo "  Bluetooth (AirPods): pair via the Bluetooth icon in the"
        echo "  Pi menu bar; Dose picks up the default mic/speaker"
        echo "  automatically, including after reconnects."
    fi
fi

# ── One voice, always hers ──
# This deliberately sits OUTSIDE the "models are missing" block above.
# That block is skipped whenever any .onnx is present, so a station
# carrying an older voice looked complete and kept it forever. Here we
# check by NAME, on every launch, and only ever delete once hers is
# actually on disk.
# Any other voice is deleted ON SIGHT, whether or not hers is here
# yet. That is safe: the app refuses to speak in a voice it does not
# recognise, so an old file is not a fallback that keeps the station
# talking — it is dead weight. Silence until her voice arrives is the
# intended behaviour.
V_NAME="en_US-hfc_female-medium"
RETIRED=""
for F in "$VOICE_DIR"/*.onnx "$VOICE_DIR"/*.onnx.json; do
    [ -e "$F" ] || continue
    case "$(basename "$F")" in
        $V_NAME*) ;;
        *) rm -f "$F"; RETIRED="yes" ;;
    esac
done
if [ -n "$RETIRED" ]; then
    echo "  Retired an older voice; she is the only one now."
    rm -f "$VOICE_DIR"/cache/*.wav 2>/dev/null || true
    rm -f "$VOICE_DIR"/cache/.voice 2>/dev/null || true
fi

# ── Check for updates ──
echo "  Checking for updates..."
TEMP_FILE=$(mktemp)
if curl -sL "$RAW_URL/dose_app.py?nocache=$(date +%s)" -o "$TEMP_FILE" 2>/dev/null; then
    # Compare downloaded file with current
    LOCAL_HASH=$(md5sum "$APP_DIR/dose_app.py" 2>/dev/null | cut -d' ' -f1)
    REMOTE_HASH=$(md5sum "$TEMP_FILE" 2>/dev/null | cut -d' ' -f1)

    if [ -n "$REMOTE_HASH" ] && [ "$LOCAL_HASH" != "$REMOTE_HASH" ]; then
        echo ""
        echo "  ┌─────────────────────────────────────┐"
        echo "  │   An update is available from GitHub  │"
        echo "  └─────────────────────────────────────┘"
        echo ""
        read -p "  Would you like to update? (y/n): " ANSWER
        if [ "$ANSWER" = "y" ] || [ "$ANSWER" = "Y" ]; then
            cp "$TEMP_FILE" "$APP_DIR/dose_app.py"
            # Also update DOSE.sh, the voice assistant, and logo
            curl -sL "$RAW_URL/DOSE.sh?nocache=$(date +%s)" -o "$APP_DIR/DOSE.sh" 2>/dev/null || true
            curl -sL "$RAW_URL/dose_voice.py?nocache=$(date +%s)" -o "$APP_DIR/dose_voice.py" 2>/dev/null || true
            curl -sL "$RAW_URL/dose_logo.png?nocache=$(date +%s)" -o "$APP_DIR/dose_logo.png" 2>/dev/null || true
            curl -sL "$RAW_URL/demo_qr.png?nocache=$(date +%s)" -o "$APP_DIR/demo_qr.png" 2>/dev/null || true
            chmod +x "$APP_DIR"/*.py "$APP_DIR"/*.sh 2>/dev/null || true
            echo "  Updated!"
        else
            echo "  Skipped update."
        fi
    else
        echo "  App is up to date."
    fi
else
    echo "  No internet — skipping update check."
fi
rm -f "$TEMP_FILE"
echo ""

# ── Create desktop shortcut ──
mkdir -p "$HOME/Desktop"
cat > "$HOME/Desktop/DOSE.desktop" << EOF
[Desktop Entry]
Type=Application
Name=DOSE Home Station
Exec=/bin/bash $APP_DIR/DOSE.sh
Terminal=false
StartupNotify=false
EOF
chmod +x "$HOME/Desktop/DOSE.desktop"
gio set "$HOME/Desktop/DOSE.desktop" metadata::trusted true 2>/dev/null || true
dbus-launch gio set "$HOME/Desktop/DOSE.desktop" metadata::trusted true 2>/dev/null || true

# ── Autostart on boot ──
mkdir -p "$HOME/.config/autostart"
cat > "$HOME/.config/autostart/dose.desktop" << EOF
[Desktop Entry]
Type=Application
Name=DOSE Home Station
Exec=/bin/bash $APP_DIR/DOSE.sh
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

# ── Speech model: finish downloading BEFORE the app opens ──
# This deliberately sits outside the one-time setup block and runs on
# every launch until it succeeds. It used to be inside that block, run
# once, and swallow every error — so a failed or interrupted download
# left the app showing "Speech: downloading…" forever with nothing to
# go on and no second attempt.
echo "  Checking the speech model..."
# Whisper does the hearing. Fetch it before the app opens so the first
# thing you say is not paying for a download. The HuggingFace audio
# course builds its assistant's transcription stage on exactly this
# model family; these are the CTranslate2 conversions that make it
# fast enough for a Pi.
python3 - <<'PYEOF'
import os, sys
try:
    from faster_whisper import WhisperModel
except Exception as e:
    print("  Whisper not installed yet (%s) — the app will fetch it."
          % str(e)[:60])
    sys.exit(0)
models = [m for m in os.environ.get(
    "DOSE_WHISPER_MODELS",
    "distil-small.en,small.en,base.en,tiny.en").split(",") if m]
for m in models:
    try:
        print("  Downloading the speech model (%s)..." % m)
        sys.stdout.flush()
        WhisperModel(m, device="cpu", compute_type="int8")
        print("  Speech model ready: %s" % m)
        sys.exit(0)
    except Exception as e:
        print("  %s unavailable (%s)" % (m, str(e)[:70]))
print("  No Whisper model could be downloaded — the app will keep "
      "retrying and use the fast recogniser meanwhile.")
PYEOF

python3 - <<'PYEOF'
import os, sys
try:
    import moonshine_voice as mv
except Exception as e:
    print("  Speech library missing (%s) — the app will install it." % e)
    sys.exit(0)

# MOST ACCURATE FIRST, tiny only as a last resort. The app orders
# them the same way; if these disagree the app downloads a second
# model on first use and the wait lands on the user.
want = {"tiny": "TINY_STREAMING", "base": "BASE_STREAMING",
        "small": "SMALL_STREAMING", "medium": "MEDIUM_STREAMING"}.get(
    os.environ.get("DOSE_STT_ARCH", ""), "")
order = ([want] if want else []) + [
    a for a in ("MEDIUM_STREAMING", "SMALL_STREAMING",
                "BASE_STREAMING", "TINY_STREAMING") if a != want]
order = [a for a in order if hasattr(mv.ModelArch, a)]
if not order:
    print("  This speech package has no usable model types.")
    sys.exit(0)

for arch in order:
    try:
        print("  Downloading the speech model (%s)... this can take a "
              "few minutes on first run." % arch.split("_")[0].lower())
        sys.stdout.flush()
        path, _a = mv.get_model_for_language("en",
                                             getattr(mv.ModelArch, arch))
        print("  Speech model ready: %s" % arch.split("_")[0].lower())
        sys.exit(0)
    except Exception as e:
        print("  %s unavailable (%s)" % (arch.split("_")[0].lower(),
                                         str(e)[:70]))
print("  Speech model could not be downloaded — the app will keep "
      "retrying in the background.")
PYEOF

# ── Pi tuning for a conversational assistant ──
# On a stock Raspberry Pi the CPU sits in "ondemand" and idles at
# 600 MHz. Speech recognition and speech synthesis are short bursts,
# so the governor is still ramping up while you are waiting for a
# reply — which is felt as exactly the lag we are trying to remove.
# Pinning "performance" removes that ramp. It is reversible (it lasts
# until reboot) and needs no config file edits.
echo "  Tuning for low latency..."
for G in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    [ -w "$G" ] && echo performance > "$G" 2>/dev/null || \
        echo performance 2>/dev/null | sudo tee "$G" >/dev/null 2>&1 || true
done

# Keep the ONNX runtimes (speech + voice) to 3 of the 4 cores so the
# touchscreen UI always keeps one and stays at full frame rate.
export OMP_NUM_THREADS=${OMP_NUM_THREADS:-3}
export ORT_NUM_THREADS=${ORT_NUM_THREADS:-3}

# ── Launch ──
echo "  Starting DOSE..."
echo "  Press Esc to exit."
echo ""
export DISPLAY=:0
python3 "$APP_DIR/dose_app.py" 2>"$APP_DIR/error.log"
EXIT_CODE=$?
if [ $EXIT_CODE -ne 0 ]; then
    echo ""
    echo "  DOSE crashed. Error details:"
    echo ""
    cat "$APP_DIR/error.log"
    echo ""
    echo "  Press any key to close..."
    read -n 1 -s
fi
