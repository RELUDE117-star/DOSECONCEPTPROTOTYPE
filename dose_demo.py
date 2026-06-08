#!/usr/bin/env python3
"""
DOSE Home Station Prototype
============================
Full-screen kiosk application for the Dose Home Station.

Boots into a clean idle screen (clock, next dose). The camera continuously
reads QR codes in the background; when a medication code is scanned, the
screen shows instructions (what to take it with, which button to press),
then returns to idle after the code leaves the camera's view.

Hardware
--------
  * Raspberry Pi 4B
  * Raspberry Pi Camera Module 3 Wide (120° FOV, IMX708, CSI ribbon)
  * Elecrow 5" 800×480 capacitive touchscreen (HDMI + USB touch)

Controls
--------
  Esc           quit the application
  c             toggle a small camera preview (for aiming / debug)
  tap screen    dismiss a result back to the idle screen

QR Codes
--------
  Encode these exact strings, one per code:  blue   red   green   yellow
  Use generate_qr_codes.py to create printable QR code sheets.
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


# ---------------------------------------------------------------------------
# Medication database (demo data — not real clinical guidance)
# ---------------------------------------------------------------------------
MEDS = {
    "blue": {
        "name": "Blue",
        "accent": "#5B9BFF",
        "take_with": "Take with a full glass of water. With or without food.",
        "button": 1,
    },
    "red": {
        "name": "Red",
        "accent": "#FF6B6B",
        "take_with": "Take with food to avoid stomach upset. Avoid alcohol.",
        "button": 2,
    },
    "green": {
        "name": "Green",
        "accent": "#5BD08A",
        "take_with": "Take on an empty stomach, about 1 hour before eating.",
        "button": 3,
    },
    "yellow": {
        "name": "Yellow",
        "accent": "#E6C34A",
        "take_with": "Take in the morning with water. Do not crush or chew.",
        "button": 4,
    },
}

NEXT_DOSE_TIME = "10:00 AM"
NEXT_DOSE_PILLS = "2 Pills"

# ---------------------------------------------------------------------------
# Display & camera settings — tuned for Elecrow 5" (800×480) + Camera Module 3 Wide
# ---------------------------------------------------------------------------
SCREEN_W = 800
SCREEN_H = 480
BG = "#000000"
TEXT = "#F4F4F2"
MUTED = "#7E8186"

# Camera Module 3 Wide capture resolution.
# 1280×720 balances QR decode speed with readability at the wide 120° FOV.
# Drop to (800, 480) if scanning feels sluggish on your Pi 4B.
CAPTURE_RES = (1280, 720)

IDLE_AFTER = 6.0       # seconds with no QR visible → return to idle
SCAN_INTERVAL = 80     # ms between camera frames
FULLSCREEN = True


def _pick_font_family(root):
    """Pick the best available sans-serif font."""
    preferred = [
        "Inter", "SF Pro Display", "SF Pro Text", "Helvetica Neue",
        "Roboto", "Arial", "DejaVu Sans",
    ]
    available = set(tkfont.families(root))
    for name in preferred:
        if name in available:
            return name
    return "DejaVu Sans"


def _draw_wifi_icon(canvas, color):
    """Draw a simple monochrome wifi glyph with arcs and a dot."""
    cx, cy = 19, 26
    for r in (16, 11, 6):
        canvas.create_arc(
            cx - r, cy - r, cx + r, cy + r,
            start=55, extent=70, style="arc",
            outline=color, width=3,
        )
    canvas.create_oval(cx - 2, cy - 2, cx + 2, cy + 2, fill=color, outline=color)


class DoseApp:
    """Main application class for the DOSE Home Station kiosk UI."""

    def __init__(self, root):
        self.root = root
        self.current = None        # currently displayed medication key
        self.last_seen = 0.0       # monotonic time of last QR detection
        self.photo = None          # keep reference to prevent GC
        self.show_preview = False

        # ---- window setup (fullscreen kiosk) ----
        root.title("DOSE Home Station")
        root.configure(bg=BG)
        root.geometry(f"{SCREEN_W}x{SCREEN_H}")
        if FULLSCREEN:
            root.attributes("-fullscreen", True)
        root.config(cursor="none")

        # ---- key bindings ----
        root.bind("<Escape>", lambda _: self._quit())
        root.bind("c", lambda _: self._toggle_preview())
        root.bind("<Button-1>", lambda _: self._show_idle())

        # ---- fonts (sized for 800×480 at ~5") ----
        fam = _pick_font_family(root)
        self.f_clock = tkfont.Font(family=fam, size=30)
        self.f_label = tkfont.Font(family=fam, size=12, weight="bold")
        self.f_xl = tkfont.Font(family=fam, size=52)
        self.f_name = tkfont.Font(family=fam, size=46)
        self.f_pills = tkfont.Font(family=fam, size=24)
        self.f_body = tkfont.Font(family=fam, size=18)
        self.f_num = tkfont.Font(family=fam, size=64, weight="bold")
        self.f_mark = tkfont.Font(family=fam, size=18)

        # ---- shared top bar ----
        self.clock_label = tk.Label(root, text="", fg=TEXT, bg=BG, font=self.f_clock)
        self.clock_label.place(x=56, y=38)

        wifi_canvas = tk.Canvas(root, width=40, height=32, bg=BG, highlightthickness=0)
        wifi_canvas.place(x=704, y=44)
        _draw_wifi_icon(wifi_canvas, TEXT)

        tk.Label(
            root, text="dose", fg=MUTED, bg=BG, font=self.f_mark,
        ).place(x=716, y=430)

        # ---- idle view ----
        self.idle_frame = tk.Frame(root, bg=BG)
        self.idle_frame.place(x=0, y=120, width=SCREEN_W, height=SCREEN_H - 120)

        tk.Label(
            self.idle_frame, text="NEXT DOSE", fg=MUTED, bg=BG, font=self.f_label,
        ).place(x=56, y=60)
        tk.Label(
            self.idle_frame, text=NEXT_DOSE_TIME, fg=TEXT, bg=BG, font=self.f_xl,
        ).place(x=53, y=82)
        tk.Label(
            self.idle_frame, text=NEXT_DOSE_PILLS, fg=TEXT, bg=BG, font=self.f_pills,
        ).place(x=56, y=164)

        # ---- result view ----
        self.result_frame = tk.Frame(root, bg=BG)
        self.result_frame.place(x=0, y=120, width=SCREEN_W, height=SCREEN_H - 120)

        self.r_name = tk.Label(
            self.result_frame, text="", fg=TEXT, bg=BG,
            font=self.f_name, anchor="w",
        )
        self.r_name.place(x=54, y=34)

        tk.Label(
            self.result_frame, text="TAKE WITH", fg=MUTED, bg=BG, font=self.f_label,
        ).place(x=56, y=116)

        self.r_take = tk.Label(
            self.result_frame, text="", fg=TEXT, bg=BG,
            font=self.f_body, anchor="nw", justify="left", wraplength=690,
        )
        self.r_take.place(x=56, y=138, width=690, height=70)

        tk.Label(
            self.result_frame, text="PRESS BUTTON", fg=MUTED, bg=BG, font=self.f_label,
        ).place(x=56, y=224)

        self.r_num = tk.Label(
            self.result_frame, text="", bg=BG, font=self.f_num, anchor="w",
        )
        self.r_num.place(x=52, y=246)

        # ---- camera preview overlay (off by default, toggle with 'c') ----
        self.preview_label = tk.Label(root, bg=BG, highlightthickness=0)

        # ---- start ----
        self._show_idle()
        self._tick_clock()
        self._init_camera()

    # ----------------------------------------------------------------- camera
    def _init_camera(self):
        """Initialize Raspberry Pi Camera Module 3 Wide via picamera2."""
        if not HAVE_CAMERA:
            return
        self.cam = Picamera2()
        config = self.cam.create_preview_configuration(
            main={"size": CAPTURE_RES, "format": "RGB888"},
        )
        self.cam.configure(config)
        self.cam.start()

        # Camera Module 3 Wide supports continuous autofocus (AfMode 2).
        # Once the camera + pill slot are physically fixed, you can lock focus
        # for instant identical scans:
        #   self.cam.set_controls({"AfMode": 0, "LensPosition": 4.0})
        try:
            self.cam.set_controls({"AfMode": 2})
        except Exception:
            pass

        self.root.after(150, self._scan_loop)

    # ------------------------------------------------------------------ views
    def _show_idle(self):
        self.current = None
        self.idle_frame.tkraise()

    def _show_result(self, key):
        self.current = key
        med = MEDS[key]
        self.r_name.configure(text=med["name"])
        self.r_take.configure(text=med["take_with"])
        self.r_num.configure(text=str(med["button"]), fg=med["accent"])
        self.result_frame.tkraise()

    def _toggle_preview(self):
        self.show_preview = not self.show_preview
        if self.show_preview:
            self.preview_label.place(x=24, y=336, width=200, height=120)
            self.preview_label.lift()
        else:
            self.preview_label.place_forget()

    # ------------------------------------------------------------------- loop
    def _tick_clock(self):
        self.clock_label.configure(text=time.strftime("%-I:%M %p"))
        self.root.after(1000, self._tick_clock)

    def _scan_loop(self):
        """Capture a frame, decode QR codes, update the UI."""
        arr = self.cam.capture_array()
        img = Image.fromarray(arr[:, :, ::-1])

        found = None
        for code in qr_decode(img):
            text = code.data.decode("utf-8", "ignore").strip().lower()
            if text in MEDS:
                found = text

        now = time.monotonic()
        if found:
            self.last_seen = now
            if found != self.current:
                self._show_result(found)
        elif self.current is not None and now - self.last_seen > IDLE_AFTER:
            self._show_idle()

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
