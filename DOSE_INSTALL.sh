#!/bin/bash
# ══════════════════════════════════════════════════════════════
#  DOSE Home Station
#  Right-click this file → "Execute in Terminal"
#  Or open Terminal and type:  bash DOSE_INSTALL.sh
# ══════════════════════════════════════════════════════════════

trap 'echo ""; echo "Something went wrong. See error above."; echo "Press any key to close..."; read -n 1 -s; exit 1' ERR

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$HOME/dose-home-station"

clear
echo "========================================="
echo "  DOSE Home Station"
echo "========================================="
echo ""

# ── First-time: install packages ──
if [ ! -f "$INSTALL_DIR/.installed" ]; then
    echo "  First-time setup (this takes a few minutes)."
    echo "  You'll be asked for your password once."
    echo ""
    sudo apt update -y
    sudo apt install -y python3-tk python3-pil python3-pil.imagetk libzbar0 python3-pip 2>&1
    pip install --break-system-packages pyzbar Pillow 2>/dev/null || pip install pyzbar Pillow 2>/dev/null || true
    # picamera2 is optional — app works without it
    sudo apt install -y python3-picamera2 2>/dev/null || true
    mkdir -p "$INSTALL_DIR"
    touch "$INSTALL_DIR/.installed"
    echo ""
    echo "  Packages installed!"
fi

# ── Copy app files ──
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/dose_app.py" "$INSTALL_DIR/dose_app.py" 2>/dev/null || true

# ── Create desktop shortcut ──
DESKTOP_DIR="$HOME/Desktop"
mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_DIR/DOSE.desktop" << EOF
[Desktop Entry]
Type=Application
Name=DOSE Home Station
Exec=/bin/bash -c 'export DISPLAY=:0; python3 $INSTALL_DIR/dose_app.py'
Terminal=false
StartupNotify=false
EOF
chmod +x "$DESKTOP_DIR/DOSE.desktop"
gio set "$DESKTOP_DIR/DOSE.desktop" metadata::trusted true 2>/dev/null || true
dbus-launch gio set "$DESKTOP_DIR/DOSE.desktop" metadata::trusted true 2>/dev/null || true

# ── Autostart on boot ──
mkdir -p "$HOME/.config/autostart"
cat > "$HOME/.config/autostart/dose.desktop" << EOF
[Desktop Entry]
Type=Application
Name=DOSE Home Station
Exec=/bin/bash -c 'export DISPLAY=:0; python3 $INSTALL_DIR/dose_app.py'
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

echo ""
echo "  Starting DOSE..."
echo "  Press Esc to exit the app."
sleep 1

export DISPLAY=:0
python3 "$INSTALL_DIR/dose_app.py"
