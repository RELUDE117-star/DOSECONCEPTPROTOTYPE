#!/bin/bash
# ══════════════════════════════════════════════════════════════
#  DOSE Home Station
#  Double-click → "Execute in Terminal" → app runs
# ══════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/dose-home-station"
RAW_URL="https://raw.githubusercontent.com/relude117-star/doseconceptprototype/claude/quirky-brown-vkHwi"

# NEVER BLOCK, AND NEVER DIE, WITHOUT A TERMINAL.
#
# This script is launched two ways: from a desktop icon with a terminal
# attached, and from an autostart entry or a systemd unit with no tty at
# all. The unguarded `read -n 1 -s` here would wait forever for a
# keypress nobody is there to give, so it is only attempted when stdin
# really is a terminal, and even then with a timeout.
trap 'echo ""; echo "Something went wrong (see above)."; \
      if [ -t 0 ]; then echo "Press any key to close..."; \
      read -n 1 -s -t 30; fi; exit 1' ERR

# `clear` needs TERM. Under systemd there is no TERM and no tty, so it
# exits non-zero — which, with the ERR trap above, killed the whole
# launcher before it did anything. The service log said exactly this,
# five times in a row as systemd retried:
#
#   TERM environment variable not set.
#   Something went wrong (see above).
#
# Clearing a screen nobody is looking at is not worth failing a launch
# over, so this is now advisory.
clear 2>/dev/null || true
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
# gradio_client lets the Pi use a FREE Hugging Face ZeroGPU Whisper
# Space instead of running Whisper locally. Optional — cloud STT also
# works through Groq with plain HTTP, and everything falls back to the
# local model offline.
probe "gradio_client"      || PIP_PKGS="$PIP_PKGS gradio_client"
# Python 3.13 removed stdlib audioop; audioop-lts restores it
python3 -c "import audioop" 2>/dev/null || PIP_PKGS="$PIP_PKGS audioop-lts"
command -v pip3 >/dev/null 2>&1 || APT_PKGS="$APT_PKGS python3-pip"
command -v arecord >/dev/null 2>&1 || APT_PKGS="$APT_PKGS alsa-utils"
command -v pactl >/dev/null 2>&1 || APT_PKGS="$APT_PKGS pulseaudio-utils pipewire-pulse"
command -v pw-record >/dev/null 2>&1 || APT_PKGS="$APT_PKGS pipewire"

# ── DO NOT RUN apt ON EVERY LAUNCH ───────────────────────────────────
# This block is gated on "is anything missing", which sounds
# self-limiting and is not: one probe that can never be satisfied makes
# it true for ever. On the device that is exactly what happened — a full
# `apt update` plus an install pass on EVERY start, measured at about
# EIGHTY SECONDS before the app appeared.
#
# That is bad on its own and disqualifying for restart-on-crash: a
# supervisor that restarts the app would leave the station blank for
# over a minute each time. A medication cabinet must come back in
# seconds.
#
# So the expensive pass is rate-limited by a stamp file. Still
# self-healing — it retries on the next launch after the window — but a
# restart minutes later is instant. DOSE_FORCE_DEPS=1 forces it.
DEPS_STAMP="$APP_DIR/.deps_checked"
DEPS_MAX_AGE_HOURS="${DOSE_DEPS_MAX_AGE_HOURS:-12}"
deps_check_is_fresh() {
    [ "${DOSE_FORCE_DEPS:-0}" = "1" ] && return 1
    [ -f "$DEPS_STAMP" ] || return 1
    local now stamp age
    now=$(date +%s)
    stamp=$(cat "$DEPS_STAMP" 2>/dev/null || echo 0)
    case "$stamp" in ''|*[!0-9]*) return 1 ;; esac
    age=$(( (now - stamp) / 3600 ))
    [ "$age" -lt "$DEPS_MAX_AGE_HOURS" ]
}

if [ -n "$APT_PKGS$PIP_PKGS" ] && deps_check_is_fresh; then
    echo "  Components checked less than ${DEPS_MAX_AGE_HOURS}h ago — skipping"
    echo "  the install pass so this start is fast."
    echo "  (force with DOSE_FORCE_DEPS=1)"
    APT_PKGS=""; PIP_PKGS=""
