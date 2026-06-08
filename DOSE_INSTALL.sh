#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  DOSE Home Station — Single-File Installer & App
#  Double-click this file → pick "Execute in Terminal" → DONE.
# ═══════════════════════════════════════════════════════════════════

# Keep terminal open on any error
trap 'echo ""; echo "ERROR: Something went wrong (see above)."; echo "Press any key to close..."; read -n 1 -s; exit 1' ERR

INSTALL_DIR="$HOME/dose-home-station"
DESKTOP_FILE="$HOME/Desktop/DOSE.desktop"
AUTOSTART_FILE="$HOME/.config/autostart/dose-home-station.desktop"

clear
echo "========================================="
echo "  DOSE Home Station"
echo "========================================="
echo ""

# ── Check if already installed ──
if [ -f "$INSTALL_DIR/dose_app.py" ]; then
    echo "  DOSE is already installed."
    echo "  Checking for updates..."
    # Re-extract the app in case this is a newer version
    extract_app=true
else
    echo "  First-time install. This takes a few minutes."
    echo "  You may be asked for your password once."
    echo "  (You won't see it as you type — that's normal)"
    echo ""
    sleep 1

    # ── Install system packages ──
    echo "[1/3] Installing system packages..."
    sudo apt update -y
    sudo apt install -y \
        python3-tk \
        python3-pil \
        python3-pil.imagetk \
        libzbar0 \
        python3-picamera2 \
        python3-pip \
        git

    echo ""
    echo "[2/3] Installing Python packages..."
    pip install --break-system-packages pyzbar Pillow 'qrcode[pil]' 2>/dev/null || \
    pip install pyzbar Pillow 'qrcode[pil]' 2>/dev/null || true

    extract_app=true

    # ── Create desktop shortcut ──
    echo ""
    echo "[3/3] Creating shortcuts..."

    mkdir -p "$HOME/Desktop"
    mkdir -p "$HOME/.config/autostart"
fi

# ── Extract the app ──
mkdir -p "$INSTALL_DIR"

cat > "$INSTALL_DIR/dose_app.py" << 'PYTHON_APP'
#!/usr/bin/env python3
"""
DOSE Home Station — Prototype Application
Matches the v1.0 Design & Build Specification exactly.

States: IDLE → READ → HOLD → CONFIRM → DISPENSED → IDLE
Hardware: Pi 4B + Camera Module 3 Wide + Elecrow 5" 800×480
"""

import time
import tkinter as tk
from tkinter import font as tkfont

from PIL import Image, ImageTk
from pyzbar.pyzbar import decode as qr_decode

try:
    from picamera2 import Picamera2
    HAVE_CAMERA = True
except Exception:
    HAVE_CAMERA = False

# ── Design tokens (from spec v1.0) ──
SCREEN_BG    = "#070708"
SCREEN_FG    = "#F4F4F2"
SCREEN_MUTED = "#7E8186"
DOSE_BLUE    = "#2F62F2"

SCREEN_W = 800
SCREEN_H = 480
CAPTURE_RES = (1280, 720)
HOLD_TIME = 3.0
DISPENSED_TIME = 4.0
IDLE_AFTER = 6.0
SCAN_INTERVAL = 80

MEDS = {
    "blue":   {"name": "Blue",   "accent": "#5B9BFF", "button": 1,
               "take_with": "A full glass of water. With or without food."},
    "red":    {"name": "Red",    "accent": "#FF6B6B", "button": 2,
               "take_with": "Food, to avoid stomach upset. Avoid alcohol."},
    "green":  {"name": "Green",  "accent": "#5BD08A", "button": 3,
               "take_with": "An empty stomach, ~1 hour before eating."},
    "yellow": {"name": "Yellow", "accent": "#E6C34A", "button": 4,
               "take_with": "Morning, with water. Do not crush or chew."},
}

NEXT_DOSE_TIME  = "10:00 AM"
NEXT_DOSE_PILLS = "2 Pills"

# States
S_IDLE      = "idle"
S_READ      = "read"
S_HOLD      = "hold"
S_CONFIRM   = "confirm"
S_DISPENSED = "dispensed"


def _pick_font(root):
    preferred = ["SF Pro Display", "SF Pro Text", "Inter", "Helvetica Neue",
                 "Roboto", "Arial", "DejaVu Sans"]
    available = set(tkfont.families(root))
    for name in preferred:
        if name in available:
            return name
    return "DejaVu Sans"


