# DOSE Home Station Prototype

A full-screen kiosk application for the DOSE medication dispenser prototype,
built for Raspberry Pi.

## Hardware

| Component | Model |
|-----------|-------|
| Computer | Raspberry Pi 4B |
| Camera | Raspberry Pi Camera Module 3 Wide (120° FOV, autofocus) |
| Display | Elecrow 5" 800×480 capacitive touchscreen |

## Setup (One-Click Install)

1. Download the ZIP from GitHub: click the green **Code** button → **Download ZIP**
2. Unzip the folder
3. Open the folder and **double-click `install.sh`**
4. If asked, choose **"Execute in Terminal"**
5. Enter your Raspberry Pi password when asked
6. Done! You now have a **DOSE** icon on your desktop

## Running the App

- **Double-click the DOSE icon** on your desktop
- The app also **starts automatically** when you turn on the Pi
- Press **Esc** on a keyboard to exit

## Auto-Updates

Every time the app launches, it automatically pulls the latest version
from GitHub. Just push updates to this repo from your laptop and the
Raspberry Pi will pick them up on next launch.

## Controls

| Key | Action |
|-----|--------|
| `Esc` | Quit |
| `c` | Toggle camera preview (for aiming) |
| Tap screen | Dismiss result → idle |

## QR Codes for Testing

Double-click `generate_qr_codes.py` or run from terminal:

```bash
python3 generate_qr_codes.py
```

Creates printable QR codes in `qr_codes/`. The demo uses four codes:
**blue**, **red**, **green**, **yellow**.

## Uninstall

Double-click `uninstall.sh` to remove everything.

## Display Setup

The Elecrow 5" touchscreen should work out of the box on Raspberry Pi OS
Bookworm. If touch input isn't working, add to `/boot/config.txt`:

```
hdmi_group=2
hdmi_mode=87
hdmi_cvt 800 480 60 6 0 0 0
```

## Project Structure

```
install.sh               ← Double-click to install (one time)
launch.sh                ← Auto-updater + launcher (used by shortcuts)
dose_demo.py             ← Main application
generate_qr_codes.py     ← QR code generator utility
uninstall.sh             ← Double-click to remove
requirements.txt         ← Python dependencies
```
