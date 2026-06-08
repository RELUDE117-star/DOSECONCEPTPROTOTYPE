#!/bin/bash
# DOSE Home Station — Launcher with auto-update
# This script is called by the desktop shortcut and autostart.
export DISPLAY=:0
INSTALL_DIR="$HOME/dose-home-station"

# Make sure the install folder exists
if [ ! -d "$INSTALL_DIR" ]; then
    lxterminal -e bash -c "echo 'ERROR: DOSE is not installed yet. Run the installer first.'; echo ''; echo 'Press any key to close...'; read -n 1 -s"
    exit 1
fi

cd "$INSTALL_DIR"

# Auto-update: pull latest code from GitHub (no login needed for public repos)
if [ -d .git ]; then
    git pull origin main --ff-only 2>/dev/null || \
    git pull origin claude/quirky-brown-vkHwi --ff-only 2>/dev/null || \
    true
fi

# Launch the app — if it crashes, show the error so the user can report it
python3 "$INSTALL_DIR/dose_demo.py" 2>/tmp/dose_error.log
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    ERROR=$(cat /tmp/dose_error.log)
    lxterminal -e bash -c "echo '========================================'; echo '  DOSE failed to start'; echo '========================================'; echo ''; cat /tmp/dose_error.log; echo ''; echo 'Press any key to close...'; read -n 1 -s"
fi