class DoseApp:
    def __init__(self, root):
        self.root = root
        self.state = S_IDLE
        self.current_med = None
        self.last_qr_seen = 0.0
        self.hold_start = 0.0
        self.dispensed_at = 0.0
        self.photo = None
        self.show_preview = False

        # ── Window setup ──
        root.title("DOSE Home Station")
        root.configure(bg=SCREEN_BG)
        root.geometry(f"{SCREEN_W}x{SCREEN_H}")
        root.attributes("-fullscreen", True)
        root.config(cursor="none")

        root.bind("<Escape>", lambda _: self._quit())
        root.bind("c", lambda _: self._toggle_preview())
        root.bind("<Button-1>", lambda _: self._on_tap())

        # ── Fonts (spec: clean neutral grotesque, light weights for large numerals) ──
        fam = _pick_font(root)
        self.f_clock = tkfont.Font(family=fam, size=30, weight="normal")
        self.f_label = tkfont.Font(family=fam, size=13, weight="bold")
        self.f_xl    = tkfont.Font(family=fam, size=52, weight="normal")
        self.f_name  = tkfont.Font(family=fam, size=48, weight="bold")
        self.f_pills = tkfont.Font(family=fam, size=24, weight="normal")
        self.f_body  = tkfont.Font(family=fam, size=19, weight="normal")
        self.f_big   = tkfont.Font(family=fam, size=40, weight="bold")
        self.f_hint  = tkfont.Font(family=fam, size=16, weight="normal")
        self.f_wm    = tkfont.Font(family=fam, size=18, weight="normal")
        self.f_strip = tkfont.Font(family=fam, size=17, weight="normal")

        # ── Top bar (persistent across all states) ──
        self.clock_label = tk.Label(root, text="", fg=SCREEN_FG, bg=SCREEN_BG,
                                    font=self.f_clock)
        self.clock_label.place(x=52, y=40)

        wifi = tk.Canvas(root, width=40, height=32, bg=SCREEN_BG, highlightthickness=0)
        wifi.place(x=710, y=44)
        self._draw_wifi(wifi, SCREEN_FG)

        # ── "dose" wordmark (bottom right, persistent) ──
        self.wm_label = tk.Label(root, text="dose", fg="#9A9DA2", bg=SCREEN_BG,
                                 font=self.f_wm)
        self.wm_label.place(x=718, y=430)

        # ══════════════════════════════════════════════
        # STATE 00: IDLE
        # ══════════════════════════════════════════════
        self.idle_frame = tk.Frame(root, bg=SCREEN_BG)
        self.idle_frame.place(x=0, y=120, width=SCREEN_W, height=SCREEN_H - 120)

        tk.Label(self.idle_frame, text="NEXT DOSE", fg=SCREEN_MUTED, bg=SCREEN_BG,
                 font=self.f_label).place(x=52, y=50)
        tk.Label(self.idle_frame, text=NEXT_DOSE_TIME, fg=SCREEN_FG, bg=SCREEN_BG,
                 font=self.f_xl).place(x=49, y=74)
        tk.Label(self.idle_frame, text=NEXT_DOSE_PILLS, fg=SCREEN_FG, bg=SCREEN_BG,
                 font=self.f_pills).place(x=52, y=156)

        # ══════════════════════════════════════════════
        # STATE 01: READ (hover)
        # ══════════════════════════════════════════════
        self.read_frame = tk.Frame(root, bg=SCREEN_BG)
        self.read_frame.place(x=0, y=120, width=SCREEN_W, height=SCREEN_H - 120)

        self.read_name = tk.Label(self.read_frame, text="", fg=SCREEN_FG, bg=SCREEN_BG,
                                  font=self.f_name, anchor="w")
        self.read_name.place(x=52, y=30)

        tk.Label(self.read_frame, text="TAKE WITH", fg=SCREEN_MUTED, bg=SCREEN_BG,
                 font=self.f_label).place(x=52, y=110)
        self.read_take = tk.Label(self.read_frame, text="", fg=SCREEN_FG, bg=SCREEN_BG,
                                  font=self.f_body, anchor="nw", justify="left",
                                  wraplength=620)
        self.read_take.place(x=52, y=136, width=620)

        self.read_hint = tk.Label(self.read_frame, text="Hold to confirm",
                                  fg=SCREEN_MUTED, bg=SCREEN_BG, font=self.f_hint)
        self.read_hint.place(x=52, y=290)

        # ══════════════════════════════════════════════
        # STATE 02: HOLD (commit)
        # ══════════════════════════════════════════════
        self.hold_frame = tk.Frame(root, bg=SCREEN_BG)
        self.hold_frame.place(x=0, y=120, width=SCREEN_W, height=SCREEN_H - 120)

        self.hold_name = tk.Label(self.hold_frame, text="", fg=SCREEN_FG, bg=SCREEN_BG,
                                  font=self.f_name, anchor="w")
        self.hold_name.place(x=52, y=30)

        tk.Label(self.hold_frame, text="HOLD TO CONFIRM", fg=SCREEN_MUTED, bg=SCREEN_BG,
                 font=self.f_label).place(x=52, y=110)
        self.hold_keeping = tk.Label(self.hold_frame, text="Keep holding…",
                                     fg=SCREEN_FG, bg=SCREEN_BG, font=self.f_body)
        self.hold_keeping.place(x=52, y=136)

        # Progress bar background
        self.prog_bg = tk.Canvas(self.hold_frame, width=620, height=6,
                                 bg=SCREEN_BG, highlightthickness=0)
        self.prog_bg.place(x=52, y=290)

        # ══════════════════════════════════════════════
        # STATE 03: CONFIRM (press to dispense)
        # ══════════════════════════════════════════════
        self.confirm_frame = tk.Frame(root, bg=SCREEN_BG)
        self.confirm_frame.place(x=0, y=120, width=SCREEN_W, height=SCREEN_H - 120)

        tk.Label(self.confirm_frame, text="CONFIRMED", fg=SCREEN_MUTED, bg=SCREEN_BG,
                 font=self.f_label).place(x=52, y=50)
        self.confirm_text = tk.Label(self.confirm_frame, text="Press down\nto dispense",
                                     fg=SCREEN_FG, bg=SCREEN_BG, font=self.f_big,
                                     anchor="nw", justify="left")
        self.confirm_text.place(x=52, y=80)

        # ══════════════════════════════════════════════
        # STATE 04: DISPENSED
        # ══════════════════════════════════════════════
        self.dispensed_frame = tk.Frame(root, bg=SCREEN_BG)
        self.dispensed_frame.place(x=0, y=120, width=SCREEN_W, height=SCREEN_H - 120)

        tk.Label(self.dispensed_frame, text="DISPENSED", fg=SCREEN_MUTED, bg=SCREEN_BG,
                 font=self.f_label).place(x=52, y=50)
        self.disp_name = tk.Label(self.dispensed_frame, text="", fg=SCREEN_FG,
                                  bg=SCREEN_BG, font=self.f_big, anchor="w")
        self.disp_name.place(x=52, y=80)

        # Bottom strip
        self.strip_frame = tk.Frame(self.dispensed_frame, bg="#1A1A1C",
                                    highlightbackground="#2A2A2E",
                                    highlightthickness=1)
        self.strip_frame.place(x=0, y=240, width=SCREEN_W, height=50)
        self.strip_label = tk.Label(self.strip_frame, text="", fg=SCREEN_FG,
                                    bg="#1A1A1C", font=self.f_strip, anchor="w")
        self.strip_label.place(x=52, y=12)

        # ── Camera preview (debug) ──
        self.preview_label = tk.Label(root, bg=SCREEN_BG, highlightthickness=0)

        # ── Start ──
        self._go_idle()
        self._tick_clock()
        self._init_camera()

    # ── WiFi icon ──
    def _draw_wifi(self, canvas, color):
        cx, cy = 19, 26
        for r in (16, 11, 6):
            canvas.create_arc(cx - r, cy - r, cx + r, cy + r,
                              start=55, extent=70, style="arc",
                              outline=color, width=3)
        canvas.create_oval(cx - 2, cy - 2, cx + 2, cy + 2,
                           fill=color, outline=color)

    # ── Camera ──
    def _init_camera(self):
        if not HAVE_CAMERA:
            return
        self.cam = Picamera2()
        config = self.cam.create_preview_configuration(
            main={"size": CAPTURE_RES, "format": "RGB888"})
        self.cam.configure(config)
        self.cam.start()
        try:
            self.cam.set_controls({"AfMode": 2})
        except Exception:
            pass
        self.root.after(150, self._scan_loop)

    # ── State transitions ──
    def _go_idle(self):
        self.state = S_IDLE
        self.current_med = None
        self.wm_label.lift()
        self.idle_frame.tkraise()

    def _go_read(self, key):
        self.state = S_READ
        self.current_med = key
        med = MEDS[key]
        self.read_name.configure(text=med["name"])
        self.read_take.configure(text=med["take_with"])
        self.wm_label.lift()
        self.read_frame.tkraise()

    def _go_hold(self):
        self.state = S_HOLD
        self.hold_start = time.monotonic()
        med = MEDS[self.current_med]
        self.hold_name.configure(text=med["name"])
        self.wm_label.lift()
        self.hold_frame.tkraise()
        self._update_progress()

    def _go_confirm(self):
        self.state = S_CONFIRM
        self.wm_label.lift()
        self.confirm_frame.tkraise()

    def _go_dispensed(self):
        self.state = S_DISPENSED
        self.dispensed_at = time.monotonic()
        med = MEDS[self.current_med]
        self.disp_name.configure(text=med["name"])
        self.strip_label.configure(
            text=f"Dispensed: {med['name']}  ·  please check before taking medication")
        self.dispensed_frame.tkraise()
        self.root.after(int(DISPENSED_TIME * 1000), self._dispensed_timeout)

    def _dispensed_timeout(self):
        if self.state == S_DISPENSED:
            self._go_idle()

    # ── Progress bar for hold state ──
    def _update_progress(self):
        if self.state != S_HOLD:
            return
        elapsed = time.monotonic() - self.hold_start
        frac = min(elapsed / HOLD_TIME, 1.0)
        accent = MEDS[self.current_med]["accent"]

        self.prog_bg.delete("all")
        self.prog_bg.create_rectangle(0, 0, 620, 6,
                                      fill="rgba(255,255,255,0.14)",
                                      outline="")
        self.prog_bg.create_rectangle(0, 0, 620, 6,
                                      fill="#2A2A2E", outline="")
        self.prog_bg.create_rectangle(0, 0, int(620 * frac), 6,
                                      fill=accent, outline="")

        if frac >= 1.0:
            self._go_confirm()
        else:
            self.root.after(30, self._update_progress)

    # ── Touch / tap handler (simulates capacitive buttons on touchscreen) ──
    def _on_tap(self):
        if self.state == S_READ:
            self._go_hold()
        elif self.state == S_CONFIRM:
            self._go_dispensed()
        elif self.state == S_DISPENSED:
            self._go_idle()

    def _toggle_preview(self):
        self.show_preview = not self.show_preview
        if self.show_preview:
            self.preview_label.place(x=24, y=336, width=200, height=120)
            self.preview_label.lift()
        else:
            self.preview_label.place_forget()

    # ── Clock ──
    def _tick_clock(self):
        self.clock_label.configure(text=time.strftime("%-I:%M %p"))
        self.root.after(1000, self._tick_clock)

    # ── QR scan loop ──
    def _scan_loop(self):
        arr = self.cam.capture_array()
        img = Image.fromarray(arr[:, :, ::-1])

        found = None
        for code in qr_decode(img):
            text = code.data.decode("utf-8", "ignore").strip().lower()
            if text in MEDS:
                found = text

        now = time.monotonic()
        if found:
            self.last_qr_seen = now
            if self.state == S_IDLE and found != self.current_med:
                self._go_read(found)
        elif self.state == S_READ and now - self.last_qr_seen > IDLE_AFTER:
            self._go_idle()

        if self.show_preview:
            disp = img.resize((200, 120))
            self.photo = ImageTk.PhotoImage(disp)
            self.preview_label.configure(image=self.photo)

        self.root.after(SCAN_INTERVAL, self._scan_loop)

    def _quit(self):
        try:
            if HAVE_CAMERA:
                self.cam.stop()
        finally:
            self.root.destroy()


