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
Icon=$INSTALL_DIR/dose_icon.png
Terminal=false
Categories=Utility;
StartupNotify=false
EOF
chmod +x "$DESKTOP_DIR/DOSE.desktop"

# Mark the shortcut as trusted so the Pi treats it as an app, not a text file
gio set "$DESKTOP_DIR/DOSE.desktop" metadata::trusted true 2>/dev/null || true
# Also handle older Raspberry Pi OS versions
dbus-launch gio set "$DESKTOP_DIR/DOSE.desktop" metadata::trusted true 2>/dev/null || true

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

# ---- create app icon ----
python3 -c "
from PIL import Image, ImageDraw, ImageFont
size = 128
img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)
draw.rounded_rectangle([4, 4, size-4, size-4], radius=24, fill='#5B9BFF')
try:
    font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', 72)
except: font = ImageFont.load_default()
bbox = draw.textbbox((0, 0), 'D', font=font)
tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
draw.text(((size-tw)//2, (size-th)//2 - 8), 'D', fill='white', font=font)
img.save('$INSTALL_DIR/dose_icon.png')
print('  Icon created.')
" 2>/dev/null || true

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
