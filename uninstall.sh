#!/bin/bash
# DOSE Home Station — Uninstaller
# Double-click to run, or run from terminal.
set -euo pipefail

if [ ! -t 1 ]; then
    if command -v lxterminal &>/dev/null; then
        exec lxterminal -e "bash \"$0\""
    elif command -v xterm &>/dev/null; then
        exec xterm -e "bash \"$0\""
    fi
fi

echo "Removing DOSE Home Station..."
rm -f "$HOME/Desktop/DOSE.desktop"
rm -f "$HOME/.config/autostart/dose-home-station.desktop"
rm -rf "$HOME/dose-home-station"
echo ""
echo "Done! DOSE has been removed."
echo "Press any key to close..."
read -n 1 -s