def main():
    root = tk.Tk()
    DoseApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
PYTHON_APP

chmod +x "$INSTALL_DIR/dose_app.py"

# ── Create launcher script ──
cat > "$INSTALL_DIR/launch.sh" << 'LAUNCHER'
#!/bin/bash
export DISPLAY=:0
INSTALL_DIR="$HOME/dose-home-station"
cd "$INSTALL_DIR"
python3 "$INSTALL_DIR/dose_app.py" 2>/tmp/dose_error.log
EXIT_CODE=$?
if [ $EXIT_CODE -ne 0 ]; then
    lxterminal -e bash -c "echo 'DOSE failed to start:'; echo ''; cat /tmp/dose_error.log; echo ''; echo 'Press any key to close...'; read -n 1 -s" 2>/dev/null
fi
LAUNCHER
chmod +x "$INSTALL_DIR/launch.sh"

# ── Create desktop shortcut ──
cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Type=Application
Name=DOSE Home Station
Comment=Launch DOSE medication dispenser
Exec=/bin/bash $INSTALL_DIR/launch.sh
Terminal=false
Categories=Utility;
StartupNotify=false
EOF
chmod +x "$DESKTOP_FILE"
gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true
dbus-launch gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true

# ── Autostart on boot ──
mkdir -p "$(dirname "$AUTOSTART_FILE")"
cat > "$AUTOSTART_FILE" << EOF
[Desktop Entry]
Type=Application
Name=DOSE Home Station
Exec=/bin/bash $INSTALL_DIR/launch.sh
Terminal=false
X-GNOME-Autostart-enabled=true
EOF

# ── Verify ──
echo ""
echo "  Checking everything works..."
python3 -c "
import tkinter as tk
from PIL import Image, ImageTk
from pyzbar.pyzbar import decode
print('  All dependencies OK!')
"

echo ""
echo "========================================="
echo "  DONE!"
echo "========================================="
echo ""
echo "  Starting DOSE now..."
echo "  Press Esc to exit the app."
echo ""
sleep 1

# Launch the app
export DISPLAY=:0
python3 "$INSTALL_DIR/dose_app.py" &

echo "  DOSE is running!"
echo "  You can close this terminal window."
echo "  Press any key to close..."
read -n 1 -s
