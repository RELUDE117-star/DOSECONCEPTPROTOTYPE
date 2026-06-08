# DOSE Home Station Prototype

A full-screen kiosk application for the DOSE medication dispenser prototype,
built for Raspberry Pi.

## Hardware

| Component | Model |
|-----------|-------|
| Computer | Raspberry Pi 4B |
| Camera | Raspberry Pi Camera Module 3 Wide (120° FOV, autofocus) |
| Display | Elecrow 5" 800×480 capacitive touchscreen |

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/relude117-star/doseconceptprototype.git
cd doseconceptprototype
chmod +x install.sh
./install.sh
```

The installer handles all system packages, Python dependencies, a desktop
shortcut, and kiosk autostart on boot.

### 2. Connect hardware

- Attach the Camera Module 3 Wide via CSI ribbon cable
- Connect the Elecrow display via HDMI + USB (for touch)
- Enable the camera in `raspi-config` → Interface Options → Camera

### 3. Run

```bash
python3 dose_demo.py
```

The app launches full-screen. Controls:

| Key | Action |
|-----|--------|
| `Esc` | Quit |
| `c` | Toggle camera preview (for aiming) |
| Tap screen | Dismiss result → idle |

### 4. Generate QR codes

```bash
python3 generate_qr_codes.py
```

Creates individual PNGs and a printable 2×2 sheet in `qr_codes/`.
Print them, cut them out, and hold them in front of the camera to test.

The demo uses four medication codes: **blue**, **red**, **green**, **yellow**.

## How It Works

1. The idle screen shows a clock and the next scheduled dose
2. The camera continuously scans for QR codes in the background
3. When a medication QR code is detected, the screen shows:
   - Medication name
   - Instructions (what to take it with)
   - Which dispenser button to press
4. After the QR code leaves the camera view for 6 seconds, the screen
   returns to idle

## Auto-Start on Boot

The installer sets up autostart automatically. To disable it:

```bash
rm ~/.config/autostart/dose-home-station.desktop
```

To re-enable, run `install.sh` again.

## Uninstall

```bash
chmod +x uninstall.sh
./uninstall.sh
```

Removes shortcuts and autostart. System packages are left in place.

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
dose_demo.py          — Main application
generate_qr_codes.py  — QR code generator utility
install.sh            — One-step Raspberry Pi installer
uninstall.sh          — Remove shortcuts and autostart
requirements.txt      — Python dependencies
```
