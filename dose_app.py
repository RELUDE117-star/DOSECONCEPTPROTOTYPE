#!/usr/bin/env python3
"""
DOSE Home Station — Prototype Application
v2.0 — QR-to-load, MPR121 touch, persistent counts, auto-update

States: IDLE → (QR scan) → QTY_CONFIRM → IDLE
        IDLE → READ → HOLD → CONFIRM → DISPENSED → IDLE

Meds start empty (count 0). Scan a QR to load a slot.
MPR121 pads 0-3 map to blue/red/green/yellow.

Gracefully handles missing hardware.
"""

import json
import hashlib
import os
import sys
import time
import threading
import subprocess
import base64
import io
import tkinter as tk
from tkinter import font as tkfont
from urllib.request import urlopen
from urllib.error import URLError

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

HAVE_MPR121 = False
mpr121_mod = None
try:
    import board
    import busio
    import adafruit_mpr121
    mpr121_mod = adafruit_mpr121
    HAVE_MPR121 = True
except Exception:
    pass

# ── Design tokens ──
SCREEN_BG    = "#070708"
SCREEN_FG    = "#F4F4F2"
SCREEN_MUTED = "#7E8186"
CARD_BG      = "#141418"

SCREEN_W = 800
SCREEN_H = 480
CAPTURE_RES = (1280, 720)
HOLD_TIME = 3.0
DISPENSED_TIME = 4.0
SCAN_INTERVAL = 100
DEFAULT_QTY = 30

SLOT_KEYS = ["blue", "red", "green", "yellow"]

SLOT_DEFS = {
    "blue":   {"accent": "#5B9BFF", "pad": 0},
    "red":    {"accent": "#FF6B6B", "pad": 1},
    "green":  {"accent": "#5BD08A", "pad": 2},
    "yellow": {"accent": "#E6C34A", "pad": 3},
}

S_IDLE        = "idle"
S_QTY_CONFIRM = "qty_confirm"
S_READ        = "read"
S_HOLD        = "hold"
S_CONFIRM     = "confirm"
S_DISPENSED   = "dispensed"

DATA_PATH = os.path.expanduser("~/dose-home-station/med_data.json")
APP_DIR = os.path.expanduser("~/dose-home-station")
APP_FILE = os.path.join(APP_DIR, "dose_app.py")
RAW_URL = ("https://raw.githubusercontent.com/relude117-star/"
           "doseconceptprototype/claude/quirky-brown-vkHwi/dose_app.py")

DOSE_LOGO_B64 = (
    "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"
    "DQXm1BIVyT4AAAAASUVORK5CYII="
)


def _raise(widget):
    """Raise widget in stacking order — safe for Canvas too."""
    widget.tk.call('raise', widget._w)


def _pick_font(root):
    preferred = ["Nunito", "Nunito Sans", "SF Pro Display", "Inter",
                 "Helvetica Neue", "Roboto", "DejaVu Sans"]
    available = set(tkfont.families(root))
    for name in preferred:
        if name in available:
            return name
    return "DejaVu Sans"