fi

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
    # Stamp it even if some package could not be installed. The point is
    # "we tried recently", not "everything succeeded" — otherwise one
    # permanently unavailable package means the slow pass runs for ever,
    # which is the bug this replaced.
    date +%s > "$DEPS_STAMP" 2>/dev/null || true
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
    # Never block forever: the station autostarts with Terminal=false,
    # so there is nobody to press a key. Wait briefly if a terminal is
    # attached, otherwise carry straight on.
    [ -t 0 ] && read -n 1 -s -t 30 || true
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
    # Never block forever: the station autostarts with Terminal=false,
    # so there is nobody to press a key. Wait briefly if a terminal is
    # attached, otherwise carry straight on.
    [ -t 0 ] && read -n 1 -s -t 30 || true
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
    # Silero VAD is fetched as a plain ONNX file below — NOT as the
    # pip package, which drags in torch and torchaudio.
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

# ── Update, every launch, WITHOUT ASKING ──
# This used to end in:  read -p "Would you like to update? (y/n)"
# The station autostarts from a desktop entry with Terminal=false, so
# there is no terminal to answer that prompt — the read got EOF and
# the update was skipped EVERY SINGLE TIME. The device sat on a build
# from days earlier while fix after fix was pushed to it. An
# interactive question on a touchscreen with no keyboard is not a
# safeguard, it is a wall.
#
# It also never fetched dose_nlu.py, so even a successful update left
# the language rules stale.
#
# Now: fetch everything, VALIDATE it, back up what is there, and only
# then replace. A broken download changes nothing.
echo "  Checking for updates..."
STAGE=$(mktemp -d)
UPDATE_OK=1
for F in dose_app.py dose_voice.py dose_nlu.py DOSE.sh; do
    if ! curl -fsSL "$RAW_URL/$F?nocache=$(date +%s)" -o "$STAGE/$F" 2>/dev/null; then
        echo "  Could not fetch $F — keeping what is installed."
        UPDATE_OK=0
        break
    fi
    # must be a real file, not an error page
    if [ "$(stat -c%s "$STAGE/$F" 2>/dev/null || echo 0)" -lt 500 ]; then
        echo "  $F download looks wrong — keeping what is installed."
        UPDATE_OK=0
        break
    fi
    case "$F" in
        *.py)
            if ! python3 -c "import sys,py_compile;py_compile.compile(sys.argv[1],doraise=True)" "$STAGE/$F" 2>/dev/null; then
                echo "  $F is not valid python — keeping what is installed."
                UPDATE_OK=0
                break
            fi
            ;;
    esac
done

