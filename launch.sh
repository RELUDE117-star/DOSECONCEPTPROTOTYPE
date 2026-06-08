#!/bin/bash
# DOSE Home Station — Launcher with auto-update
# This script is called by the desktop shortcut and autostart.
# It pulls the latest code from GitHub before launching the app.
INSTALL_DIR="$HOME/dose-home-station"
cd "$INSTALL_DIR"

# Auto-update: pull latest code from GitHub (silent, no login needed)
if [ -d .git ]; then
    git pull origin main --ff-only 2>/dev/null || \
    git pull origin claude/quirky-brown-vkHwi --ff-only 2>/dev/null || \
    true
fi

# Launch the app
exec python3 "$INSTALL_DIR/dose_demo.py"
