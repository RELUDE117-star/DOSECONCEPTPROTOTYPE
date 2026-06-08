#!/bin/bash
# DOSE Home Station — Installer
# Double-click this file → pick "Execute in Terminal"
# Everything else is automatic.

# Keep the terminal open if ANYTHING goes wrong
trap 'echo ""; echo "Something went wrong. See error above."; echo "Press any key to close..."; read -n 1 -s' ERR

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$HOME/dose-home-station"

clear
echo "========================================="
echo "  DOSE Home Station — Installer"
echo "========================================="
echo ""
echo "  This will install everything you need."
echo "  You may be asked for your password once."
echo "  (You won't see it as you type — that's normal)"
echo ""
sleep 1

# ---- system packages ----
echo "[1/4] Installing system packages..."
echo "  (this may take a few minutes)"
echo ""
sudo apt update -y
sudo apt install -y \
    python3-tk \
    python3-pil \
    python3-pil.imagetk \
    libzbar0 \
    python3-picamera2 \
    python3-pip \
    git

# ---- Python packages ----
echo ""
echo "[2/4] Installing Python packages..."
pip install --break-system-packages pyzbar Pillow 'qrcode[pil]' 2>/dev/null || \
pip install pyzbar Pillow 'qrcode[pil]' 2>/dev/null || true

# ---- copy app files ----
echo ""
echo "[3/4] Installing DOSE app..."
mkdir -p "$INSTALL_DIR"
if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
    cp -r "$SCRIPT_DIR"/* "$INSTALL_DIR"/
    cp "$SCRIPT_DIR"/.gitignore "$INSTALL_DIR"/ 2>/dev/null || true
fi
chmod +x "$INSTALL_DIR"/*.py 2>/dev/null || true
chmod +x "$INSTALL_DIR"/*.sh 2>/dev/null || true

# Set up git for future auto-updates (pulling public repos needs no login)
cd "$INSTALL_DIR"
if [ ! -d .git ]; then
    git init -q 2>/dev/null || true
    git remote add origin https://github.com/relude117-star/doseconceptprototype.git 2>/dev/null || true
fi

# ---- create desktop shortcut ----
echo ""
echo "[4/4] Creating shortcuts..."

# Create app icon
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
" 2>/dev/null || true

# Desktop shortcut
DESKTOP_DIR="$HOME/Desktop"
mkdir -p "$DESKTOP_DIR"
DESKTOP_FILE="$DESKTOP_DIR/DOSE.desktop"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=DOSE Home Station
Comment=Launch DOSE medication dispenser
Exec=/bin/bash $INSTALL_DIR/launch.sh
Icon=$INSTALL_DIR/dose_icon.png
Terminal=false
Categories=Utility;
StartupNotify=false
EOF
chmod +x "$DESKTOP_FILE"
# Mark as trusted so Pi treats it as an app, not a text file
gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true
dbus-launch gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true

# Autostart on boot
AUTOSTART_DIR="$HOME/.config/autostart"
mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/dose-home-station.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DOSE Home Station
Exec=/bin/bash $INSTALL_DIR/launch.sh
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

# ---- verify everything works ----
echo ""
echo "  Checking dependencies..."
python3 -c "
import tkinter as tk
from PIL import Image, ImageTk
from pyzbar.pyzbar import decode
print('  All good!')
"

echo ""
echo "========================================="
echo "  INSTALLATION COMPLETE!"
echo "========================================="
echo ""
echo "  - DOSE icon added to your desktop"
echo "  - App will auto-start when Pi boots"
echo "  - Updates download automatically"
echo ""
echo "  Starting DOSE now..."
sleep 2

# Launch the app
export DISPLAY=:0
python3 "$INSTALL_DIR/dose_demo.py" &

echo ""
echo "  DOSE is running!"
echo "  Press Esc on a keyboard to exit the app."
echo ""
echo "  You can close this terminal window now."
echo "  Press any key to close..."
read -n 1 -s
