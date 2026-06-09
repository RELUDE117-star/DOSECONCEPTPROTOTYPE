#!/usr/bin/env python3
"""
DOSE Home Station — Prototype Application
Braun/Dieter Rams inspired medication dispensing kiosk.

Modes: Standby, Storage, Settings
Dispensing: READ → HOLD → CONFIRM → DISPENSED

Gracefully handles missing hardware (camera, pyzbar, PIL).
"""

import tkinter as tk
import tkinter.font as tkfont
import json
import os
import subprocess
import sys
import time
import threading
import math
import base64
import io
from datetime import datetime

# ── Graceful optional imports ──────────────────────────────────────────────
PIL_AVAILABLE = False
try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except Exception:
    pass

CAMERA_AVAILABLE = False
try:
    from picamera2 import Picamera2
    from pyzbar.pyzbar import decode as pyzbar_decode
    if PIL_AVAILABLE:
        CAMERA_AVAILABLE = True
except Exception:
    pass

# ── Module-level constants ─────────────────────────────────────────────────
SCREEN_W = 800
SCREEN_H = 480
MARGIN_LEFT = 52
HOLD_TIME = 3.0

ALL_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
CONFIG_PATH = os.path.expanduser("~/.dose_config.json")
APP_DIR = os.path.expanduser("~/dose-home-station")
RAW_URL = "https://raw.githubusercontent.com/relude117-star/doseconceptprototype/claude/quirky-brown-vkHwi"

DEFAULT_MEDS = {
    "blue": {
        "name": "Lisinopril",
        "accent": "#4A90D9",
        "pills_left": 28,
        "schedule_time": "8:00 AM",
        "schedule_days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "take_with": "Take with water",
        "safety": "Do not take with potassium supplements."
    },
    "red": {
        "name": "Metformin",
        "accent": "#D94A4A",
        "pills_left": 56,
        "schedule_time": "9:00 AM",
        "schedule_days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "take_with": "Take with food",
        "safety": "Avoid alcohol while taking this medication."
    },
    "green": {
        "name": "Atorvastatin",
        "accent": "#4AD97A",
        "pills_left": 30,
        "schedule_time": "10:00 PM",
        "schedule_days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "take_with": "Take at bedtime",
        "safety": "Avoid grapefruit juice."
    },
    "yellow": {
        "name": "Vitamin D",
        "accent": "#D9C84A",
        "pills_left": 90,
        "schedule_time": "8:00 AM",
        "schedule_days": ["Mon", "Wed", "Fri"],
        "take_with": "Take with food",
        "safety": "Do not exceed recommended dose."
    }
}

DARK_THEME = {
    "bg": "#070708",
    "fg": "#F4F4F2",
    "muted": "#7E8186",
    "card_bg": "#141418",
    "btn_bg": "#1E1E24",
    "btn_active": "#2A2A32",
    "popup_bg": "#1A1A20"
}

LIGHT_THEME = {
    "bg": "#F4F4F2",
    "fg": "#1A1A1C",
    "muted": "#888888",
    "card_bg": "#E8E8E6",
    "btn_bg": "#DCDCDA",
    "btn_active": "#D0D0CE",
    "popup_bg": "#E0E0DE"
}

DOSE_LOGO_B64 = (
    "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"
    "DQXm1BIVyT4AAAAASUVORK5CYII="
)


