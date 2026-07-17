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

# ── First run: install dependencies ──
if [ ! -f "$APP_DIR/.ready" ]; then
    echo "  First-time setup (takes a few minutes)..."
    echo "  You may be asked for your password."
    echo ""
    sudo apt update -y
    sudo apt install -y python3-tk python3-pil python3-pil.imagetk libzbar0 python3-pip fonts-inter fonts-nunito curl python3-smbus i2c-tools 2>/dev/null || true
    sudo apt install -y python3-picamera2 2>/dev/null || true
    pip install --break-system-packages pyzbar Pillow adafruit-circuitpython-mpr121 "qrcode[pil]" 2>/dev/null \
        || pip install pyzbar Pillow adafruit-circuitpython-mpr121 "qrcode[pil]" 2>/dev/null || true
fi

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

# ── Copy app files ──
mkdir -p "$APP_DIR"
cp "$SCRIPT_DIR/dose_app.py" "$APP_DIR/dose_app.py" 2>/dev/null || true
cp "$SCRIPT_DIR/DOSE.sh" "$APP_DIR/DOSE.sh" 2>/dev/null || true
cp "$SCRIPT_DIR/dose_logo.png" "$APP_DIR/dose_logo.png" 2>/dev/null || true
cp "$SCRIPT_DIR/demo_qr.png" "$APP_DIR/demo_qr.png" 2>/dev/null || true
chmod +x "$APP_DIR"/*.py "$APP_DIR"/*.sh 2>/dev/null || true
touch "$APP_DIR/.ready"

# ── Check for updates ──
echo "  Checking for updates..."
TEMP_FILE=$(mktemp)
if curl -sL "$RAW_URL/dose_app.py" -o "$TEMP_FILE" 2>/dev/null; then
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
            # Also update DOSE.sh and logo
            curl -sL "$RAW_URL/DOSE.sh" -o "$APP_DIR/DOSE.sh" 2>/dev/null || true
            curl -sL "$RAW_URL/dose_logo.png" -o "$APP_DIR/dose_logo.png" 2>/dev/null || true
            curl -sL "$RAW_URL/demo_qr.png" -o "$APP_DIR/demo_qr.png" 2>/dev/null || true
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
