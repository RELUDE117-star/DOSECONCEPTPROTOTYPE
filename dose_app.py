#!/usr/bin/env python3
"""
DOSE Home Station — Prototype Application
v1.0 Design & Build Specification

States: IDLE → READ → HOLD → CONFIRM → DISPENSED → IDLE

Gracefully handles missing hardware:
  - No camera? Shows "Camera not connected" on idle screen.
  - No touch sensor? Touchscreen taps advance the flow.
"""

import time
import subprocess
import tkinter as tk
from tkinter import font as tkfont

try:
    from PIL import Image, ImageTk
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False

try:
    from pyzbar.pyzbar import decode as qr_decode
    HAVE_PYZBAR = True
except ImportError:
    HAVE_PYZBAR = False

HAVE_CAMERA = False
Picamera2 = None
try:
    from picamera2 import Picamera2 as _Picamera2
    Picamera2 = _Picamera2
    HAVE_CAMERA = True
except Exception:
    pass

# ── Design tokens (v1.0 spec) ──
SCREEN_BG    = "#070708"
SCREEN_FG    = "#F4F4F2"
SCREEN_MUTED = "#7E8186"

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


def _detect_hardware():
    """Check what's actually connected to the Pi."""
    hw = {"camera": False, "display": True, "touch": False}

    if HAVE_CAMERA and HAVE_PIL and HAVE_PYZBAR:
        try:
            cam = Picamera2()
            cam.close()
            hw["camera"] = True
        except Exception:
            pass

    return hw