# ═══════════════════════════════════════════════════════════════════════════
#  DoseApp
# ═══════════════════════════════════════════════════════════════════════════
class DoseApp:

    # ── Constructor ────────────────────────────────────────────────────────
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("DOSE")
        self.root.geometry(f"{SCREEN_W}x{SCREEN_H}+0+0")
        self.root.attributes("-fullscreen", True)
        self.root.configure(cursor="none")
        self.root.resizable(False, False)

        # ── Fonts ──────────────────────────────────────────────────────────
        preferred = "Nunito"
        fallback = "DejaVu Sans"
        families = tkfont.families(self.root)
        base = preferred if preferred in families else fallback

        self.font_clock = tkfont.Font(family=base, size=32)
        self.font_hero = tkfont.Font(family=base, size=44, weight="bold")
        self.font_name = tkfont.Font(family=base, size=36, weight="bold")
        self.font_body = tkfont.Font(family=base, size=20)
        self.font_body_bold = tkfont.Font(family=base, size=20, weight="bold")
        self.font_label = tkfont.Font(family=base, size=11, weight="bold")
        self.font_small = tkfont.Font(family=base, size=14)
        self.font_small_bold = tkfont.Font(family=base, size=14, weight="bold")
        self.font_btn = tkfont.Font(family=base, size=16, weight="bold")
        self.font_btn_lg = tkfont.Font(family=base, size=18, weight="bold")
        self.font_title = tkfont.Font(family=base, size=24, weight="bold")
        self.font_medium = tkfont.Font(family=base, size=18)
        self.font_bold_lg = tkfont.Font(family=base, size=28, weight="bold")

        # ── State ──────────────────────────────────────────────────────────
        self.meds = {}
        self.settings = {"night_mode": False, "alarm_sound": True}
        self.theme = dict(DARK_THEME)
        self.mode = "standby"
        self.selected_pill = "blue"
        self.dispense_state = 0
        self.dispense_pill = None
        self.hold_start = 0
        self.hold_after_id = None
        self.menu_visible = False
        self.menu_dismiss_id = None
        self.camera = None
        self.camera_running = False

        # ── Config ─────────────────────────────────────────────────────────
        self._load_config()
        self._apply_theme_colors()

        # ── Root frames ────────────────────────────────────────────────────
        self.main_frame = tk.Frame(self.root, bg=self.theme["bg"],
                                   width=SCREEN_W, height=SCREEN_H)
        self.main_frame.place(x=0, y=0, width=SCREEN_W, height=SCREEN_H)

        self.overlay_frame = tk.Frame(self.root, bg=self.theme["bg"],
                                      width=SCREEN_W, height=SCREEN_H)

        # D button canvas — 48x48 in bottom-right
        self.d_btn_canvas = tk.Canvas(self.root, width=48, height=48,
                                      highlightthickness=0,
                                      bg=self.theme["bg"], bd=0)
        self.d_btn_canvas.place(x=740, y=424)
        self._draw_d_button()
        self.d_btn_canvas.bind("<Button-1>", self._on_d_pressed)

        # Popup menu frame
        self.popup_frame = tk.Frame(self.root, bg=self.theme["popup_bg"],
                                    highlightbackground=self.theme["muted"],
                                    highlightthickness=1)

        # ── Bindings ───────────────────────────────────────────────────────
        self.root.bind("<Escape>", lambda e: self.root.destroy())

        # ── Build all modes ────────────────────────────────────────────────
        self._build_standby()
        self._build_storage()
        self._build_settings()
        self._build_overlay()

        # ── Show initial mode ──────────────────────────────────────────────
        self._show_mode("standby")
        self._tick_clock()

        if CAMERA_AVAILABLE:
            self._start_camera()

        self.root.focus_force()

    # ── Safe widget raising ────────────────────────────────────────────────
    @staticmethod
    def _raise_widget(widget):
        widget.tk.call('raise', widget._w)

    # ── Config persistence ─────────────────────────────────────────────────
    def _load_config(self):
        loaded = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r") as f:
                    loaded = json.load(f)
            except Exception:
                pass

        saved_meds = loaded.get("meds", {})
        self.meds = {}
        for key, defaults in DEFAULT_MEDS.items():
            entry = dict(defaults)
            if key in saved_meds:
                for k, v in saved_meds[key].items():
                    entry[k] = v
            self.meds[key] = entry

        saved_settings = loaded.get("settings", {})
        for k in self.settings:
            if k in saved_settings:
                self.settings[k] = saved_settings[k]

    def _save_config(self):
        data = {"meds": self.meds, "settings": self.settings}
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    # ── Theme ──────────────────────────────────────────────────────────────
    def _apply_theme_colors(self):
        if self.settings.get("night_mode", False):
            self.theme = dict(LIGHT_THEME)
        else:
            self.theme = dict(DARK_THEME)

    def _apply_theme(self):
        self._apply_theme_colors()
        self.root.configure(bg=self.theme["bg"])
        self.main_frame.configure(bg=self.theme["bg"])
        self.overlay_frame.configure(bg=self.theme["bg"])
        self.d_btn_canvas.configure(bg=self.theme["bg"])
        self.popup_frame.configure(bg=self.theme["popup_bg"],
                                   highlightbackground=self.theme["muted"])
        self._draw_d_button()
        # Rebuild everything for deep recolor
        self._build_standby()
        self._build_storage()
        self._build_settings()
        self._build_overlay()
        self._show_mode(self.mode)

    # ── D Button drawing ───────────────────────────────────────────────────
    def _draw_d_button(self):
        self.d_btn_canvas.delete("all")
        loaded = False
        try:
            if PIL_AVAILABLE:
                raw = base64.b64decode(DOSE_LOGO_B64)
                img = Image.open(io.BytesIO(raw))
                resample = getattr(Image, 'LANCZOS',
                                   getattr(Image, 'ANTIALIAS', None))
                img = img.resize((48, 48), resample)
                self._d_logo_img = ImageTk.PhotoImage(img)
                self.d_btn_canvas.create_image(24, 24,
                                               image=self._d_logo_img)
                loaded = True
        except Exception:
            pass
        if not loaded:
            self._canvas_rounded_rect(self.d_btn_canvas, 2, 2, 46, 46,
                                      12, fill="#4A90D9", outline="")
            self.d_btn_canvas.create_text(24, 24, text="D",
                                          fill="white",
                                          font=self.font_btn_lg)

    # ── D Menu ─────────────────────────────────────────────────────────────
    def _on_d_pressed(self, event=None):
        if self.menu_visible:
            self._hide_menu()
        else:
            self._show_menu()

    def _show_menu(self):
        self.menu_visible = True
        # Clear and build menu
        for w in self.popup_frame.winfo_children():
            w.destroy()

        items = [("Standby", "standby"), ("Storage", "storage"),
                 ("Settings", "settings")]
        for label, mode_val in items:
            btn = tk.Button(self.popup_frame, text=label,
                            font=self.font_small_bold,
                            bg=self.theme["popup_bg"],
                            fg=self.theme["fg"],
                            activebackground=self.theme["btn_active"],
                            activeforeground=self.theme["fg"],
                            bd=0, padx=20, pady=10, anchor="w",
                            command=lambda m=mode_val: self._menu_pick(m))
            btn.pack(fill="x")

        self.popup_frame.place(x=640, y=340, width=150)
        self._raise_widget(self.popup_frame)

        if self.menu_dismiss_id:
            self.root.after_cancel(self.menu_dismiss_id)
        self.menu_dismiss_id = self.root.after(45000, self._hide_menu)

    def _hide_menu(self):
        self.menu_visible = False
        self.popup_frame.place_forget()
        if self.menu_dismiss_id:
            self.root.after_cancel(self.menu_dismiss_id)
            self.menu_dismiss_id = None

    def _menu_pick(self, mode_val):
        self._hide_menu()
        if self.dispense_state > 0:
            self._end_dispense()
        self._show_mode(mode_val)

    # ── Mode switching ─────────────────────────────────────────────────────
    def _show_mode(self, mode):
        self.mode = mode
        self.standby_frame.place_forget()
        self.storage_frame.place_forget()
        self.settings_frame.place_forget()
        self.overlay_frame.place_forget()

        if mode == "standby":
            self.standby_frame.place(x=0, y=0,
                                     width=SCREEN_W, height=SCREEN_H)
            self._update_standby()
        elif mode == "storage":
            self.storage_frame.place(x=0, y=0,
                                     width=SCREEN_W, height=SCREEN_H)
            self._update_storage()
        elif mode == "settings":
            self.settings_frame.place(x=0, y=0,
                                      width=SCREEN_W, height=SCREEN_H)

        self._raise_widget(self.d_btn_canvas)

    # ── Clock ──────────────────────────────────────────────────────────────
    def _tick_clock(self):
        try:
            now_str = datetime.now().strftime("%-I:%M %p")
        except ValueError:
            now_str = datetime.now().strftime("%I:%M %p").lstrip("0")
        try:
            self.clock_label.configure(text=now_str)
        except Exception:
            pass
        self.root.after(1000, self._tick_clock)

    # ── Canvas helper ──────────────────────────────────────────────────────
    @staticmethod
    def _canvas_rounded_rect(canvas, x1, y1, x2, y2, r, **kwargs):
        points = [
            x1 + r, y1,
            x1 + r, y1,
            x2 - r, y1,
            x2 - r, y1,
            x2, y1,
            x2, y1 + r,
            x2, y1 + r,
            x2, y2 - r,
            x2, y2 - r,
            x2, y2,
            x2 - r, y2,
            x2 - r, y2,
            x1 + r, y2,
            x1 + r, y2,
            x1, y2,
            x1, y2 - r,
            x1, y2 - r,
            x1, y1 + r,
            x1, y1 + r,
            x1, y1,
        ]
        return canvas.create_polygon(points, smooth=True, **kwargs)

    # ══════════════════════════════════════════════════════════════════════
    #  STANDBY MODE
    # ══════════════════════════════════════════════════════════════════════
    def _build_standby(self):
        if hasattr(self, 'standby_frame'):
            self.standby_frame.destroy()

        self.standby_frame = tk.Frame(self.main_frame,
                                      bg=self.theme["bg"],
                                      width=SCREEN_W, height=SCREEN_H)

        # Clock
        self.clock_label = tk.Label(self.standby_frame, text="",
                                    font=self.font_clock,
                                    bg=self.theme["bg"],
                                    fg=self.theme["fg"])
        self.clock_label.place(x=MARGIN_LEFT, y=30)

        # WiFi icon (text placeholder)
        wifi_lbl = tk.Label(self.standby_frame, text="◉",
                            font=self.font_body,
                            bg=self.theme["bg"],
                            fg=self.theme["muted"])
        wifi_lbl.place(x=SCREEN_W - MARGIN_LEFT - 30, y=35)

        # NEXT DOSE label
        next_lbl = tk.Label(self.standby_frame, text="NEXT DOSE",
                            font=self.font_label,
                            bg=self.theme["bg"],
                            fg=self.theme["muted"])
        next_lbl.place(x=MARGIN_LEFT, y=120)

        # Next dose time (hero)
        self.next_dose_label = tk.Label(self.standby_frame, text="",
                                        font=self.font_hero,
                                        bg=self.theme["bg"],
                                        fg=self.theme["fg"])
        self.next_dose_label.place(x=MARGIN_LEFT, y=148)

        # Pill count
        self.standby_pill_count = tk.Label(self.standby_frame, text="",
                                           font=self.font_body,
                                           bg=self.theme["bg"],
                                           fg=self.theme["muted"])
        self.standby_pill_count.place(x=MARGIN_LEFT, y=210)

        # Camera status
        if not CAMERA_AVAILABLE:
            cam_lbl = tk.Label(self.standby_frame,
                               text="Camera not connected",
                               font=self.font_small,
                               bg=self.theme["bg"],
                               fg=self.theme["muted"])
            cam_lbl.place(x=MARGIN_LEFT, y=280)

        # Tap to demo dispense
        self.standby_frame.bind("<Button-1>", self._standby_tap)

    def _standby_tap(self, event=None):
        self._start_dispense("blue")

    def _update_standby(self):
        next_time, next_name, total_pills = self._compute_next_dose()
        self.next_dose_label.configure(text=next_time)
        self.standby_pill_count.configure(
            text=f"{total_pills} pills remaining  •  {next_name}")

    def _compute_next_dose(self):
        now = datetime.now()
        today_name = now.strftime("%a")
        best_time = None
        best_name = ""
        total = 0
        for key, med in self.meds.items():
            total += med.get("pills_left", 0)
            sched = med.get("schedule_time", "8:00 AM")
            days = med.get("schedule_days", ALL_DAYS)
            if today_name in days:
                try:
                    t = datetime.strptime(sched, "%I:%M %p").replace(
                        year=now.year, month=now.month, day=now.day)
                    if t > now:
                        if best_time is None or t < best_time:
                            best_time = t
                            best_name = med["name"]
                except Exception:
                    pass
        if best_time:
            try:
                display = best_time.strftime("%-I:%M %p")
            except ValueError:
                display = best_time.strftime("%I:%M %p").lstrip("0")
        else:
            display = "No more today"
        return display, best_name, total

    # ══════════════════════════════════════════════════════════════════════
    #  STORAGE MODE
    # ══════════════════════════════════════════════════════════════════════
    def _build_storage(self):
        if hasattr(self, 'storage_frame'):
            self.storage_frame.destroy()

        self.storage_frame = tk.Frame(self.main_frame,
                                      bg=self.theme["bg"],
                                      width=SCREEN_W, height=SCREEN_H)

        # Left panel — pill list
        self.storage_left = tk.Frame(self.storage_frame,
                                     bg=self.theme["bg"],
                                     width=280, height=SCREEN_H)
        self.storage_left.place(x=0, y=0, width=280, height=SCREEN_H)
        self.storage_left.pack_propagate(False)

        title = tk.Label(self.storage_left, text="STORAGE",
                         font=self.font_label,
                         bg=self.theme["bg"],
                         fg=self.theme["muted"])
        title.pack(anchor="w", padx=MARGIN_LEFT, pady=(30, 20))

        self.pill_buttons = {}
        for pill_key in ["blue", "red", "green", "yellow"]:
            med = self.meds[pill_key]
            row = tk.Frame(self.storage_left, bg=self.theme["bg"])
            row.pack(fill="x", padx=(MARGIN_LEFT, 10), pady=6)

            dot_canvas = tk.Canvas(row, width=16, height=16,
                                   bg=self.theme["bg"],
                                   highlightthickness=0)
            dot_canvas.pack(side="left", padx=(0, 10))
            dot_canvas.create_oval(2, 2, 14, 14,
                                   fill=med["accent"], outline="")

            btn = tk.Button(row, text=med["name"],
                            font=self.font_small_bold,
                            bg=self.theme["bg"],
                            fg=self.theme["fg"],
                            activebackground=self.theme["btn_active"],
                            activeforeground=self.theme["fg"],
                            bd=0, anchor="w",
                            command=lambda k=pill_key: self._select_pill(k))
            btn.pack(side="left", fill="x", expand=True)
            self.pill_buttons[pill_key] = btn

        # Right panel — detail
        self.storage_right = tk.Frame(self.storage_frame,
                                      bg=self.theme["card_bg"],
                                      width=520, height=SCREEN_H)
        self.storage_right.place(x=280, y=0, width=520, height=SCREEN_H)
        self.storage_right.pack_propagate(False)

        self._build_storage_detail()

    def _build_storage_detail(self):
        for w in self.storage_right.winfo_children():
            w.destroy()

        med = self.meds[self.selected_pill]
        pad_x = 30

        # Pill name
        name_lbl = tk.Label(self.storage_right, text=med["name"],
                            font=self.font_name,
                            bg=self.theme["card_bg"],
                            fg=med["accent"])
        name_lbl.pack(anchor="w", padx=pad_x, pady=(28, 2))

        # Pill count
        count_lbl = tk.Label(self.storage_right,
                             text=f"{med['pills_left']} pills remaining",
                             font=self.font_body,
                             bg=self.theme["card_bg"],
                             fg=self.theme["muted"])
        count_lbl.pack(anchor="w", padx=pad_x)

        # Schedule time picker
        sched_label = tk.Label(self.storage_right, text="SCHEDULE",
                               font=self.font_label,
                               bg=self.theme["card_bg"],
                               fg=self.theme["muted"])
        sched_label.pack(anchor="w", padx=pad_x, pady=(18, 6))

        time_frame = tk.Frame(self.storage_right, bg=self.theme["card_bg"])
        time_frame.pack(anchor="w", padx=pad_x)

        # Parse current schedule time
        parts = med["schedule_time"].split()
        time_parts = parts[0].split(":")
        self._sched_hour = int(time_parts[0])
        self._sched_min = int(time_parts[1])
        self._sched_ampm = parts[1] if len(parts) > 1 else "AM"

        # Hour
        tk.Button(time_frame, text="▲", font=self.font_small,
                  bg=self.theme["btn_bg"], fg=self.theme["fg"],
                  activebackground=self.theme["btn_active"], bd=0,
                  width=3,
                  command=lambda: self._adjust_schedule("hour", 1)
                  ).grid(row=0, column=0, padx=2)
        self.hour_label = tk.Label(time_frame,
                                   text=str(self._sched_hour),
                                   font=self.font_body_bold,
                                   bg=self.theme["card_bg"],
                                   fg=self.theme["fg"], width=3)
        self.hour_label.grid(row=1, column=0, padx=2)
        tk.Button(time_frame, text="▼", font=self.font_small,
                  bg=self.theme["btn_bg"], fg=self.theme["fg"],
                  activebackground=self.theme["btn_active"], bd=0,
                  width=3,
                  command=lambda: self._adjust_schedule("hour", -1)
                  ).grid(row=2, column=0, padx=2)

        tk.Label(time_frame, text=":", font=self.font_body_bold,
                 bg=self.theme["card_bg"],
                 fg=self.theme["fg"]).grid(row=1, column=1)

        # Minute
        tk.Button(time_frame, text="▲", font=self.font_small,
                  bg=self.theme["btn_bg"], fg=self.theme["fg"],
                  activebackground=self.theme["btn_active"], bd=0,
                  width=3,
                  command=lambda: self._adjust_schedule("min", 1)
                  ).grid(row=0, column=2, padx=2)
        self.min_label = tk.Label(time_frame,
                                  text=f"{self._sched_min:02d}",
                                  font=self.font_body_bold,
                                  bg=self.theme["card_bg"],
                                  fg=self.theme["fg"], width=3)
        self.min_label.grid(row=1, column=2, padx=2)
        tk.Button(time_frame, text="▼", font=self.font_small,
                  bg=self.theme["btn_bg"], fg=self.theme["fg"],
                  activebackground=self.theme["btn_active"], bd=0,
                  width=3,
                  command=lambda: self._adjust_schedule("min", -1)
                  ).grid(row=2, column=2, padx=2)

        # AM/PM
        tk.Button(time_frame, text="▲", font=self.font_small,
                  bg=self.theme["btn_bg"], fg=self.theme["fg"],
                  activebackground=self.theme["btn_active"], bd=0,
                  width=3,
                  command=lambda: self._adjust_schedule("ampm", 1)
                  ).grid(row=0, column=3, padx=(10, 2))
        self.ampm_label = tk.Label(time_frame,
                                   text=self._sched_ampm,
                                   font=self.font_body_bold,
                                   bg=self.theme["card_bg"],
                                   fg=self.theme["fg"], width=3)
        self.ampm_label.grid(row=1, column=3, padx=(10, 2))
        tk.Button(time_frame, text="▼", font=self.font_small,
                  bg=self.theme["btn_bg"], fg=self.theme["fg"],
                  activebackground=self.theme["btn_active"], bd=0,
                  width=3,
                  command=lambda: self._adjust_schedule("ampm", -1)
                  ).grid(row=2, column=3, padx=(10, 2))

        # Day-of-week selector
        day_label = tk.Label(self.storage_right, text="DAYS",
                             font=self.font_label,
                             bg=self.theme["card_bg"],
                             fg=self.theme["muted"])
        day_label.pack(anchor="w", padx=pad_x, pady=(14, 6))

        day_frame = tk.Frame(self.storage_right, bg=self.theme["card_bg"])
        day_frame.pack(anchor="w", padx=pad_x)

        self.day_buttons = {}
        for d in ALL_DAYS:
            active = d in med.get("schedule_days", [])
            bg = med["accent"] if active else self.theme["btn_bg"]
            fg = "#FFFFFF" if active else self.theme["fg"]
            btn = tk.Button(day_frame, text=d[:2], font=self.font_label,
                            bg=bg, fg=fg,
                            activebackground=self.theme["btn_active"],
                            bd=0, width=3, padx=2, pady=4,
                            command=lambda dd=d: self._toggle_day(dd))
            btn.pack(side="left", padx=2)
            self.day_buttons[d] = btn

        # Instructions
        inst_lbl = tk.Label(self.storage_right,
                            text=med.get("take_with", ""),
                            font=self.font_small,
                            bg=self.theme["card_bg"],
                            fg=self.theme["fg"])
        inst_lbl.pack(anchor="w", padx=pad_x, pady=(18, 2))

        # Safety
        safety_lbl = tk.Label(self.storage_right,
                              text=med.get("safety", ""),
                              font=self.font_small,
                              bg=self.theme["card_bg"],
                              fg=self.theme["muted"])
        safety_lbl.pack(anchor="w", padx=pad_x, pady=(2, 10))

    def _select_pill(self, key):
        self.selected_pill = key
        self._build_storage_detail()
        self._update_storage()

    def _update_storage(self):
        for key, btn in self.pill_buttons.items():
            if key == self.selected_pill:
                btn.configure(bg=self.theme["btn_bg"])
            else:
                btn.configure(bg=self.theme["bg"])

    def _adjust_schedule(self, field, delta):
        if field == "hour":
            self._sched_hour += delta
            if self._sched_hour > 12:
                self._sched_hour = 1
            elif self._sched_hour < 1:
                self._sched_hour = 12
            self.hour_label.configure(text=str(self._sched_hour))
        elif field == "min":
            self._sched_min += delta * 5
            if self._sched_min >= 60:
                self._sched_min = 0
            elif self._sched_min < 0:
                self._sched_min = 55
            self.min_label.configure(text=f"{self._sched_min:02d}")
        elif field == "ampm":
            self._sched_ampm = "PM" if self._sched_ampm == "AM" else "AM"
            self.ampm_label.configure(text=self._sched_ampm)

        new_time = f"{self._sched_hour}:{self._sched_min:02d} {self._sched_ampm}"
        self.meds[self.selected_pill]["schedule_time"] = new_time
        self._save_config()

    def _toggle_day(self, day):
        days = self.meds[self.selected_pill].get("schedule_days", [])
        if day in days:
            days.remove(day)
        else:
            days.append(day)
        self.meds[self.selected_pill]["schedule_days"] = days
        self._save_config()

        med = self.meds[self.selected_pill]
        for d, btn in self.day_buttons.items():
            active = d in days
            bg = med["accent"] if active else self.theme["btn_bg"]
            fg = "#FFFFFF" if active else self.theme["fg"]
            btn.configure(bg=bg, fg=fg)

    # ══════════════════════════════════════════════════════════════════════
    #  SETTINGS MODE
    # ══════════════════════════════════════════════════════════════════════
    def _build_settings(self):
        if hasattr(self, 'settings_frame'):
            self.settings_frame.destroy()

        self.settings_frame = tk.Frame(self.main_frame,
                                       bg=self.theme["bg"],
                                       width=SCREEN_W, height=SCREEN_H)

        title = tk.Label(self.settings_frame, text="SETTINGS",
                         font=self.font_label,
                         bg=self.theme["bg"],
                         fg=self.theme["muted"])
        title.place(x=MARGIN_LEFT, y=30)

        # Night mode toggle
        night_lbl = tk.Label(self.settings_frame, text="Day / Night Mode",
                             font=self.font_body,
                             bg=self.theme["bg"],
                             fg=self.theme["fg"])
        night_lbl.place(x=MARGIN_LEFT, y=80)

        self.night_toggle = tk.Canvas(self.settings_frame,
                                      width=60, height=30,
                                      bg=self.theme["bg"],
                                      highlightthickness=0)
        self.night_toggle.place(x=400, y=80)
        self._draw_toggle(self.night_toggle,
                          self.settings.get("night_mode", False))
        self.night_toggle.bind("<Button-1>", self._toggle_night)

        # Alarm sound toggle
        alarm_lbl = tk.Label(self.settings_frame, text="Alarm Sound",
                             font=self.font_body,
                             bg=self.theme["bg"],
                             fg=self.theme["fg"])
        alarm_lbl.place(x=MARGIN_LEFT, y=130)

        self.alarm_toggle = tk.Canvas(self.settings_frame,
                                      width=60, height=30,
                                      bg=self.theme["bg"],
                                      highlightthickness=0)
        self.alarm_toggle.place(x=400, y=130)
        self._draw_toggle(self.alarm_toggle,
                          self.settings.get("alarm_sound", True))
        self.alarm_toggle.bind("<Button-1>", self._toggle_alarm)

        # Update section
        update_lbl = tk.Label(self.settings_frame,
                              text="Check for Updates",
                              font=self.font_body,
                              bg=self.theme["bg"],
                              fg=self.theme["fg"])
        update_lbl.place(x=MARGIN_LEFT, y=200)

        self.update_btn = tk.Button(self.settings_frame, text="UPDATE",
                                    font=self.font_btn,
                                    bg="#4A90D9", fg="#FFFFFF",
                                    activebackground="#3A7BC8",
                                    activeforeground="#FFFFFF",
                                    bd=0, padx=20, pady=6,
                                    command=self._on_update_pressed)
        self.update_btn.place(x=400, y=196)

        self.update_status = tk.Label(self.settings_frame, text="",
                                      font=self.font_small,
                                      bg=self.theme["bg"],
                                      fg=self.theme["muted"])
        self.update_status.place(x=MARGIN_LEFT, y=250)

    def _draw_toggle(self, canvas, is_on):
        canvas.delete("all")
        if is_on:
            self._canvas_rounded_rect(canvas, 0, 0, 60, 30, 15,
                                      fill="#4A90D9", outline="")
            canvas.create_oval(32, 2, 58, 28, fill="#FFFFFF", outline="")
        else:
            self._canvas_rounded_rect(canvas, 0, 0, 60, 30, 15,
                                      fill=self.theme["btn_bg"], outline="")
            canvas.create_oval(2, 2, 28, 28, fill="#FFFFFF", outline="")

    def _toggle_night(self, event=None):
        self.settings["night_mode"] = not self.settings.get("night_mode",
                                                            False)
        self._save_config()
        self._apply_theme()

    def _toggle_alarm(self, event=None):
        self.settings["alarm_sound"] = not self.settings.get("alarm_sound",
                                                             True)
        self._save_config()
        self._draw_toggle(self.alarm_toggle,
                          self.settings["alarm_sound"])

    # ── Update System ──────────────────────────────────────────────────────
    def _on_update_pressed(self):
        self.update_status.configure(text="Checking...")
        self.update_btn.configure(state="disabled")
        t = threading.Thread(target=self._do_update_check, daemon=True)
        t.start()

    def _do_update_check(self):
        try:
            import urllib.request
            import hashlib

            # Check remote dose_app.py
            url = RAW_URL + "/dose_app.py"
            req = urllib.request.Request(url)
            resp = urllib.request.urlopen(req, timeout=15)
            remote_data = resp.read()
            remote_hash = hashlib.md5(remote_data).hexdigest()

            # Local hash
            local_path = os.path.abspath(__file__)
            with open(local_path, "rb") as f:
                local_hash = hashlib.md5(f.read()).hexdigest()

            if remote_hash == local_hash:
                self.root.after(0, self._update_result,
                                "You're on the latest version.")
            else:
                self.root.after(0, self._apply_update, remote_data)
        except Exception as e:
            self.root.after(0, self._update_result,
                            f"No internet or error: {e}")

    def _update_result(self, msg):
        try:
            self.update_status.configure(text=msg)
            self.update_btn.configure(state="normal")
        except Exception:
            pass

    def _apply_update(self, remote_data):
        try:
            self.update_status.configure(text="Downloading...")
        except Exception:
            pass

        def do_download():
            try:
                import urllib.request

                # Write dose_app.py
                local_path = os.path.abspath(__file__)
                with open(local_path, "wb") as f:
                    f.write(remote_data)

                # Try to fetch DOSE.sh
                try:
                    url_sh = RAW_URL + "/DOSE.sh"
                    resp = urllib.request.urlopen(url_sh, timeout=15)
                    sh_data = resp.read()
                    sh_path = os.path.join(APP_DIR, "DOSE.sh")
                    os.makedirs(APP_DIR, exist_ok=True)
                    with open(sh_path, "wb") as f:
                        f.write(sh_data)
                    os.chmod(sh_path, 0o755)
                except Exception:
                    pass

                # Try to fetch dose_logo.png
                try:
                    url_logo = RAW_URL + "/dose_logo.png"
                    resp = urllib.request.urlopen(url_logo, timeout=15)
                    logo_data = resp.read()
                    logo_path = os.path.join(APP_DIR, "dose_logo.png")
                    with open(logo_path, "wb") as f:
                        f.write(logo_data)
                except Exception:
                    pass

                self.root.after(0, self._finish_update)
            except Exception as e:
                self.root.after(0, self._update_result,
                                f"Update failed: {e}")

        t = threading.Thread(target=do_download, daemon=True)
        t.start()

    def _finish_update(self):
        try:
            self.update_status.configure(text="Updated! Restarting...")
        except Exception:
            pass
        self.root.after(2000, self._restart_app)

    def _restart_app(self):
        try:
            if self.camera and self.camera_running:
                self.camera_running = False
                self.camera.stop()
                self.camera.close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception:
            sys.exit(0)

    # ══════════════════════════════════════════════════════════════════════
    #  DISPENSING FLOW (Overlay)
    # ══════════════════════════════════════════════════════════════════════
    def _build_overlay(self):
        for w in self.overlay_frame.winfo_children():
            w.destroy()
        self.overlay_frame.configure(bg=self.theme["bg"])

    def _start_dispense(self, pill_key):
        self.dispense_pill = pill_key
        self.dispense_state = 1
        self.overlay_frame.place(x=0, y=0,
                                 width=SCREEN_W, height=SCREEN_H)
        self._raise_widget(self.overlay_frame)
        self._raise_widget(self.d_btn_canvas)
        self._show_dispense_read()

    def _end_dispense(self):
        self.dispense_state = 0
        self.dispense_pill = None
        if self.hold_after_id:
            self.root.after_cancel(self.hold_after_id)
            self.hold_after_id = None
        self.overlay_frame.place_forget()
        self._show_mode(self.mode)

    # ── Stage 1: READ ──────────────────────────────────────────────────────
    def _show_dispense_read(self):
        for w in self.overlay_frame.winfo_children():
            w.destroy()

        med = self.meds.get(self.dispense_pill, self.meds["blue"])

        lbl_stage = tk.Label(self.overlay_frame, text="SCAN RESULT",
                             font=self.font_label,
                             bg=self.theme["bg"],
                             fg=self.theme["muted"])
        lbl_stage.place(x=MARGIN_LEFT, y=40)

        lbl_name = tk.Label(self.overlay_frame, text=med["name"],
                            font=self.font_name,
                            bg=self.theme["bg"],
                            fg=med["accent"])
        lbl_name.place(x=MARGIN_LEFT, y=72)

        lbl_count = tk.Label(self.overlay_frame,
                             text=f"{med['pills_left']} pills remaining",
                             font=self.font_body,
                             bg=self.theme["bg"],
                             fg=self.theme["muted"])
        lbl_count.place(x=MARGIN_LEFT, y=125)

        lbl_take = tk.Label(self.overlay_frame,
                            text=med.get("take_with", ""),
                            font=self.font_small,
                            bg=self.theme["bg"],
                            fg=self.theme["fg"])
        lbl_take.place(x=MARGIN_LEFT, y=170)

        lbl_safety = tk.Label(self.overlay_frame,
                              text=med.get("safety", ""),
                              font=self.font_small,
                              bg=self.theme["bg"],
                              fg=self.theme["muted"])
        lbl_safety.place(x=MARGIN_LEFT, y=200)

        lbl_q = tk.Label(self.overlay_frame, text="Dispense this medication?",
                         font=self.font_body_bold,
                         bg=self.theme["bg"],
                         fg=self.theme["fg"])
        lbl_q.place(x=MARGIN_LEFT, y=280)

        btn_yes = tk.Button(self.overlay_frame, text="Yes",
                            font=self.font_btn_lg,
                            bg="#4A90D9", fg="#FFFFFF",
                            activebackground="#3A7BC8",
                            activeforeground="#FFFFFF",
                            bd=0, padx=40, pady=12,
                            command=self._read_yes)
        btn_yes.place(x=MARGIN_LEFT, y=340)

        btn_no = tk.Button(self.overlay_frame, text="No",
                           font=self.font_btn_lg,
                           bg=self.theme["btn_bg"],
                           fg=self.theme["fg"],
                           activebackground=self.theme["btn_active"],
                           activeforeground=self.theme["fg"],
                           bd=0, padx=40, pady=12,
                           command=self._end_dispense)
        btn_no.place(x=220, y=340)

    def _read_yes(self):
        self.dispense_state = 2
        self._show_dispense_hold()

    # ── Stage 2: HOLD ─────────────────────────────────────────────────────
    def _show_dispense_hold(self):
        for w in self.overlay_frame.winfo_children():
            w.destroy()

        med = self.meds.get(self.dispense_pill, self.meds["blue"])

        lbl_inst = tk.Label(self.overlay_frame,
                            text="PRESS AND HOLD TO DISPENSE",
                            font=self.font_label,
                            bg=self.theme["bg"],
                            fg=self.theme["muted"])
        lbl_inst.place(x=MARGIN_LEFT, y=80)

        lbl_name = tk.Label(self.overlay_frame, text=med["name"],
                            font=self.font_title,
                            bg=self.theme["bg"],
                            fg=med["accent"])
        lbl_name.place(x=MARGIN_LEFT, y=120)

        # Progress bar canvas
        self.hold_canvas = tk.Canvas(self.overlay_frame,
                                     width=SCREEN_W - 2 * MARGIN_LEFT,
                                     height=40,
                                     bg=self.theme["card_bg"],
                                     highlightthickness=0)
        self.hold_canvas.place(x=MARGIN_LEFT, y=200)

        self.hold_progress_bar = self.hold_canvas.create_rectangle(
            0, 0, 0, 40, fill=med["accent"], outline="")

        self.hold_label = tk.Label(self.overlay_frame,
                                   text="Hold for 3 seconds...",
                                   font=self.font_body,
                                   bg=self.theme["bg"],
                                   fg=self.theme["fg"])
        self.hold_label.place(x=MARGIN_LEFT, y=260)

        # Hold area — large invisible frame for touch target
        self.hold_area = tk.Frame(self.overlay_frame,
                                  bg=self.theme["bg"],
                                  width=SCREEN_W,
                                  height=SCREEN_H)
        self.hold_area.place(x=0, y=0, width=SCREEN_W, height=SCREEN_H)
        self._raise_widget(self.hold_canvas)
        self._raise_widget(lbl_inst)
        self._raise_widget(lbl_name)
        self._raise_widget(self.hold_label)
        self._raise_widget(self.d_btn_canvas)

        # Bind press/release to overlay frame and all children
        self._bind_hold_recursive(self.overlay_frame)

    def _bind_hold_recursive(self, widget):
        widget.bind("<ButtonPress-1>", self._hold_press)
        widget.bind("<ButtonRelease-1>", self._hold_release)
        for child in widget.winfo_children():
            self._bind_hold_recursive(child)

    def _hold_press(self, event=None):
        if self.dispense_state != 2:
            return
        self.hold_start = time.time()
        self._hold_update()

    def _hold_release(self, event=None):
        if self.dispense_state != 2:
            return
        self.hold_start = 0
        if self.hold_after_id:
            self.root.after_cancel(self.hold_after_id)
            self.hold_after_id = None
        # Reset progress bar
        try:
            self.hold_canvas.coords(self.hold_progress_bar, 0, 0, 0, 40)
            self.hold_label.configure(text="Hold for 3 seconds...")
        except Exception:
            pass

    def _hold_update(self):
        if self.dispense_state != 2 or self.hold_start == 0:
            return
        elapsed = time.time() - self.hold_start
        bar_w = SCREEN_W - 2 * MARGIN_LEFT
        frac = min(elapsed / HOLD_TIME, 1.0)
        try:
            self.hold_canvas.coords(self.hold_progress_bar,
                                    0, 0, int(bar_w * frac), 40)
            remaining = max(0, HOLD_TIME - elapsed)
            self.hold_label.configure(
                text=f"Hold for {remaining:.1f} seconds...")
        except Exception:
            pass

        if elapsed >= HOLD_TIME:
            self.hold_start = 0
            self.dispense_state = 3
            self._show_dispense_confirm()
            return

        self.hold_after_id = self.root.after(50, self._hold_update)

    # ── Stage 3: CONFIRM ───────────────────────────────────────────────────
    def _show_dispense_confirm(self):
        for w in self.overlay_frame.winfo_children():
            w.destroy()

        med = self.meds.get(self.dispense_pill, self.meds["blue"])

        lbl_ready = tk.Label(self.overlay_frame, text="READY",
                             font=self.font_label,
                             bg=self.theme["bg"],
                             fg=self.theme["muted"])
        lbl_ready.place(x=MARGIN_LEFT, y=80)

        lbl_name = tk.Label(self.overlay_frame, text=med["name"],
                            font=self.font_title,
                            bg=self.theme["bg"],
                            fg=med["accent"])
        lbl_name.place(x=MARGIN_LEFT, y=120)

        lbl_inst = tk.Label(self.overlay_frame,
                            text="Press down to dispense",
                            font=self.font_body,
                            bg=self.theme["bg"],
                            fg=self.theme["fg"])
        lbl_inst.place(x=MARGIN_LEFT, y=200)

        btn_dispense = tk.Button(self.overlay_frame, text="DISPENSE",
                                 font=self.font_btn_lg,
                                 bg=med["accent"], fg="#FFFFFF",
                                 activebackground=med["accent"],
                                 activeforeground="#FFFFFF",
                                 bd=0, padx=60, pady=16,
                                 command=self._confirm_dispense)
        btn_dispense.place(x=MARGIN_LEFT, y=270)

        btn_cancel = tk.Button(self.overlay_frame, text="Cancel",
                               font=self.font_btn,
                               bg=self.theme["btn_bg"],
                               fg=self.theme["fg"],
                               activebackground=self.theme["btn_active"],
                               activeforeground=self.theme["fg"],
                               bd=0, padx=30, pady=12,
                               command=self._end_dispense)
        btn_cancel.place(x=MARGIN_LEFT, y=350)

    def _confirm_dispense(self):
        med = self.meds.get(self.dispense_pill, self.meds["blue"])
        if med["pills_left"] > 0:
            med["pills_left"] -= 1
            self._save_config()
        self.dispense_state = 4
        self._play_sound()
        self._show_dispense_done()

    # ── Stage 4: DISPENSED ─────────────────────────────────────────────────
    def _show_dispense_done(self):
        for w in self.overlay_frame.winfo_children():
            w.destroy()

        med = self.meds.get(self.dispense_pill, self.meds["blue"])

        lbl_done = tk.Label(self.overlay_frame, text="DISPENSED",
                            font=self.font_label,
                            bg=self.theme["bg"],
                            fg=self.theme["muted"])
        lbl_done.place(x=MARGIN_LEFT, y=80)

        lbl_name = tk.Label(self.overlay_frame, text=med["name"],
                            font=self.font_name,
                            bg=self.theme["bg"],
                            fg=med["accent"])
        lbl_name.place(x=MARGIN_LEFT, y=120)

        check_lbl = tk.Label(self.overlay_frame, text="✓",
                             font=tkfont.Font(family="DejaVu Sans",
                                              size=72),
                             bg=self.theme["bg"],
                             fg="#4AD97A")
        check_lbl.place(x=SCREEN_W // 2 - 40, y=200)

        lbl_count = tk.Label(self.overlay_frame,
                             text=f"{med['pills_left']} pills remaining",
                             font=self.font_body,
                             bg=self.theme["bg"],
                             fg=self.theme["muted"])
        lbl_count.place(x=MARGIN_LEFT, y=380)

        self.root.after(4000, self._end_dispense)

    def _play_sound(self):
        try:
            subprocess.Popen(["aplay", "-q",
                              "/usr/share/sounds/alsa/Front_Center.wav"],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception:
            try:
                self.root.bell()
            except Exception:
                pass

    # ══════════════════════════════════════════════════════════════════════
    #  CAMERA
    # ══════════════════════════════════════════════════════════════════════
    def _start_camera(self):
        try:
            self.camera = Picamera2()
            config = self.camera.create_still_configuration(
                main={"size": (320, 240)})
            self.camera.configure(config)
            self.camera.start()
            self.camera_running = True
            t = threading.Thread(target=self._camera_loop, daemon=True)
            t.start()
        except Exception:
            self.camera = None
            self.camera_running = False

    def _camera_loop(self):
        while self.camera_running:
            try:
                frame = self.camera.capture_array()
                if PIL_AVAILABLE:
                    img = Image.fromarray(frame)
                    results = pyzbar_decode(img)
                    for r in results:
                        data = r.data.decode("utf-8", errors="ignore").lower()
                        if data in self.meds and self.dispense_state == 0:
                            self.root.after(0, self._start_dispense, data)
                            time.sleep(2)
                            break
                time.sleep(0.3)
            except Exception:
                time.sleep(1)

    # ══════════════════════════════════════════════════════════════════════
    #  RUN
    # ══════════════════════════════════════════════════════════════════════
    def run(self):
        try:
            self.root.mainloop()
        finally:
            try:
                if self.camera and self.camera_running:
                    self.camera_running = False
                    self.camera.stop()
                    self.camera.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    try:
        app = DoseApp()
        app.run()
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        log_path = os.path.join(os.path.expanduser("~"),
                                "dose-home-station", "crash.log")
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "w") as f:
                f.write(err)
        except Exception:
            pass
        print("\n  DOSE crashed. Error:\n")
        print(err)
        print(f"\n  Error log saved to: {log_path}")
        print("\n  Press Enter to close...")
        try:
            input()
        except Exception:
            pass