if [ "$UPDATE_OK" = "1" ]; then
    CHANGED=""
    for F in dose_app.py dose_voice.py dose_nlu.py DOSE.sh; do
        A=$(md5sum "$STAGE/$F" 2>/dev/null | cut -d' ' -f1)
        B=$(md5sum "$APP_DIR/$F" 2>/dev/null | cut -d' ' -f1)
        [ "$A" != "$B" ] && CHANGED="$CHANGED $F"
    done
    if [ -n "$CHANGED" ]; then
        echo "  Updating:$CHANGED"
        mkdir -p "$APP_DIR/.backup"
        for F in dose_app.py dose_voice.py dose_nlu.py DOSE.sh; do
            [ -f "$APP_DIR/$F" ] && cp "$APP_DIR/$F" "$APP_DIR/.backup/$F" 2>/dev/null
            cp "$STAGE/$F" "$APP_DIR/$F"
        done
        curl -fsSL "$RAW_URL/dose_logo.png?nocache=$(date +%s)" -o "$APP_DIR/dose_logo.png" 2>/dev/null || true
        curl -fsSL "$RAW_URL/demo_qr.png?nocache=$(date +%s)" -o "$APP_DIR/demo_qr.png" 2>/dev/null || true
        chmod +x "$APP_DIR"/*.py "$APP_DIR"/*.sh 2>/dev/null || true
        echo "  Updated. Build $(md5sum "$APP_DIR/dose_app.py" | cut -c1-7)"
    else
        echo "  Already on the newest build ($(md5sum "$APP_DIR/dose_app.py" 2>/dev/null | cut -c1-7))."
    fi
fi
rm -rf "$STAGE"
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
# We need BOTH: the FAST model (tiny.en, the lead recogniser) and the
# ESCALATION model (base.en). The old script grabbed only one, so the
# other loaded mid-conversation the first time it was needed. Fetch
# both now, fast one first.
fast = os.environ.get("DOSE_FAST_WHISPER", "tiny.en")
esc = [m for m in os.environ.get(
    "DOSE_WHISPER_MODELS", "base.en,distil-small.en,tiny.en").split(",")
    if m]
wanted, seen = [], set()
for m in [fast] + esc:
    if m and m not in seen:
        seen.add(m); wanted.append(m)
got_fast, got_esc = False, False
for m in wanted:
    try:
        print("  Downloading the speech model (%s)..." % m)
        sys.stdout.flush()
        WhisperModel(m, device="cpu", compute_type="int8")
        print("  Speech model ready: %s" % m)
        if m == fast:
            got_fast = True
        else:
            got_esc = True
        # stop once we have the fast model and one escalation model
        if got_fast and got_esc:
            break
    except Exception as e:
        print("  %s unavailable (%s)" % (m, str(e)[:70]))
if not got_fast:
    print("  Fast model could not be downloaded — the app will keep "
          "retrying.")
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
# base, then tiny. NOT medium: the device measured moonshine medium at
# 4.11 s on a single word. This is the model that answers every
# sentence — Whisper is the accuracy path and escalates only when this
# one fails.
order = ([want] if want else []) + [
    a for a in ("BASE_STREAMING", "TINY_STREAMING") if a != want]
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

# ── Voice detector: one file, no package ──
# `pip install silero-vad` requires torch and torchaudio. On a 4 GB Pi
# that failed to install AND pushed the load average past 5 while it
# tried. All we need is the model itself; onnxruntime is already here.
VAD_MODEL="$VOICE_DIR/silero_vad.onnx"
if [ ! -s "$VAD_MODEL" ] || [ "$(stat -c%s "$VAD_MODEL" 2>/dev/null || echo 0)" -lt 500000 ]; then
    echo "  Downloading the voice detector (2 MB)..."
    if curl -sSL -o "$VAD_MODEL.part" \
        "https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx" \
        && [ "$(stat -c%s "$VAD_MODEL.part" 2>/dev/null || echo 0)" -gt 500000 ]; then
        mv "$VAD_MODEL.part" "$VAD_MODEL"
        echo "  Voice detector ready."
    else
        rm -f "$VAD_MODEL.part"
        echo "  (voice detector will be fetched by the app)"
    fi
fi

# ── Pi tuning: cool when idle, fast under load ──
# We deliberately do NOT pin "performance" — that holds all four cores
# at 1500 MHz every second of the day, idle or not, and that constant
# flat-out running is what was cooking the Pi (66-70 C at rest). It is
# not an overclock (the clock never exceeds stock) but it is pure heat
# for no benefit. "schedutil" / "ondemand" idle at 600 MHz and jump to
# full clock the moment there is work — in tens of milliseconds, far
# below one turn of conversation — so the Pi runs many degrees cooler
# with no felt lag. Reversible; lasts until reboot; no config edits.
echo "  Tuning for cool, responsive operation..."
GOV=ondemand
for CAND in schedutil ondemand conservative; do
    if grep -qw "$CAND" /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors 2>/dev/null; then
        GOV="$CAND"; break
    fi
done
for G in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
    [ -w "$G" ] && echo "$GOV" > "$G" 2>/dev/null || \
        echo "$GOV" 2>/dev/null | sudo tee "$G" >/dev/null 2>&1 || true
done
# Never leave a stray overclock in the firmware config. If a previous
# setup ever wrote one, comment it out so the SoC stays at stock clocks.
for CFG in /boot/firmware/config.txt /boot/config.txt; do
    [ -w "$CFG" ] || continue
    sed -i -E 's/^[[:space:]]*(arm_freq|over_voltage|force_turbo|gpu_freq|arm_freq_min)[[:space:]]*=/#&/' "$CFG" 2>/dev/null || true
done

# Keep the ONNX runtimes (speech + voice) to 3 of the 4 cores so the
# touchscreen UI always keeps one and stays at full frame rate.
export OMP_NUM_THREADS=${OMP_NUM_THREADS:-3}
export ORT_NUM_THREADS=${ORT_NUM_THREADS:-3}

# ── Audit reporting (optional) ──
# The AUDIT page can post its report straight to GitHub so it can be
# read without transcribing a photograph. That needs a token, and a
# token must never live in the repository — this one is public. Put a
# fine-grained personal access token with Issues:write into
# ~/dose-home-station/github_token and the button starts working; do
# nothing and the report is still written to
# ~/dose-home-station/audit-latest.txt.
# Pick a token up off a USB stick at launch, so it can be set once
# and forgotten. Typing 93 characters on a touchscreen is not a plan.
if [ ! -s "$APP_DIR/github_token" ]; then
    for D in /media/*/ /media/*/*/ /mnt/*/ /run/media/*/*/; do
        for N in github_token dose_github_token github_token.txt; do
            if [ -s "$D$N" ]; then
                cp "$D$N" "$APP_DIR/github_token" 2>/dev/null && {
                    chmod 600 "$APP_DIR/github_token"
                    echo "  GitHub token imported from $D"
                    break 2
                }
            fi
        done
    done
