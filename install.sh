#!/bin/bash
# DOSE Home Station — One-Click Installer
# Just double-click this file from the file manager.
# If it asks "Execute" or "Execute in Terminal" → pick "Execute in Terminal".
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# If we're not inside a terminal, relaunch ourselves inside one
if [ ! -t 1 ]; then
    if command -v lxterminal &>/dev/null; then
        exec lxterminal -e "bash \"$0\""
    elif command -v xterm &>/dev/null; then
        exec xterm -e "bash \"$0\""
    fi
fi

clear
echo "========================================="
echo "  DOSE Home Station — Installer"
echo "========================================="
echo ""
echo "This will install everything automatically."
echo "You may be asked for your password once."
echo ""
sleep 1

# ---- system packages ----
echo "[1/5] Installing system packages..."
sudo apt update -y
sudo apt install -y \
    python3-tk \
    python3-pil \
    python3-pil.imagetk \
    libzbar0 \
    python3-picamera2 \
    git

# ---- Python packages ----
echo ""
echo "[2/5] Installing Python packages..."
pip install --break-system-packages -r "$SCRIPT_DIR/requirements.txt" 2>/dev/null || \
pip install -r "$SCRIPT_DIR/requirements.txt"

# ---- make scripts executable ----
echo ""
echo "[3/5] Setting permissions..."
chmod +x "$SCRIPT_DIR/dose_demo.py"
chmod +x "$SCRIPT_DIR/generate_qr_codes.py"
chmod +x "$SCRIPT_DIR/launch.sh"
chmod +x "$SCRIPT_DIR/uninstall.sh"

# ---- install to a fixed location so updates work ----
echo ""
echo "[4/5] Installing to home folder..."
INSTALL_DIR="$HOME/dose-home-station"
if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
    mkdir -p "$INSTALL_DIR"
    cp -r "$SCRIPT_DIR"/* "$INSTALL_DIR"/
    cp -r "$SCRIPT_DIR"/.git "$INSTALL_DIR"/ 2>/dev/null || true
    cp "$SCRIPT_DIR"/.gitignore "$INSTALL_DIR"/ 2>/dev/null || true
fi

# ---- set the remote URL to HTTPS (no login needed for pull) ----
cd "$INSTALL_DIR"
if [ -d .git ]; then
    git remote set-url origin https://github.com/relude117-star/doseconceptprototype.git 2>/dev/null || true
fi

# ---- create desktop shortcut (double-click to run) ----
echo ""
echo "[5/5] Creating shortcuts..."
DESKTOP_DIR="$HOME/Desktop"
mkdir -p "$DESKTOP_DIR"

cat > "$DESKTOP_DIR/DOSE.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DOSE Home Station
Comment=Launch DOSE medication dispenser
Exec=bash $INSTALL_DIR/launch.sh
Icon=utilities-terminal
Terminal=false
Categories=Utility;
EOF
chmod +x "$DESKTOP_DIR/DOSE.desktop"

# ---- autostart on boot ----
AUTOSTART_DIR="$HOME/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/dose-home-station.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DOSE Home Station
Exec=bash $INSTALL_DIR/launch.sh
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

echo ""
echo "========================================="
echo "  DONE! Installation complete!"
echo "========================================="
echo ""
echo "  You now have a 'DOSE' icon on your desktop."
echo "  Double-click it to launch the app."
echo ""
echo "  The app also starts automatically when"
echo "  you turn on your Raspberry Pi."
echo ""
echo "  Updates from GitHub are pulled automatically"
echo "  every time the app launches."
echo ""
echo "  Press any key to close this window..."
read -n 1 -s