class DoseApp:
    def __init__(self, root):
        self.root = root
        self.state = S_IDLE
        self.current_med = None
        self.last_qr_seen = 0.0
        self.hold_start = 0.0
        self.photo = None
        self.show_preview = False
        self.cam = None

        hw = _detect_hardware()
        self.has_camera = hw["camera"]

        root.title("DOSE Home Station")
        root.configure(bg=SCREEN_BG)
        root.geometry(f"{SCREEN_W}x{SCREEN_H}")
        root.attributes("-fullscreen", True)
        root.config(cursor="none")

        root.bind("<Escape>", lambda _: self._quit())
        root.bind("c", lambda _: self._toggle_preview())
        root.bind("<Button-1>", lambda _: self._on_tap())

        # ── Fonts ──
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
        self.f_hw    = tkfont.Font(family=fam, size=11, weight="normal")

        # ── Top bar ──
        self.clock_label = tk.Label(root, text="", fg=SCREEN_FG, bg=SCREEN_BG,
                                    font=self.f_clock)
        self.clock_label.place(x=52, y=40)

        wifi = tk.Canvas(root, width=40, height=32, bg=SCREEN_BG, highlightthickness=0)
        wifi.place(x=710, y=44)
        self._draw_wifi(wifi, SCREEN_FG)

        # ── "dose" wordmark ──
        self.wm_label = tk.Label(root, text="dose", fg="#9A9DA2", bg=SCREEN_BG,
                                 font=self.f_wm)
        self.wm_label.place(x=718, y=430)

        # ══════════════════════════════════════
        # STATE 00: IDLE
        # ══════════════════════════════════════
        self.idle_frame = tk.Frame(root, bg=SCREEN_BG)
        self.idle_frame.place(x=0, y=120, width=SCREEN_W, height=SCREEN_H - 120)

        tk.Label(self.idle_frame, text="NEXT DOSE", fg=SCREEN_MUTED, bg=SCREEN_BG,
                 font=self.f_label).place(x=52, y=50)
        tk.Label(self.idle_frame, text=NEXT_DOSE_TIME, fg=SCREEN_FG, bg=SCREEN_BG,
                 font=self.f_xl).place(x=49, y=74)
        tk.Label(self.idle_frame, text=NEXT_DOSE_PILLS, fg=SCREEN_FG, bg=SCREEN_BG,
                 font=self.f_pills).place(x=52, y=156)

        # Hardware status (bottom left, subtle)
        if not self.has_camera:
            self.hw_status = tk.Label(self.idle_frame, text="Camera not connected",
                                      fg="#555555", bg=SCREEN_BG, font=self.f_hw)
            self.hw_status.place(x=52, y=300)

        # ══════════════════════════════════════
        # STATE 01: READ
        # ══════════════════════════════════════
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

        # ══════════════════════════════════════
        # STATE 02: HOLD
        # ══════════════════════════════════════
        self.hold_frame = tk.Frame(root, bg=SCREEN_BG)
        self.hold_frame.place(x=0, y=120, width=SCREEN_W, height=SCREEN_H - 120)

        self.hold_name = tk.Label(self.hold_frame, text="", fg=SCREEN_FG, bg=SCREEN_BG,
                                  font=self.f_name, anchor="w")
        self.hold_name.place(x=52, y=30)

        tk.Label(self.hold_frame, text="HOLD TO CONFIRM", fg=SCREEN_MUTED, bg=SCREEN_BG,
                 font=self.f_label).place(x=52, y=110)
        tk.Label(self.hold_frame, text="Keep holding…", fg=SCREEN_FG, bg=SCREEN_BG,
                 font=self.f_body).place(x=52, y=136)

        self.prog_canvas = tk.Canvas(self.hold_frame, width=620, height=6,
                                     bg=SCREEN_BG, highlightthickness=0)
        self.prog_canvas.place(x=52, y=290)

        # ══════════════════════════════════════
        # STATE 03: CONFIRM
        # ══════════════════════════════════════
        self.confirm_frame = tk.Frame(root, bg=SCREEN_BG)
        self.confirm_frame.place(x=0, y=120, width=SCREEN_W, height=SCREEN_H - 120)

        tk.Label(self.confirm_frame, text="CONFIRMED", fg=SCREEN_MUTED, bg=SCREEN_BG,
                 font=self.f_label).place(x=52, y=50)
        tk.Label(self.confirm_frame, text="Press down\nto dispense",
                 fg=SCREEN_FG, bg=SCREEN_BG, font=self.f_big,
                 anchor="nw", justify="left").place(x=52, y=80)

        # ══════════════════════════════════════
        # STATE 04: DISPENSED
        # ══════════════════════════════════════
        self.dispensed_frame = tk.Frame(root, bg=SCREEN_BG)
        self.dispensed_frame.place(x=0, y=120, width=SCREEN_W, height=SCREEN_H - 120)

        tk.Label(self.dispensed_frame, text="DISPENSED", fg=SCREEN_MUTED, bg=SCREEN_BG,
                 font=self.f_label).place(x=52, y=50)
        self.disp_name = tk.Label(self.dispensed_frame, text="", fg=SCREEN_FG,
                                  bg=SCREEN_BG, font=self.f_big, anchor="w")
        self.disp_name.place(x=52, y=80)

        self.strip_frame = tk.Frame(self.dispensed_frame, bg="#1A1A1C")
        self.strip_frame.place(x=0, y=240, width=SCREEN_W, height=50)
        # Top border line on strip
        tk.Frame(self.strip_frame, bg="#2A2A2E", height=1).place(x=0, y=0,
                                                                  width=SCREEN_W)
        self.strip_label = tk.Label(self.strip_frame, text="", fg=SCREEN_FG,
                                    bg="#1A1A1C", font=self.f_strip, anchor="w")
        self.strip_label.place(x=52, y=14)

        # ── Preview ──
        self.preview_label = tk.Label(root, bg=SCREEN_BG, highlightthickness=0)

        # ── Start ──
        self._go_idle()
        self._tick_clock()
        self._init_camera()

    def _draw_wifi(self, canvas, color):
        cx, cy = 19, 26
        for r in (16, 11, 6):
            canvas.create_arc(cx - r, cy - r, cx + r, cy + r,
                              start=55, extent=70, style="arc",
                              outline=color, width=3)
        canvas.create_oval(cx - 2, cy - 2, cx + 2, cy + 2,
                           fill=color, outline=color)

    def _init_camera(self):
        if not self.has_camera:
            return
        try:
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
        except Exception:
            self.has_camera = False
            self.cam = None

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
        if not self.current_med:
            return
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
        med = MEDS[self.current_med]
        self.disp_name.configure(text=med["name"])
        self.strip_label.configure(
            text=f"Dispensed: {med['name']}  ·  please check before taking medication")
        self.dispensed_frame.tkraise()
        self.root.after(int(DISPENSED_TIME * 1000), self._dispensed_timeout)

    def _dispensed_timeout(self):
        if self.state == S_DISPENSED:
            self._go_idle()

    def _update_progress(self):
        if self.state != S_HOLD:
            return
        elapsed = time.monotonic() - self.hold_start
        frac = min(elapsed / HOLD_TIME, 1.0)
        accent = MEDS[self.current_med]["accent"]

        self.prog_canvas.delete("all")
        self.prog_canvas.create_rectangle(0, 0, 620, 6, fill="#2A2A2E", outline="")
        self.prog_canvas.create_rectangle(0, 0, int(620 * frac), 6,
                                          fill=accent, outline="")

        if frac >= 1.0:
            self._go_confirm()
        else:
            self.root.after(30, self._update_progress)

    def _on_tap(self):
        if self.state == S_IDLE and not self.has_camera:
            # No camera: tap cycles through demo with "blue" medication
            self._go_read("blue")
        elif self.state == S_READ:
            self._go_hold()
        elif self.state == S_CONFIRM:
            self._go_dispensed()
        elif self.state == S_DISPENSED:
            self._go_idle()

    def _toggle_preview(self):
        if not self.has_camera:
            return
        self.show_preview = not self.show_preview
        if self.show_preview:
            self.preview_label.place(x=24, y=336, width=200, height=120)
            self.preview_label.lift()
        else:
            self.preview_label.place_forget()

    def _tick_clock(self):
        self.clock_label.configure(text=time.strftime("%-I:%M %p"))
        self.root.after(1000, self._tick_clock)

    def _scan_loop(self):
        if not self.cam:
            return
        try:
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
                if self.state == S_IDLE:
                    self._go_read(found)
            elif self.state == S_READ and now - self.last_qr_seen > IDLE_AFTER:
                self._go_idle()

            if self.show_preview:
                disp = img.resize((200, 120))
                self.photo = ImageTk.PhotoImage(disp)
                self.preview_label.configure(image=self.photo)
        except Exception:
            pass

        self.root.after(SCAN_INTERVAL, self._scan_loop)

    def _quit(self):
        try:
            if self.cam:
                self.cam.stop()
        except Exception:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    DoseApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
