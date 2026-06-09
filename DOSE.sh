#!/bin/bash
# ══════════════════════════════════════════════════════════════
#  DOSE Home Station
#  Double-click → "Execute in Terminal" → app launches
#  First run installs what's needed. Every run checks for updates.
# ══════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$HOME/dose-home-station"
REPO="https://github.com/relude117-star/doseconceptprototype.git"
BRANCH="claude/quirky-brown-vkHwi"

# Keep terminal open on errors
trap 'echo ""; echo "Something went wrong (see above)."; echo "Press any key to close..."; read -n 1 -s; exit 1' ERR

clear
echo "  DOSE Home Station"
echo ""

# ── Install dependencies if first run ──
if [ ! -f "$INSTALL_DIR/.ready" ]; then
    echo "  Setting up for the first time..."
    echo "  You may be asked for your password."
    echo ""
    sudo apt update -y
    sudo apt install -y python3-tk python3-pil python3-pil.imagetk libzbar0 python3-pip git fonts-nunito 2>/dev/null || true
    sudo apt install -y python3-picamera2 2>/dev/null || true
    pip install --break-system-packages pyzbar Pillow 2>/dev/null || pip install pyzbar Pillow 2>/dev/null || true
fi

# ── Copy app to a fixed location ──
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/dose_app.py" "$INSTALL_DIR/dose_app.py" 2>/dev/null || true
cp "$SCRIPT_DIR/DOSE.sh" "$INSTALL_DIR/DOSE.sh" 2>/dev/null || true
chmod +x "$INSTALL_DIR"/*.py "$INSTALL_DIR"/*.sh 2>/dev/null || true
touch "$INSTALL_DIR/.ready"

# ── Set up git for auto-updates (one time) ──
cd "$INSTALL_DIR"
if [ ! -d .git ]; then
    git init -q 2>/dev/null || true
    git remote add origin "$REPO" 2>/dev/null || true
fi

# ── Pull latest from GitHub (no login needed) ──
echo "  Checking for updates..."
git fetch origin "$BRANCH" --depth=1 2>/dev/null && \
git checkout FETCH_HEAD -- dose_app.py DOSE.sh 2>/dev/null || true
echo ""

# ── Create desktop shortcut (so next time they just click it) ──
DESKTOP_DIR="$HOME/Desktop"
mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_DIR/DOSE.desktop" << EOF
[Desktop Entry]
Type=Application
Name=DOSE Home Station
Exec=/bin/bash $INSTALL_DIR/DOSE.sh
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
Exec=/bin/bash $INSTALL_DIR/DOSE.sh
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

# ── Launch the app ──
echo "  Starting DOSE..."
echo "  Press Esc to exit."
echo ""
export DISPLAY=:0
python3 "$INSTALL_DIR/dose_app.py"