fi
if [ -f "$APP_DIR/github_token" ]; then
    chmod 600 "$APP_DIR/github_token" 2>/dev/null || true
fi

# ── Cloud STT credentials (free tiers) off a USB stick, once ──
# Same idea for the transcription providers: drop a file named
# 'groq_key' (Groq free tier) or 'hf_token' (Hugging Face free ZeroGPU)
# on a stick and the Pi hands STT to the cloud, keeping the local model
# only for offline. Neither ever goes in the repository.
for CRED in groq_key hf_token; do
    if [ ! -s "$APP_DIR/$CRED" ]; then
        for D in /media/*/ /media/*/*/ /mnt/*/ /run/media/*/*/; do
            if [ -s "$D$CRED" ]; then
                cp "$D$CRED" "$APP_DIR/$CRED" 2>/dev/null && {
                    chmod 600 "$APP_DIR/$CRED"
                    echo "  $CRED imported from $D"
                    break
                }
            fi
        done
    fi
    [ -f "$APP_DIR/$CRED" ] && chmod 600 "$APP_DIR/$CRED" 2>/dev/null || true
done

# ── EVERYTHING READY? ──
# A single gate before launch. The app used to open while pieces were
# still arriving, so the first screen you saw listed things as MISSING
# or "installing" and the first thing you said went to whatever
# happened to be loaded. Anything still missing here is retried ONCE,
# in the foreground, and then reported plainly — the app still starts
# either way, because a station that will not open is worse than one
# that is missing its best recogniser.
echo ""
echo "  Checking everything is in place..."
MISSING=""
for SPEC in "sounddevice:sounddevice" "vosk:vosk" "piper:piper-tts"             "rapidfuzz:rapidfuzz" "jellyfish:jellyfish"             "onnxruntime:onnxruntime" "numpy:numpy"             "faster_whisper:faster-whisper"             "moonshine_voice:moonshine-voice"; do
    MOD=${SPEC%%:*}; PKG=${SPEC##*:}
    if ! python3 -c "import $MOD" 2>/dev/null; then
        echo "    installing $PKG..."
        python3 -m pip install --break-system-packages "$PKG" 2>/dev/null             || python3 -m pip install "$PKG" 2>/dev/null || true
        python3 -c "import $MOD" 2>/dev/null || MISSING="$MISSING $PKG"
    fi
done
[ -s "$VOICE_DIR/silero_vad.onnx" ] || MISSING="$MISSING voice-detector"
ls "$VOICE_DIR"/*.onnx >/dev/null 2>&1 || MISSING="$MISSING voice-model"
ls -d "$VOICE_DIR"/vosk-model* >/dev/null 2>&1 || MISSING="$MISSING speech-model"

if [ -n "$MISSING" ]; then
    echo ""
    echo "  ┌──────────────────────────────────────────────────┐"
    echo "  │  Starting without:$(printf '%-31s' "$MISSING")│"
    echo "  │  DOSE will keep trying in the background.        │"
    echo "  │  Check Settings -> Update Software for details.  │"
    echo "  └──────────────────────────────────────────────────┘"
else
    echo "  Everything is in place."
fi

# ── AUDIO CLEANUP BEFORE LAUNCH ──────────────────────────────────────
# A crash does not tidy up after itself. When the app aborts — and it
# has, inside ONNX Runtime during TTS — its `arecord` child is orphaned
# onto init, still holding the ALSA capture device. The NEXT instance
# then finds the microphone busy and goes deaf, which is precisely the
# fault that took this station out twice.
#
# Found on the device: `arecord -D plughw:4,0` parented to PID 1, left
# by a previous instance, on the WRONG card at that.
#
# So every launch starts from a known-clean audio state: terminate any
# recorder or player left by a previous run, then — if the capture
# device is STILL busy, which means the kernel is holding a stranded
# PCM whose owner is gone — reset the USB device, the only thing that
# reliably clears it.
cleanup_audio() {
    local stale
    stale=$(pgrep -x arecord; pgrep -x aplay; pgrep -x pw-record) 2>/dev/null
    if [ -n "$stale" ]; then
        echo "  Clearing $(echo "$stale" | grep -c .) leftover audio process(es)"
        # TERM, never KILL: a killed recorder does not release the
        # device, which is the whole problem being cleaned up here.
        echo "$stale" | xargs -r kill -TERM 2>/dev/null
        sleep 2
        echo "$stale" | xargs -r kill -KILL 2>/dev/null
        sleep 1
    fi

    # Is the microphone actually openable?
    local card="${DOSE_MIC_CARD%%[,:]*}"
    [ -n "$card" ] || return 0
    if arecord -D "plughw:${card},0" -f S16_LE -r 48000 -c 2 -d 1 \
            /dev/null >/dev/null 2>&1; then
        return 0
    fi
    echo "  Microphone still busy — resetting the USB device"
    local devpath
    devpath=$(python3 - <<'PY' 2>/dev/null
import re, subprocess
try:
    out = subprocess.run(["lsusb"], capture_output=True, text=True,
                         timeout=5).stdout
except Exception:
    raise SystemExit
for line in out.splitlines():
    if "airhug" in line.lower():
        m = re.match(r"Bus (\d+) Device (\d+)", line)
        if m:
            print("/dev/bus/usb/%s/%s" % (m.group(1), m.group(2)))
            break
PY
)
    [ -n "$devpath" ] || return 0
    sudo -n python3 - "$devpath" <<'PY' 2>/dev/null || true
import fcntl, os, sys
USBDEVFS_RESET = ord('U') << 8 | 20
try:
    fd = os.open(sys.argv[1], os.O_WRONLY)
    fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    os.close(fd)
    print("    USB reset sent")
except Exception as e:
    print("    reset unavailable: %s" % e)
PY
    sleep 3
}
cleanup_audio

# ── Launch ──
echo "  Starting DOSE..."
echo "  Press Esc to exit."
echo ""
export DISPLAY=:0

# ── WHERE THE LOGS GO ────────────────────────────────────────────────
# This used to be:  python3 dose_app.py 2>"$APP_DIR/error.log"
#
# Three things were wrong with it, and together they meant this station
# has never kept a usable record of anything it did.
#
#   1. `2>` TRUNCATES. Every restart destroyed the log of the run that
#      caused the restart. The one thing you always want after a crash
#      is the last thing the crashed process said, and it was deleted
#      by the process that replaced it. `>>` appends.
#
#   2. Only stderr was kept. Everything the app printed deliberately
#      went to stdout, which under systemd goes to StandardOutput and
#      under a terminal went to the screen and then nowhere.
#
#   3. The file was not there at all. A live check on 2026-09-18 found
#      no error.log, no logs/dose.log, and an empty journal, while the
#      app had been running for seven minutes. Every traceback this
#      station has ever produced has gone to a file nobody could find.
#
# Both streams now append to one timestamped log, and the log is
# rotated by size so it cannot fill the SD card. tee keeps stdout
# flowing to systemd as well, so `journalctl --user -u dose-home-station`
# still works and the two views agree.
LOG_DIR="$APP_DIR/logs"
mkdir -p "$LOG_DIR" 2>/dev/null
APP_LOG="$LOG_DIR/dose.log"
# Rotate at 8 MB, keep one previous. Cheap, and bounded forever.
if [ -f "$APP_LOG" ] && [ "$(wc -c < "$APP_LOG" 2>/dev/null || echo 0)" -gt 8388608 ]; then
    mv -f "$APP_LOG" "$APP_LOG.1" 2>/dev/null || true
fi
{
    echo ""
    echo "=== DOSE start $(date '+%Y-%m-%d %H:%M:%S') pid=$$ ==="
} >> "$APP_LOG" 2>/dev/null

python3 -u "$APP_DIR/dose_app.py" 2>&1 | tee -a "$APP_LOG"
EXIT_CODE=${PIPESTATUS[0]}
if [ $EXIT_CODE -ne 0 ]; then
    echo ""
    echo "  DOSE crashed (exit $EXIT_CODE). Last 40 lines:"
    echo ""
    tail -40 "$APP_LOG" 2>/dev/null || echo "  (no log)"
    echo ""
    echo "  Full log: $APP_LOG"
    echo ""
    echo "  Press any key to close..."
    # Never block forever: the station autostarts with Terminal=false,
    # so there is nobody to press a key. Wait briefly if a terminal is
    # attached, otherwise carry straight on.
    [ -t 0 ] && read -n 1 -s -t 30 || true
fi
