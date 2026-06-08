#!/bin/bash
# DOSE Home Station — Raspberry Pi installer
# Run on Raspberry Pi OS Bookworm (64-bit recommended):
#   chmod +x install.sh && ./install.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "========================================="
echo "  DOSE Home Station — Installer"
echo "========================================="
echo ""

# ---- system packages ----
echo "[1/4] Installing system packages..."
sudo apt update
sudo apt install -y \
    python3-tk \
    python3-pil \
    python3-pil.imagetk \
    libzbar0 \
    python3-picamera2 \
    git

# ---- Python packages (user-level, no venv needed) ----
echo ""
echo "[2/4] Installing Python packages..."
pip install --break-system-packages -r "$SCRIPT_DIR/requirements.txt"

# ---- desktop shortcut for manual launch ----
echo ""
echo "[3/4] Creating desktop shortcut..."
DESKTOP_DIR="$HOME/Desktop"
mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_DIR/dose-home-station.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DOSE Home Station
Comment=DOSE medication dispenser kiosk
Exec=python3 $SCRIPT_DIR/dose_demo.py
Icon=utilities-terminal
Terminal=false
Categories=Utility;
EOF
chmod +x "$DESKTOP_DIR/dose-home-station.desktop"

# ---- optional kiosk autostart ----
echo ""
echo "[4/4] Setting up kiosk autostart..."
AUTOSTART_DIR="$HOME/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/dose-home-station.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DOSE Home Station
Comment=DOSE medication dispenser kiosk (auto-start)
Exec=python3 $SCRIPT_DIR/dose_demo.py
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

echo ""
echo "========================================="
echo "  Installation complete!"
echo "========================================="
echo ""
echo "  To run now:        python3 $SCRIPT_DIR/dose_demo.py"
echo "  Desktop shortcut:  ~/Desktop/dose-home-station.desktop"
echo "  Auto-start:        Enabled (reboot to test)"
echo ""
echo "  Controls:"
echo "    Esc   — quit"
echo "    c     — toggle camera preview"
echo "    tap   — dismiss result"
echo ""
echo "  To generate QR codes for testing:"
echo "    python3 $SCRIPT_DIR/generate_qr_codes.py"
echo ""
