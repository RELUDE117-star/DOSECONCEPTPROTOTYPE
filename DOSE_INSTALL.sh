#!/bin/bash
# ══════════════════════════════════════════════════════════════
#  DOSE Home Station
#  Right-click this file → "Execute in Terminal"
# ══════════════════════════════════════════════════════════════

trap 'echo ""; echo "Something went wrong. See error above."; echo "Press any key to close..."; read -n 1 -s; exit 1' ERR

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="$HOME/dose-home-station"
REPO_URL="https://github.com/relude117-star/doseconceptprototype.git"
BRANCH="claude/quirky-brown-vkHwi"

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
    sudo apt install -y \
        python3-tk python3-pil python3-pil.imagetk \
        libzbar0 python3-pip git \
        fonts-nunito 2>/dev/null || true
    # picamera2 is optional
    sudo apt install -y python3-picamera2 2>/dev/null || true
    pip install --break-system-packages pyzbar Pillow 2>/dev/null || \
    pip install pyzbar Pillow 2>/dev/null || true
    echo ""
    echo "  Packages installed!"
fi

# ── Copy app files to install location ──
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/dose_app.py" "$INSTALL_DIR/dose_app.py" 2>/dev/null || true
cp "$SCRIPT_DIR/DOSE_INSTALL.sh" "$INSTALL_DIR/DOSE_INSTALL.sh" 2>/dev/null || true
chmod +x "$INSTALL_DIR"/*.py "$INSTALL_DIR"/*.sh 2>/dev/null || true
touch "$INSTALL_DIR/.installed"

# ── Set up git for auto-updates ──
cd "$INSTALL_DIR"
if [ ! -d .git ]; then
    git init -q 2>/dev/null || true
    git remote add origin "$REPO_URL" 2>/dev/null || true
    git fetch origin "$BRANCH" --depth=1 2>/dev/null || true
fi

# ── Create launch script (with auto-update) ──
cat > "$INSTALL_DIR/launch.sh" << 'LAUNCHER'
#!/bin/bash
export DISPLAY=:0
INSTALL_DIR="$HOME/dose-home-station"
cd "$INSTALL_DIR"

# Auto-update from GitHub (no login needed for public repos)
if [ -d .git ]; then
    git fetch origin --depth=1 2>/dev/null && \
    git reset --hard origin/claude/quirky-brown-vkHwi 2>/dev/null || true
fi

python3 "$INSTALL_DIR/dose_app.py" 2>/tmp/dose_error.log
if [ $? -ne 0 ]; then
    lxterminal -e bash -c "echo 'DOSE error:'; cat /tmp/dose_error.log; echo ''; echo 'Press any key...'; read -n 1 -s" 2>/dev/null &
fi
LAUNCHER
chmod +x "$INSTALL_DIR/launch.sh"

# ── Desktop shortcut ──
DESKTOP_DIR="$HOME/Desktop"
mkdir -p "$DESKTOP_DIR"
cat > "$DESKTOP_DIR/DOSE.desktop" << EOF
[Desktop Entry]
Type=Application
Name=DOSE Home Station
Exec=/bin/bash $INSTALL_DIR/launch.sh
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
Exec=/bin/bash $INSTALL_DIR/launch.sh
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

echo ""
echo "  Starting DOSE..."
echo "  Press Esc to exit the app."
sleep 1

export DISPLAY=:0
python3 "$INSTALL_DIR/dose_app.py"
