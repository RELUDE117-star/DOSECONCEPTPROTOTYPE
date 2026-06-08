#!/bin/bash
# DOSE Home Station — uninstaller
# Removes autostart entry and desktop shortcut. Does NOT remove system packages.
set -euo pipefail

echo "Removing DOSE Home Station shortcuts..."

rm -f "$HOME/Desktop/dose-home-station.desktop"
rm -f "$HOME/.config/autostart/dose-home-station.desktop"

echo "Done. System packages were left in place."