def _load_data():
    """Load persisted medication data. Returns dict keyed by slot."""
    try:
        with open(DATA_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_data(data):
    """Persist medication data."""
    try:
        os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
        with open(DATA_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


class DoseApp:
    def __init__(self, root):
        self.root = root
        self.state = S_IDLE
        self.current_med = None
        self.hold_start = 0.0
        self.photo = None
        self.logo_photo = None
        self.show_preview = False
        self.cam = None
        self.mpr = None
        self.mpr_prev = [False] * 12
        self.settings = {"constant_scan": False}

        # Medication state: slot_key -> {name, count, loaded, take_with}
        self.med_data = {}
        self._init_med_data()

        # Hardware detection
        self.has_camera = False
        if HAVE_CAMERA and HAVE_PIL and HAVE_PYZBAR:
            try:
                cam = Picamera2()
                cam.close()
                self.has_camera = True
            except Exception:
                pass

        self.has_touch = False
        if HAVE_MPR121:
            try:
                import board, busio
                i2c = busio.I2C(board.SCL, board.SDA)
                self.mpr = mpr121_mod.MPR121(i2c, address=0x5A)
                self.has_touch = True
            except Exception:
                pass

        # ── Window setup ──
        root.title("DOSE Home Station")
        root.configure(bg=SCREEN_BG)
        root.geometry(f"{SCREEN_W}x{SCREEN_H}")
        try:
            root.attributes("-fullscreen", True)
        except Exception:
            pass
        root.config(cursor="none")

        root.bind("<Escape>", lambda _: self._quit())
        root.bind("c", lambda _: self._toggle_preview())
        root.bind("<Button-1>", self._on_tap)

        # ── Fonts ──
        fam = _pick_font(root)
        self.fam = fam
        self.f_clock = tkfont.Font(family=fam, size=30)
        self.f_label = tkfont.Font(family=fam, size=13, weight="bold")
        self.f_xl    = tkfont.Font(family=fam, size=44)
        self.f_name  = tkfont.Font(family=fam, size=36, weight="bold")
        self.f_pills = tkfont.Font(family=fam, size=24)
        self.f_body  = tkfont.Font(family=fam, size=19)
        self.f_big   = tkfont.Font(family=fam, size=36, weight="bold")
        self.f_hint  = tkfont.Font(family=fam, size=16)
        self.f_wm    = tkfont.Font(family=fam, size=18)
        self.f_strip = tkfont.Font(family=fam, size=17)
        self.f_hw    = tkfont.Font(family=fam, size=11)
        self.f_count = tkfont.Font(family=fam, size=60, weight="bold")
        self.f_btn   = tkfont.Font(family=fam, size=18, weight="bold")
        self.f_small = tkfont.Font(family=fam, size=14)

        # ── Top bar ──
        self.clock_label = tk.Label(root, text="", fg=SCREEN_FG, bg=SCREEN_BG,
                                    font=self.f_clock)
        self.clock_label.place(x=52, y=40)

        # Logo
        try:
            raw = base64.b64decode(DOSE_LOGO_B64)
            logo_img = Image.open(io.BytesIO(raw)).resize((36, 36),
                                                           Image.LANCZOS)
            self.logo_photo = ImageTk.PhotoImage(logo_img)
            tk.Label(root, image=self.logo_photo, bg=SCREEN_BG).place(x=680, y=42)
        except Exception:
            pass

        wifi = tk.Canvas(root, width=40, height=32, bg=SCREEN_BG, highlightthickness=0)
        wifi.place(x=730, y=44)
        self._draw_wifi(wifi, SCREEN_FG)

        self.wm_label = tk.Label(root, text="dose", fg="#9A9DA2", bg=SCREEN_BG,
                                 font=self.f_wm)
        self.wm_label.place(x=718, y=430)

        # ══════════════════════════════════════
        # IDLE FRAME — shows slot cards
        # ══════════════════════════════════════
        self.idle_frame = tk.Frame(root, bg=SCREEN_BG)
        self.idle_frame.place(x=0, y=100, width=SCREEN_W, height=SCREEN_H - 100)

        tk.Label(self.idle_frame, text="MEDICATIONS", fg=SCREEN_MUTED, bg=SCREEN_BG,
                 font=self.f_label).place(x=52, y=10)

        self.slot_cards = {}
        for i, key in enumerate(SLOT_KEYS):
            x = 52 + i * 180
            card = tk.Frame(self.idle_frame, bg=CARD_BG, highlightthickness=0)
            card.place(x=x, y=42, width=168, height=200)

            accent = SLOT_DEFS[key]["accent"]

            tk.Frame(card, bg=accent, height=6).place(x=0, y=0, width=168)

            name_lbl = tk.Label(card, text="—", fg=SCREEN_FG, bg=CARD_BG,
                                font=self.f_btn, anchor="w")
            name_lbl.place(x=14, y=24)

            count_lbl = tk.Label(card, text="0", fg=accent, bg=CARD_BG,
                                 font=self.f_count)
            count_lbl.place(x=14, y=60)

            status_lbl = tk.Label(card, text="NOT LOADED", fg=SCREEN_MUTED,
                                  bg=CARD_BG, font=self.f_hw)
            status_lbl.place(x=14, y=160)

            self.slot_cards[key] = {
                "card": card, "name_lbl": name_lbl,
                "count_lbl": count_lbl, "status_lbl": status_lbl,
            }

        # Scan hint at bottom
        self.scan_hint = tk.Label(self.idle_frame, fg=SCREEN_MUTED, bg=SCREEN_BG,
                                  font=self.f_hint)
        self.scan_hint.place(x=52, y=270)
        self._update_scan_hint()

        # Hardware status
        hw_parts = []
        if not self.has_camera:
            hw_parts.append("Camera not connected")
        if not self.has_touch:
            hw_parts.append("Touch sensor not connected")
        if hw_parts:
            tk.Label(self.idle_frame, text="  ·  ".join(hw_parts),
                     fg="#444444", bg=SCREEN_BG, font=self.f_hw).place(x=52, y=330)

        # Update button
        self.update_btn = tk.Label(self.idle_frame, text="UPDATE",
                                   fg=SCREEN_FG, bg="#1E1E24",
                                   font=self.f_small, padx=14, pady=6,
                                   cursor="hand2")
        self.update_btn.place(x=680, y=270)
        self.update_btn.bind("<Button-1>", lambda _: self._on_update_pressed())

        self.update_status = tk.Label(self.idle_frame, text="", fg=SCREEN_MUTED,
                                      bg=SCREEN_BG, font=self.f_hw)
        self.update_status.place(x=52, y=310)

        # ══════════════════════════════════════
        # QTY CONFIRM FRAME
        # ══════════════════════════════════════
        self.qty_frame = tk.Frame(root, bg=SCREEN_BG)
        self.qty_frame.place(x=0, y=100, width=SCREEN_W, height=SCREEN_H - 100)
        self._qty_slot = None
        self._qty_value = DEFAULT_QTY

        tk.Label(self.qty_frame, text="MEDICATION LOADED", fg=SCREEN_MUTED,
                 bg=SCREEN_BG, font=self.f_label).place(x=52, y=10)

        self.qty_med_name = tk.Label(self.qty_frame, text="", fg=SCREEN_FG,
                                     bg=SCREEN_BG, font=self.f_name)
        self.qty_med_name.place(x=52, y=36)

        tk.Label(self.qty_frame, text="HOW MANY PILLS?", fg=SCREEN_MUTED,
                 bg=SCREEN_BG, font=self.f_label).place(x=52, y=110)

        self.qty_display = tk.Label(self.qty_frame, text="30", fg="#5B9BFF",
                                    bg=SCREEN_BG, font=self.f_count)
        self.qty_display.place(x=52, y=140)

        minus_btn = tk.Label(self.qty_frame, text="−", fg=SCREEN_FG, bg="#1E1E24",
                             font=self.f_big, width=3, cursor="hand2")
        minus_btn.place(x=250, y=140, height=80)
        minus_btn.bind("<Button-1>", lambda _: self._qty_adjust(-1))

        plus_btn = tk.Label(self.qty_frame, text="+", fg=SCREEN_FG, bg="#1E1E24",
                            font=self.f_big, width=3, cursor="hand2")
        plus_btn.place(x=370, y=140, height=80)
        plus_btn.bind("<Button-1>", lambda _: self._qty_adjust(1))

        confirm_qty_btn = tk.Label(self.qty_frame, text="CONFIRM", fg="#FFFFFF",
                                   bg="#3478F6", font=self.f_btn, padx=40, pady=12,
                                   cursor="hand2")
        confirm_qty_btn.place(x=52, y=280)
        confirm_qty_btn.bind("<Button-1>", lambda _: self._qty_commit())

        # ══════════════════════════════════════
        # READ FRAME
        # ══════════════════════════════════════
        self.read_frame = tk.Frame(root, bg=SCREEN_BG)
        self.read_frame.place(x=0, y=100, width=SCREEN_W, height=SCREEN_H - 100)

        self.read_name = tk.Label(self.read_frame, text="", fg=SCREEN_FG, bg=SCREEN_BG,
                                  font=self.f_name, anchor="w")
        self.read_name.place(x=52, y=30)

        tk.Label(self.read_frame, text="TAKE WITH", fg=SCREEN_MUTED, bg=SCREEN_BG,
                 font=self.f_label).place(x=52, y=90)
        self.read_take = tk.Label(self.read_frame, text="", fg=SCREEN_FG, bg=SCREEN_BG,
                                  font=self.f_body, anchor="nw", justify="left",
                                  wraplength=620)
        self.read_take.place(x=52, y=116, width=620)

        self.read_count = tk.Label(self.read_frame, text="", fg=SCREEN_MUTED,
                                   bg=SCREEN_BG, font=self.f_pills)
        self.read_count.place(x=52, y=200)

        self.read_hint = tk.Label(self.read_frame, text="Hold to confirm",
                                  fg=SCREEN_MUTED, bg=SCREEN_BG, font=self.f_hint)
        self.read_hint.place(x=52, y=290)

        # ══════════════════════════════════════
        # HOLD FRAME
        # ══════════════════════════════════════
        self.hold_frame = tk.Frame(root, bg=SCREEN_BG)
        self.hold_frame.place(x=0, y=100, width=SCREEN_W, height=SCREEN_H - 100)

        self.hold_name = tk.Label(self.hold_frame, text="", fg=SCREEN_FG, bg=SCREEN_BG,
                                  font=self.f_name, anchor="w")
        self.hold_name.place(x=52, y=30)

        tk.Label(self.hold_frame, text="HOLD TO CONFIRM", fg=SCREEN_MUTED,
                 bg=SCREEN_BG, font=self.f_label).place(x=52, y=110)
        tk.Label(self.hold_frame, text="Keep holding…", fg=SCREEN_FG,
                 bg=SCREEN_BG, font=self.f_body).place(x=52, y=136)

        self.prog_canvas = tk.Canvas(self.hold_frame, width=620, height=8,
                                     bg=SCREEN_BG, highlightthickness=0)
        self.prog_canvas.place(x=52, y=260)

        # ══════════════════════════════════════
        # CONFIRM FRAME
        # ══════════════════════════════════════
        self.confirm_frame = tk.Frame(root, bg=SCREEN_BG)
        self.confirm_frame.place(x=0, y=100, width=SCREEN_W, height=SCREEN_H - 100)

        tk.Label(self.confirm_frame, text="CONFIRMED", fg=SCREEN_MUTED,
                 bg=SCREEN_BG, font=self.f_label).place(x=52, y=50)
        tk.Label(self.confirm_frame, text="Press down\nto dispense",
                 fg=SCREEN_FG, bg=SCREEN_BG, font=self.f_big,
                 anchor="nw", justify="left").place(x=52, y=80)

        # ══════════════════════════════════════
        # DISPENSED FRAME
        # ══════════════════════════════════════
        self.dispensed_frame = tk.Frame(root, bg=SCREEN_BG)
        self.dispensed_frame.place(x=0, y=100, width=SCREEN_W, height=SCREEN_H - 100)

        tk.Label(self.dispensed_frame, text="DISPENSED", fg=SCREEN_MUTED,
                 bg=SCREEN_BG, font=self.f_label).place(x=52, y=50)
        self.disp_name = tk.Label(self.dispensed_frame, text="", fg=SCREEN_FG,
                                  bg=SCREEN_BG, font=self.f_big, anchor="w")
        self.disp_name.place(x=52, y=80)
        self.disp_remaining = tk.Label(self.dispensed_frame, text="", fg=SCREEN_MUTED,
                                       bg=SCREEN_BG, font=self.f_pills)
        self.disp_remaining.place(x=52, y=140)

        self.strip_frame = tk.Frame(self.dispensed_frame, bg="#1A1A1C")
        self.strip_frame.place(x=0, y=260, width=SCREEN_W, height=50)
        tk.Frame(self.strip_frame, bg="#2A2A2E", height=1).place(x=0, y=0,
                                                                  width=SCREEN_W)
        self.strip_label = tk.Label(self.strip_frame, text="", fg=SCREEN_FG,
                                    bg="#1A1A1C", font=self.f_strip, anchor="w")
        self.strip_label.place(x=52, y=14)

        # ── Camera preview ──
        self.preview_label = tk.Label(root, bg=SCREEN_BG, highlightthickness=0)

        # ── Start ──
        self._go_idle()
        self._tick_clock()
        self._init_camera()
        if self.has_touch:
            self._poll_touch()
        # Auto-check for updates on launch
        self.root.after(2000, lambda: self._do_update_check(silent=True))

    # ── Med data management ──────────────────────────────────────────
    def _init_med_data(self):
        saved = _load_data()
        for key in SLOT_KEYS:
            if key in saved:
                self.med_data[key] = saved[key]
            else:
                self.med_data[key] = {
                    "name": key.capitalize(),
                    "count": 0,
                    "loaded": False,
                    "take_with": "",
                }

    def _save(self):
        _save_data(self.med_data)

    def _is_loaded(self, key):
        return self.med_data.get(key, {}).get("loaded", False)

    def _get_count(self, key):
        return self.med_data.get(key, {}).get("count", 0)

    # ── UI refresh ───────────────────────────────────────────────────
    def _refresh_cards(self):
        for key in SLOT_KEYS:
            c = self.slot_cards[key]
            md = self.med_data[key]
            accent = SLOT_DEFS[key]["accent"]
            if md["loaded"]:
                c["name_lbl"].configure(text=md["name"])
                c["count_lbl"].configure(text=str(md["count"]))
                if md["count"] > 0:
                    c["status_lbl"].configure(text="READY", fg=accent)
                else:
                    c["status_lbl"].configure(text="EMPTY", fg="#FF6B6B")
            else:
                c["name_lbl"].configure(text="—")
                c["count_lbl"].configure(text="0")
                c["status_lbl"].configure(text="NOT LOADED", fg=SCREEN_MUTED)

    def _update_scan_hint(self):
        loaded = sum(1 for k in SLOT_KEYS if self._is_loaded(k))
        if loaded == 0:
            self.scan_hint.configure(text="Scan a medication QR to load a slot")
        elif loaded < 4:
            self.scan_hint.configure(text=f"{loaded}/4 loaded — scan more QR codes")
        else:
            self.scan_hint.configure(text="All slots loaded")

    # ── Safe raise helper ────────────────────────────────────────────
    def _show_frame(self, frame):
        _raise(frame)
        _raise(self.wm_label)

    # ── State transitions ────────────────────────────────────────────
    def _go_idle(self):
        self.state = S_IDLE
        self.current_med = None
        self._refresh_cards()
        self._update_scan_hint()
        self._show_frame(self.idle_frame)

    def _go_qty_confirm(self, slot_key, med_name):
        self.state = S_QTY_CONFIRM
        self._qty_slot = slot_key
        md = self.med_data[slot_key]
        if md["loaded"] and md["count"] > 0:
            self._qty_value = md["count"]
        else:
            self._qty_value = DEFAULT_QTY
        accent = SLOT_DEFS[slot_key]["accent"]
        self.qty_med_name.configure(text=med_name, fg=accent)
        self.qty_display.configure(text=str(self._qty_value), fg=accent)
        self._show_frame(self.qty_frame)

    def _qty_adjust(self, delta):
        self._qty_value = max(1, self._qty_value + delta)
        accent = SLOT_DEFS.get(self._qty_slot, {}).get("accent", "#5B9BFF")
        self.qty_display.configure(text=str(self._qty_value), fg=accent)

    def _qty_commit(self):
        key = self._qty_slot
        if key and key in self.med_data:
            self.med_data[key]["count"] = self._qty_value
            self.med_data[key]["loaded"] = True
            self._save()
        self._go_idle()

    def _go_read(self, key):
        md = self.med_data[key]
        if not md["loaded"] or md["count"] <= 0:
            return
        self.state = S_READ
        self.current_med = key
        accent = SLOT_DEFS[key]["accent"]
        self.read_name.configure(text=md["name"], fg=accent)
        self.read_take.configure(text=md.get("take_with", ""))
        self.read_count.configure(text=f"{md['count']} left")
        self._show_frame(self.read_frame)

    def _go_hold(self):
        if not self.current_med:
            return
        self.state = S_HOLD
        self.hold_start = time.monotonic()
        md = self.med_data[self.current_med]
        accent = SLOT_DEFS[self.current_med]["accent"]
        self.hold_name.configure(text=md["name"], fg=accent)
        self._show_frame(self.hold_frame)
        self._update_progress()

    def _go_confirm(self):
        self.state = S_CONFIRM
        self._show_frame(self.confirm_frame)

    def _go_dispensed(self):
        self.state = S_DISPENSED
        key = self.current_med
        md = self.med_data[key]

        if md["count"] > 0:
            md["count"] -= 1
        if md["count"] <= 0:
            md["loaded"] = False
            md["count"] = 0
        self._save()

        accent = SLOT_DEFS[key]["accent"]
        self.disp_name.configure(text=md["name"], fg=accent)
        remaining = md["count"]
        if remaining > 0:
            self.disp_remaining.configure(text=f"{remaining} remaining")
        else:
            self.disp_remaining.configure(text="Slot now empty — scan to reload")
        self.strip_label.configure(
            text=f"Dispensed: {md['name']}  ·  please check before taking")
        self._show_frame(self.dispensed_frame)
        self.root.after(int(DISPENSED_TIME * 1000), self._dispensed_timeout)

    def _dispensed_timeout(self):
        if self.state == S_DISPENSED:
            self._go_idle()

    # ── Progress bar ─────────────────────────────────────────────────
    def _update_progress(self):
        if self.state != S_HOLD:
            return
        elapsed = time.monotonic() - self.hold_start
        frac = min(elapsed / HOLD_TIME, 1.0)
        accent = SLOT_DEFS.get(self.current_med, {}).get("accent", "#5B9BFF")

        self.prog_canvas.delete("all")
        self.prog_canvas.create_rectangle(0, 0, 620, 8, fill="#2A2A2E", outline="")
        self.prog_canvas.create_rectangle(0, 0, int(620 * frac), 8,
                                          fill=accent, outline="")
        if frac >= 1.0:
            self._go_confirm()
        else:
            self.root.after(30, self._update_progress)

    # ── Tap handling ─────────────────────────────────────────────────
    def _on_tap(self, event=None):
        if self.state == S_IDLE:
            if not self.has_camera:
                for key in SLOT_KEYS:
                    if self._is_loaded(key) and self._get_count(key) > 0:
                        self._go_read(key)
                        return
        elif self.state == S_READ:
            self._go_hold()
        elif self.state == S_CONFIRM:
            self._go_dispensed()
        elif self.state == S_DISPENSED:
            self._go_idle()

    # ── MPR121 polling ───────────────────────────────────────────────
    def _poll_touch(self):
        if not self.mpr:
            return
        try:
            for key in SLOT_KEYS:
                pad = SLOT_DEFS[key]["pad"]
                touched = self.mpr[pad].value
                was = self.mpr_prev[pad]
                if touched and not was:
                    self._on_pad_press(key)
                self.mpr_prev[pad] = touched
        except Exception:
            pass
        self.root.after(50, self._poll_touch)

    def _on_pad_press(self, key):
        if self.state == S_IDLE:
            if self._is_loaded(key) and self._get_count(key) > 0:
                self._go_read(key)
        elif self.state == S_READ and self.current_med == key:
            self._go_hold()
        elif self.state == S_CONFIRM:
            self._go_dispensed()

    # ── Camera / QR scanning ─────────────────────────────────────────
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
            self.root.after(200, self._scan_loop)
        except Exception:
            self.has_camera = False
            self.cam = None

    def _scan_loop(self):
        if not self.cam:
            return
        try:
            arr = self.cam.capture_array()
            img = Image.fromarray(arr[:, :, ::-1])

            for code in qr_decode(img):
                text = code.data.decode("utf-8", "ignore").strip()
                self._handle_qr(text)
                break

            if self.show_preview:
                disp = img.resize((200, 120))
                self.photo = ImageTk.PhotoImage(disp)
                self.preview_label.configure(image=self.photo)
        except Exception:
            pass

        self.root.after(SCAN_INTERVAL, self._scan_loop)

    def _handle_qr(self, raw_text):
        """Parse QR payload and route to the right slot."""
        try:
            payload = json.loads(raw_text)
            slot = payload.get("slot", "").lower()
            med_name = payload.get("med", slot.capitalize())
        except (json.JSONDecodeError, AttributeError):
            slot = raw_text.strip().lower()
            med_name = slot.capitalize()

        if slot not in SLOT_DEFS:
            return

        md = self.med_data[slot]

        if self.state == S_IDLE:
            if not md["loaded"] or md["count"] <= 0:
                md["name"] = med_name
                md["take_with"] = ""
                self._save()
                self._go_qty_confirm(slot, med_name)
            else:
                self._go_read(slot)

    # ── Auto-update ──────────────────────────────────────────────────
    def _on_update_pressed(self):
        self.update_btn.configure(text="CHECKING…", bg="#333338")
        self.update_status.configure(text="Checking for updates…")
        threading.Thread(target=self._do_update_check, daemon=True).start()

    def _do_update_check(self, silent=False):
        try:
            resp = urlopen(RAW_URL, timeout=10)
            remote_code = resp.read()
        except Exception:
            if not silent:
                self.root.after(0, self._update_result,
                                "No internet — try later", False)
            return

        local_hash = ""
        try:
            with open(APP_FILE, "rb") as f:
                local_hash = hashlib.md5(f.read()).hexdigest()
        except FileNotFoundError:
            pass

        remote_hash = hashlib.md5(remote_code).hexdigest()
        if local_hash == remote_hash:
            if not silent:
                self.root.after(0, self._update_result, "Already up to date", False)
            return

        try:
            os.makedirs(APP_DIR, exist_ok=True)
            with open(APP_FILE, "wb") as f:
                f.write(remote_code)
        except Exception:
            if not silent:
                self.root.after(0, self._update_result, "Write failed", False)
            return

        self.root.after(0, self._update_result, "Updated! Restarting…", True)

    def _update_result(self, msg, needs_restart):
        self.update_btn.configure(text="UPDATE", bg="#1E1E24")
        self.update_status.configure(text=msg)
        if needs_restart:
            self.root.after(1500, self._restart_app)

    def _restart_app(self):
        try:
            if self.cam:
                self.cam.stop()
        except Exception:
            pass
        self.root.destroy()
        os.execv(sys.executable, [sys.executable, APP_FILE])

    # ── Preview toggle ───────────────────────────────────────────────
    def _toggle_preview(self):
        if not self.has_camera:
            return
        self.show_preview = not self.show_preview
        if self.show_preview:
            self.preview_label.place(x=24, y=340, width=200, height=120)
            _raise(self.preview_label)
        else:
            self.preview_label.place_forget()

    # ── Clock ────────────────────────────────────────────────────────
    def _tick_clock(self):
        try:
            self.clock_label.configure(text=time.strftime("%-I:%M %p"))
        except ValueError:
            self.clock_label.configure(text=time.strftime("%I:%M %p").lstrip("0"))
        self.root.after(1000, self._tick_clock)

    # ── WiFi icon ────────────────────────────────────────────────────
    def _draw_wifi(self, canvas, color):
        cx, cy = 19, 26
        for r in (16, 11, 6):
            canvas.create_arc(cx - r, cy - r, cx + r, cy + r,
                              start=55, extent=70, style="arc",
                              outline=color, width=3)
        canvas.create_oval(cx - 2, cy - 2, cx + 2, cy + 2,
                           fill=color, outline=color)

    # ── Quit ─────────────────────────────────────────────────────────
    def _quit(self):
        try:
            if self.cam:
                self.cam.stop()
        except Exception:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    app = DoseApp(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        err = traceback.format_exc()
        log_path = os.path.expanduser("~/dose-home-station/crash.log")
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "w") as f:
                f.write(err)
        except Exception:
            pass
        print(f"\n  DOSE crashed:\n\n{err}")
        print(f"  Log: {log_path}")
        print("  Press Enter to close...")
        try:
            input()
        except Exception:
            pass
