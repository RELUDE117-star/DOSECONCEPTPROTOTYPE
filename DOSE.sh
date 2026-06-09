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
    sudo apt install -y python3-tk python3-pil python3-pil.imagetk libzbar0 python3-pip fonts-nunito curl 2>/dev/null || true
    sudo apt install -y python3-picamera2 2>/dev/null || true
    pip install --break-system-packages pyzbar Pillow 2>/dev/null || pip install pyzbar Pillow 2>/dev/null || true
fi

# ── Copy app files ──
mkdir -p "$APP_DIR"
cp "$SCRIPT_DIR/dose_app.py" "$APP_DIR/dose_app.py" 2>/dev/null || true
cp "$SCRIPT_DIR/DOSE.sh" "$APP_DIR/DOSE.sh" 2>/dev/null || true
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
            # Also update DOSE.sh
            curl -sL "$RAW_URL/DOSE.sh" -o "$APP_DIR/DOSE.sh" 2>/dev/null || true
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
python3 "$APP_DIR/dose_app.py"
