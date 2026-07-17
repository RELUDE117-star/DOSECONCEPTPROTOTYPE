#!/usr/bin/env python3
"""
DOSE Home Station — Prototype Application
v4.0 — New design with navigation rail, circular hold progress, updated theme

Modes: Home (default), Storage, Settings, Add Medication
Dispensing overlay: HOLD → DISPENSED

Meds start empty (count 0). Scan a QR to load a slot.
MPR121 pads 0-3 map to blue/red/green/yellow.
Auto-update checks GitHub on launch + UPDATE button in Settings.

Gracefully handles missing hardware.
"""

import json
import hashlib
import math
import os
import sys
import time
import threading
import subprocess
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from urllib.request import urlopen
from urllib.error import URLError

# ── Graceful optional imports ──────────────────────────────────────────────
PIL_AVAILABLE = False
try:
    from PIL import Image, ImageTk, ImageDraw
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

HAVE_MPR121 = False
mpr121_mod = None
try:
    import board
    import busio
    import adafruit_mpr121
    mpr121_mod = adafruit_mpr121
    HAVE_MPR121 = True
except Exception:
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--break-system-packages",
             "adafruit-circuitpython-mpr121"],
            capture_output=True, timeout=60)
        import board
        import busio
        import adafruit_mpr121
        mpr121_mod = adafruit_mpr121
        HAVE_MPR121 = True
    except Exception:
        pass

# ── Constants ──────────────────────────────────────────────────────────────
SCREEN_W = 800
SCREEN_H = 480
RAIL_W = 128
CONTENT_W = SCREEN_W - RAIL_W  # 672
HOLD_TIME = 3.0
DISPENSED_TIME = 4.0
DEFAULT_QTY = 30
QR_PRESENCE_TIMEOUT = 15.0
ALL_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_LABELS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]

TIME_PRESETS = [
    '6:00 AM', '7:00 AM', '8:00 AM', '9:00 AM', '10:00 AM', '11:00 AM',
    '12:00 PM', '1:00 PM', '2:00 PM', '3:00 PM', '4:00 PM', '5:00 PM',
    '6:00 PM', '7:00 PM', '8:00 PM', '9:00 PM', '10:00 PM'
]

CONFIG_PATH = os.path.expanduser("~/.dose_config.json")
DATA_PATH = os.path.expanduser("~/dose-home-station/med_data.json")
APP_DIR = os.path.expanduser("~/dose-home-station")
APP_FILE = os.path.join(APP_DIR, "dose_app.py")
RAW_URL = ("https://raw.githubusercontent.com/relude117-star/"
           "doseconceptprototype/claude/quirky-brown-vkHwi")

SLOT_KEYS = ["blue", "red", "green", "yellow"]

SLOT_COLORS = {
    "blue": "#5FA3F5",
    "red": "#FF6B6B",
    "green": "#30D158",
    "yellow": "#FFD60A",
}

SLOT_PADS = {"blue": 0, "red": 1, "green": 2, "yellow": 3}

DARK_THEME = {
    "bg": "#0B0B0D",
    "fg": "#F5F5F7",
    "muted": "#8E8E93",
    "card_bg": "#1C1C1E",
    "elevated_bg": "#26262A",
    "rail_bg": "#000000",
    "divider": "#3A3A3C",
    "rail_btn_bg": "#1E1E20",
    "rail_icon_color": "#FFFFFF",
    "btn_bg": "#1E1E24",
    "btn_active": "#2A2A32",
}

LIGHT_THEME = {
    "bg": "#EDEBE7",
    "fg": "#1C1C1E",
    "muted": "#8A8A8E",
    "card_bg": "#FAF9F6",
    "elevated_bg": "#EFEDE9",
    "rail_bg": "#F3F1ED",
    "divider": "#C8C8CA",
    "rail_btn_bg": "#E2DFD9",
    "rail_icon_color": "#2A2A2E",
    "btn_bg": "#DCDCDA",
    "btn_active": "#D0D0CE",
}

ACCENT_BLUE = "#5FA3F5"


def _find_logo():
    candidates = [
        os.path.join(APP_DIR, "dose_logo.png"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "dose_logo.png"),
        "dose_logo.png",
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def _raise(widget):
    widget.tk.call('raise', widget._w)


def _load_med_data():
    try:
        with open(DATA_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_med_data(data):
    try:
        os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
        with open(DATA_PATH, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


class DoseApp:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("DOSE")
        self.root.geometry(f"{SCREEN_W}x{SCREEN_H}+0+0")
        try:
            self.root.attributes("-fullscreen", True)
        except Exception:
            pass
        self.root.configure(cursor="none")
        self.root.resizable(False, False)

        # ── Fonts ──────────────────────────────────────────────────────────
        # Inter weight variants register as separate family names in Tkinter.
        # We pick the best available for each design weight (800/700/600/400).
        families = tkfont.families(self.root)
        def _pick(candidates):
            for c in candidates:
                if c in families:
                    return c
            return "DejaVu Sans"

        # weight-800 (ExtraBold) — headlines, countdown, big numbers
        f_extrabold = _pick(["Inter ExtraBold", "Inter Black", "Inter", "DejaVu Sans"])
        # weight-700 (Bold) — card titles, buttons, nav labels
        f_bold = _pick(["Inter", "DejaVu Sans"])
        # weight-600 (SemiBold) — settings labels, medium emphasis
        f_semibold = _pick(["Inter SemiBold", "Inter Medium", "Inter", "DejaVu Sans"])
        # weight-400 (Regular) — body text, secondary info
        f_regular = _pick(["Inter Light", "Inter", "DejaVu Sans"])

        self.font_clock = tkfont.Font(family=f_extrabold, size=20, weight="bold")
        self.font_date = tkfont.Font(family=f_regular, size=13)
        self.font_name_lg = tkfont.Font(family=f_extrabold, size=28, weight="bold")
        self.font_name = tkfont.Font(family=f_bold, size=22, weight="bold")
        self.font_body = tkfont.Font(family=f_regular, size=16)
        self.font_body_bold = tkfont.Font(family=f_bold, size=16, weight="bold")
        self.font_label = tkfont.Font(family=f_extrabold, size=11, weight="bold")
        self.font_small = tkfont.Font(family=f_regular, size=13)
        self.font_small_bold = tkfont.Font(family=f_bold, size=13, weight="bold")
        self.font_btn = tkfont.Font(family=f_extrabold, size=16, weight="bold")
        self.font_btn_lg = tkfont.Font(family=f_extrabold, size=17, weight="bold")
        self.font_title = tkfont.Font(family=f_bold, size=18, weight="bold")
        self.font_medium = tkfont.Font(family=f_extrabold, size=14, weight="bold")
        self.font_count = tkfont.Font(family=f_extrabold, size=24, weight="bold")
        self.font_hold_big = tkfont.Font(family=f_extrabold, size=40, weight="bold")
        self.font_hold_label = tkfont.Font(family=f_regular, size=12)
        self.font_settings = tkfont.Font(family=f_semibold, size=17)
        self.font_rail = tkfont.Font(family=f_bold, size=10, weight="bold")
        self.font_tiny = tkfont.Font(family=f_extrabold, size=9, weight="bold")
        self.font_check = tkfont.Font(family=f_extrabold, size=48, weight="bold")

        # ── State ──────────────────────────────────────────────────────────
        self.med_data = {}
        self.settings = {"night_mode": False, "alarm_sound": True,
                         "constant_scan": False}
        self.theme = dict(DARK_THEME)
        self.mode = "home"
        self.selected_pill = "blue"
        self.dispense_state = 0
        self.dispense_pill = None
        self.hold_start = 0
        self.hold_after_id = None
        self.camera = None
        self.camera_running = False
        self.mpr = None
        self.has_touch = False
        self.mpr_prev = [False] * 12
        self.qr_last_seen = {k: 0.0 for k in SLOT_KEYS}
        self._home_prev_keys = None
        self._storage_prev_visible = None
        self._anim_queue = []
        self._anim_running = False
        self._prev_qr_present = {k: False for k in SLOT_KEYS}

        # Add-med draft state
        self._draft = {
            "name": "", "times_per_day": 1,
            "doses": [2],  # index into TIME_PRESETS
            "qty": 30,
            "days": [1, 1, 1, 1, 1, 1, 1],
        }

        # ── Config + Med data ──────────────────────────────────────────────
        self._load_config()
        self._init_med_data()
        self._apply_theme_colors()

        # ── MPR121 init ────────────────────────────────────────────────────
        self.touch_error = ""
        if HAVE_MPR121:
            try:
                subprocess.run(["sudo", "modprobe", "i2c-dev"],
                               capture_output=True, timeout=5)
            except Exception:
                pass
            try:
                import board, busio
                i2c = busio.I2C(board.SCL, board.SDA)
                self.mpr = mpr121_mod.MPR121(i2c, address=0x5A)
                self.has_touch = True
            except Exception as e:
                self.touch_error = str(e)
        else:
            self.touch_error = "Library not installed"

        # ── Root container ─────────────────────────────────────────────────
        self.root.configure(bg=self.theme["bg"])

        # Content area (left 672px)
        self.content_frame = tk.Frame(self.root, bg=self.theme["bg"],
                                      width=CONTENT_W, height=SCREEN_H)
        self.content_frame.place(x=0, y=0, width=CONTENT_W, height=SCREEN_H)

        # Navigation rail (right 128px)
        self.rail_frame = tk.Frame(self.root, bg=self.theme["rail_bg"],
                                   width=RAIL_W, height=SCREEN_H)
        self.rail_frame.place(x=CONTENT_W, y=0, width=RAIL_W, height=SCREEN_H)

        # Overlay frame for dispensing/qty
        self.overlay_frame = tk.Frame(self.root, bg=self.theme["bg"],
                                      width=CONTENT_W, height=SCREEN_H)

        # Animation canvas
        self.anim_canvas = tk.Canvas(self.root, width=CONTENT_W,
                                     height=SCREEN_H,
                                     highlightthickness=0, bd=0,
                                     bg=self.theme["bg"])

        # ── Build UI ───────────────────────────────────────────────────────
        self._build_rail()
        self._build_home()
        self._build_storage()
        self._build_settings()
        self._build_add_med()

        # ── Bindings ───────────────────────────────────────────────────────
        self.root.bind("<Escape>", lambda e: self._quit())

        # ── Show initial mode ──────────────────────────────────────────────
        self._show_mode("home")
        self._tick_clock()

        if CAMERA_AVAILABLE:
            self._start_camera()
        if self.has_touch:
            self._poll_touch()

        self.root.after(2000, lambda: self._do_update_check(silent=True))
        self.root.focus_force()

    # ── Safe widget raising ────────────────────────────────────────────────
    @staticmethod
    def _raise_widget(widget):
        widget.tk.call('raise', widget._w)

    # ── Med data management ────────────────────────────────────────────────
    def _init_med_data(self):
        saved = _load_med_data()
        for key in SLOT_KEYS:
            if key in saved:
                self.med_data[key] = saved[key]
            else:
                self.med_data[key] = {
                    "name": key.capitalize(),
                    "count": 0,
                    "loaded": False,
                    "take_with": "",
                    "schedule_time": "8:00 AM",
                    "doses": [2],
                    "times_per_day": 1,
                    "schedule_days": list(ALL_DAYS),
                }

    def _save_med(self):
        _save_med_data(self.med_data)

    def _is_loaded(self, key):
        return self.med_data.get(key, {}).get("loaded", False)

    def _get_count(self, key):
        return self.med_data.get(key, {}).get("count", 0)

    # ── Config persistence ─────────────────────────────────────────────────
    def _load_config(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r") as f:
                    loaded = json.load(f)
                saved_settings = loaded.get("settings", {})
                for k in self.settings:
                    if k in saved_settings:
                        self.settings[k] = saved_settings[k]
            except Exception:
                pass

    def _save_config(self):
        data = {"settings": self.settings}
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
        self.content_frame.configure(bg=self.theme["bg"])
        self.rail_frame.configure(bg=self.theme["rail_bg"])
        self.overlay_frame.configure(bg=self.theme["bg"])
        self._build_rail()
        self._build_home()
        self._build_storage()
        self._build_settings()
        self._build_add_med()
        self._show_mode(self.mode)

    # ══════════════════════════════════════════════════════════════════════
    #  NAVIGATION RAIL (right side, 128px)
    # ══════════════════════════════════════════════════════════════════════
    def _build_rail(self):
        for w in self.rail_frame.winfo_children():
            w.destroy()

        # Divider line on left edge
        tk.Frame(self.rail_frame, bg=self.theme["divider"],
                 width=1).place(x=0, y=0, width=1, height=SCREEN_H)

        rail_items = [
            ("Add Med", "addmed", self._nav_add_med),
            ("Storage", "storage", self._nav_storage),
            ("Settings", "settings", self._nav_settings),
        ]

        y = 32
        self._rail_buttons = {}
        for label, mode_key, cmd in rail_items:
            btn_frame = tk.Frame(self.rail_frame, bg=self.theme["rail_btn_bg"],
                                 cursor="hand2")
            btn_frame.place(x=28, y=y, width=72, height=72)
            self._draw_rail_icon(btn_frame, mode_key)
            lbl = tk.Label(self.rail_frame, text=label,
                           font=self.font_rail,
                           bg=self.theme["rail_bg"],
                           fg=self.theme["muted"])
            lbl.place(x=64, y=y + 76, anchor="n")
            for w in [btn_frame, lbl]:
                w.bind("<Button-1>", lambda e, c=cmd: c())
            self._rail_buttons[mode_key] = btn_frame
            y += 98

        # Home button at bottom with DOSE logo
        home_y = y
        home_frame = tk.Frame(self.rail_frame, cursor="hand2",
                              bg=ACCENT_BLUE)
        home_frame.place(x=28, y=home_y, width=72, height=72)
        self._draw_home_logo(home_frame)
        home_lbl = tk.Label(self.rail_frame, text="Home",
                            font=self.font_rail,
                            bg=self.theme["rail_bg"],
                            fg=self.theme["fg"])
        home_lbl.place(x=64, y=home_y + 76, anchor="n")
        for w in [home_frame, home_lbl]:
            w.bind("<Button-1>", lambda e: self._nav_home())
        self._rail_buttons["home"] = home_frame

    def _draw_rail_icon(self, frame, mode_key):
        for w in frame.winfo_children():
            w.destroy()
        c = tk.Canvas(frame, width=30, height=30,
                      bg=frame.cget("bg"), highlightthickness=0)
        c.place(relx=0.5, rely=0.5, anchor="center")
        color = self.theme["rail_icon_color"]

        if mode_key == "addmed":
            c.create_line(15, 3, 15, 27, fill=color, width=4, capstyle="round")
            c.create_line(3, 15, 27, 15, fill=color, width=4, capstyle="round")
        elif mode_key == "storage":
            c.create_oval(5, 2, 25, 30, outline=color, width=2)
            c.create_rectangle(5, 15, 25, 30, fill=ACCENT_BLUE, outline="")
            c.create_oval(5, 2, 25, 30, outline=color, width=2)
        elif mode_key == "settings":
            c.create_oval(8, 8, 22, 22, outline=color, width=3)
            for angle in range(0, 360, 45):
                rad = math.radians(angle)
                x1 = 15 + 10 * math.cos(rad)
                y1 = 15 + 10 * math.sin(rad)
                x2 = 15 + 14 * math.cos(rad)
                y2 = 15 + 14 * math.sin(rad)
                c.create_line(x1, y1, x2, y2, fill=color, width=3,
                              capstyle="round")
        c.bind("<Button-1>", lambda e: frame.event_generate("<Button-1>"))

    def _draw_home_logo(self, frame):
        for w in frame.winfo_children():
            w.destroy()
        loaded = False
        if PIL_AVAILABLE:
            logo_path = _find_logo()
            if logo_path:
                try:
                    img = Image.open(logo_path)
                    resample = getattr(Image, 'LANCZOS',
                                       getattr(Image, 'ANTIALIAS', None))
                    img = img.resize((56, 56), resample)
                    self._home_logo_img = ImageTk.PhotoImage(img)
                    lbl = tk.Label(frame, image=self._home_logo_img,
                                   bg=frame.cget("bg"), bd=0)
                    lbl.place(relx=0.5, rely=0.5, anchor="center")
                    lbl.bind("<Button-1>",
                             lambda e: frame.event_generate("<Button-1>"))
                    loaded = True
                except Exception:
                    pass
        if not loaded:
            c = tk.Canvas(frame, width=32, height=32,
                          bg=frame.cget("bg"), highlightthickness=0)
            c.place(relx=0.5, rely=0.5, anchor="center")
            c.create_text(16, 16, text="D", fill="#FFFFFF",
                          font=self.font_name_lg)

    def _update_rail_highlight(self):
        active = self.mode
        if active in ("hold", "dispensed"):
            active = self._prev_mode if hasattr(self, '_prev_mode') else "home"
        for key, frame in self._rail_buttons.items():
            if key == "home":
                if active == "home":
                    frame.configure(bg=ACCENT_BLUE)
                else:
                    frame.configure(bg=self.theme["rail_btn_bg"])
                self._draw_home_logo(frame)
            else:
                if active == key:
                    frame.configure(bg=ACCENT_BLUE)
                else:
                    frame.configure(bg=self.theme["rail_btn_bg"])
                self._draw_rail_icon(frame, key)

    def _nav_home(self):
        self._show_mode("home")

    def _nav_storage(self):
        self._show_mode("storage")

    def _nav_settings(self):
        self._show_mode("settings")

    def _nav_add_med(self):
        self._show_mode("addmed")

    # ── Mode switching ─────────────────────────────────────────────────────
    def _show_mode(self, mode):
        if hasattr(self, '_prev_mode'):
            pass
        self._prev_mode = self.mode
        self.mode = mode

        self.home_frame.place_forget()
        self.storage_frame.place_forget()
        self.settings_frame.place_forget()
        self.addmed_frame.place_forget()
        self.overlay_frame.place_forget()

        if mode == "home":
            self.home_frame.place(x=0, y=0, width=CONTENT_W, height=SCREEN_H)
            self._home_prev_keys = None
            self._update_home()
        elif mode == "storage":
            self.storage_frame.place(x=0, y=0,
                                     width=CONTENT_W, height=SCREEN_H)
            self._storage_prev_visible = None
            self._update_storage()
        elif mode == "settings":
            self.settings_frame.place(x=0, y=0,
                                      width=CONTENT_W, height=SCREEN_H)
        elif mode == "addmed":
            self.addmed_frame.place(x=0, y=0,
                                    width=CONTENT_W, height=SCREEN_H)
            self._update_addmed_display()

        self._update_rail_highlight()

    # ── Clock ──────────────────────────────────────────────────────────────
    def _tick_clock(self):
        now = datetime.now()
        try:
            now_str = now.strftime("%-I:%M %p")
        except ValueError:
            now_str = now.strftime("%I:%M %p").lstrip("0")
        try:
            self.clock_label.configure(text=now_str)
        except Exception:
            pass

        months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        dows = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        try:
            date_str = f"{dows[now.weekday()]}, {months[now.month - 1]} {now.day}"
            self.date_label.configure(text=date_str)
        except Exception:
            pass

        self._check_presence_changes()
        if self.mode == "home":
            self._update_home()
        elif self.mode == "storage":
            self._update_storage()
        self.root.after(1000, self._tick_clock)

    def _check_presence_changes(self):
        if self.dispense_state > 0:
            return
        for key in SLOT_KEYS:
            if not self._is_loaded(key):
                self._prev_qr_present[key] = False
                continue
            now_present = self._is_qr_present(key)
            was_present = self._prev_qr_present[key]
            if now_present and not was_present:
                self._animate_pill_bottle(key, "down")
            elif was_present and not now_present:
                self._animate_pill_bottle(key, "up")
            self._prev_qr_present[key] = now_present

    def _is_qr_present(self, key):
        return (time.time() - self.qr_last_seen.get(key, 0)) < QR_PRESENCE_TIMEOUT

    # ══════════════════════════════════════════════════════════════════════
    #  HOME SCREEN
    # ══════════════════════════════════════════════════════════════════════
    def _build_home(self):
        if hasattr(self, 'home_frame'):
            self.home_frame.destroy()

        self.home_frame = tk.Frame(self.content_frame, bg=self.theme["bg"],
                                   width=CONTENT_W, height=SCREEN_H)

        # Clock + date row
        self.clock_label = tk.Label(self.home_frame, text="",
                                    font=self.font_clock,
                                    bg=self.theme["bg"],
                                    fg=self.theme["fg"])
        self.clock_label.place(x=32, y=32)

        self.date_label = tk.Label(self.home_frame, text="",
                                   font=self.font_date,
                                   bg=self.theme["bg"],
                                   fg=self.theme["muted"])
        self.date_label.place(x=200, y=37)

        # TODAY'S SCHEDULE
        tk.Label(self.home_frame, text="TODAY'S SCHEDULE",
                 font=self.font_label,
                 bg=self.theme["bg"],
                 fg=self.theme["muted"]).place(x=32, y=64)

        # Schedule cards container
        self.home_cards_frame = tk.Frame(self.home_frame, bg=self.theme["bg"])
        self.home_cards_frame.place(x=32, y=92, width=608, height=356)

        self.home_card_widgets = []
        for i in range(8):
            card = tk.Frame(self.home_cards_frame, bg=self.theme["card_bg"],
                            cursor="hand2")

            accent_dot = tk.Canvas(card, width=44, height=44,
                                   bg=self.theme["card_bg"],
                                   highlightthickness=0)
            accent_dot.place(x=20, y=16)

            time_lbl = tk.Label(card, text="", font=self.font_name,
                                bg=self.theme["card_bg"],
                                fg=self.theme["fg"], anchor="w")
            time_lbl.place(x=80, y=12)

            name_lbl = tk.Label(card, text="", font=self.font_small,
                                bg=self.theme["card_bg"],
                                fg=self.theme["muted"], anchor="w")
            name_lbl.place(x=80, y=42)

            count_lbl = tk.Label(card, text="", font=self.font_title,
                                 bg=self.theme["card_bg"],
                                 fg=self.theme["fg"], anchor="e")
            count_lbl.place(x=480, y=22)

            self.home_card_widgets.append({
                "card": card, "accent_dot": accent_dot,
                "time_lbl": time_lbl, "name_lbl": name_lbl,
                "count_lbl": count_lbl,
            })

        self.home_empty_label = tk.Label(self.home_frame, text="",
                                         font=self.font_body,
                                         bg=self.theme["bg"],
                                         fg=self.theme["muted"])
        self.home_empty_label.place(x=32, y=200)

        # Hardware status
        hw_parts = []
        if not CAMERA_AVAILABLE:
            hw_parts.append("Camera not connected")
        if not self.has_touch:
            msg = "Touch: " + (self.touch_error or "not detected")
            hw_parts.append(msg)
        if hw_parts:
            tk.Label(self.home_frame, text="  ·  ".join(hw_parts),
                     font=self.font_small,
                     bg=self.theme["bg"],
                     fg="#444444", wraplength=600, justify="left"
                     ).place(x=32, y=SCREEN_H - 30)

        self.home_frame.bind("<Button-1>", self._home_tap)

    def _home_tap(self, event=None):
        for key in SLOT_KEYS:
            if (self._is_loaded(key) and self._get_count(key) > 0
                    and self._is_qr_present(key)):
                self._start_dispense(key)
                return

    def _update_home(self):
        today_sched = self._get_today_schedule()
        visible_keys = tuple((e["key"], e.get("dose_idx", 0))
                             for e in today_sched[:8])
        layout_changed = (visible_keys != self._home_prev_keys)

        if layout_changed:
            self._home_prev_keys = visible_keys
            for w in self.home_card_widgets:
                w["card"].place_forget()

        if not today_sched:
            if layout_changed:
                self.home_empty_label.configure(
                    text="Place medications in view to see schedule")
                self.home_empty_label.place(x=32, y=200)
        else:
            if layout_changed:
                self.home_empty_label.place_forget()
            for i, entry in enumerate(today_sched[:8]):
                w = self.home_card_widgets[i]
                if layout_changed:
                    y = i * 88
                    w["card"].place(x=0, y=y, width=608, height=76)

                    # Round accent dot
                    w["accent_dot"].delete("all")
                    self._draw_rounded_rect(w["accent_dot"], 0, 0, 44, 44,
                                            14, entry["accent"])
                    w["accent_dot"].configure(bg=self.theme["card_bg"])

                    # Bind tap to dispense
                    for widget in [w["card"], w["time_lbl"],
                                   w["name_lbl"], w["count_lbl"]]:
                        widget.bind("<Button-1>",
                                    lambda e, k=entry["key"]: self._start_dispense_if_ok(k))

                w["time_lbl"].configure(text=entry["time"])
                w["name_lbl"].configure(text=entry["name"])
                cnt = entry.get("count", 0)
                w["count_lbl"].configure(
                    text=f"{cnt} pill{'s' if cnt != 1 else ''}",
                    fg=entry["accent"])

    def _start_dispense_if_ok(self, key):
        if self._is_loaded(key) and self._get_count(key) > 0:
            self._start_dispense(key)

    def _get_today_schedule(self):
        now = datetime.now()
        today_name = now.strftime("%a")
        entries = []
        for key in SLOT_KEYS:
            md = self.med_data[key]
            if not md.get("loaded"):
                continue
            if not self._is_qr_present(key):
                continue
            days = md.get("schedule_days", ALL_DAYS)
            if today_name not in days:
                continue

            dose_indices = md.get("doses", [2])
            for di, dose_idx in enumerate(dose_indices):
                if 0 <= dose_idx < len(TIME_PRESETS):
                    time_str = TIME_PRESETS[dose_idx]
                else:
                    time_str = md.get("schedule_time", "8:00 AM")
                try:
                    t = datetime.strptime(time_str, "%I:%M %p").replace(
                        year=now.year, month=now.month, day=now.day)
                    sort_key = t
                except Exception:
                    sort_key = now
                try:
                    display = sort_key.strftime("%-I:%M %p")
                except ValueError:
                    display = sort_key.strftime("%I:%M %p").lstrip("0")
                entries.append({
                    "key": key,
                    "dose_idx": di,
                    "time": display,
                    "name": md["name"],
                    "count": md.get("count", 0),
                    "accent": SLOT_COLORS[key],
                    "sort": sort_key,
                })
        entries.sort(key=lambda e: e["sort"])
        return entries

    # ══════════════════════════════════════════════════════════════════════
    #  STORAGE SCREEN
    # ══════════════════════════════════════════════════════════════════════
    def _build_storage(self):
        if hasattr(self, 'storage_frame'):
            self.storage_frame.destroy()

        self.storage_frame = tk.Frame(self.content_frame,
                                      bg=self.theme["bg"],
                                      width=CONTENT_W, height=SCREEN_H)

        # Left panel: pill list card (208px wide)
        left_card = tk.Frame(self.storage_frame, bg=self.theme["card_bg"])
        left_card.place(x=32, y=32, width=208, height=416)

        self.slot_rows = {}
        for i, key in enumerate(SLOT_KEYS):
            accent = SLOT_COLORS[key]
            row = tk.Frame(left_card, bg=self.theme["elevated_bg"],
                           cursor="hand2")
            row.place(x=10, y=16 + i * 96, width=188, height=88)

            dot = tk.Canvas(row, width=14, height=14,
                            bg=self.theme["elevated_bg"],
                            highlightthickness=0)
            dot.place(x=14, y=10)
            self._draw_rounded_rect(dot, 0, 0, 14, 14, 5, accent)

            name_lbl = tk.Label(row, text="—", fg=self.theme["fg"],
                                bg=self.theme["elevated_bg"],
                                font=self.font_small_bold, anchor="w")
            name_lbl.place(x=14, y=30)

            info_lbl = tk.Label(row, text="",
                                fg=self.theme["muted"],
                                bg=self.theme["elevated_bg"],
                                font=self.font_tiny, anchor="w")
            info_lbl.place(x=14, y=54)

            for w in [row, name_lbl, info_lbl, dot]:
                w.bind("<Button-1>",
                       lambda e, k=key: self._storage_select(k))

            self.slot_rows[key] = {
                "row": row, "name_lbl": name_lbl,
                "info_lbl": info_lbl, "dot": dot,
            }

        # Right panel: detail card (384px wide)
        self.detail_card = tk.Frame(self.storage_frame,
                                    bg=self.theme["card_bg"])
        self.detail_card.place(x=256, y=32, width=384, height=416)

        pad = 24

        self.detail_name = tk.Label(self.detail_card, text="",
                                    fg=self.theme["fg"],
                                    bg=self.theme["card_bg"],
                                    font=self.font_name_lg, anchor="w")
        self.detail_name.place(x=pad, y=pad)

        self.detail_count_lbl = tk.Label(self.detail_card, text="",
                                         fg=self.theme["muted"],
                                         bg=self.theme["card_bg"],
                                         font=self.font_body, anchor="w")
        self.detail_count_lbl.place(x=pad, y=pad + 36)

        # Separator
        tk.Frame(self.detail_card, bg=self.theme["muted"],
                 height=1).place(x=pad, y=pad + 68, width=384 - 2 * pad)

        # SCHEDULE label
        self.sched_label = tk.Label(self.detail_card, text="SCHEDULE",
                                    font=self.font_label,
                                    bg=self.theme["card_bg"],
                                    fg=self.theme["muted"])
        self.sched_label.place(x=pad, y=pad + 80)

        # Dose time chips container
        self.dose_chips_frame = tk.Frame(self.detail_card,
                                         bg=self.theme["card_bg"])
        self.dose_chips_frame.place(x=pad, y=pad + 100, width=336, height=60)

        # DAYS
        tk.Label(self.detail_card, text="DAYS",
                 font=self.font_label,
                 bg=self.theme["card_bg"],
                 fg=self.theme["muted"]).place(x=pad, y=pad + 170)

        df = tk.Frame(self.detail_card, bg=self.theme["card_bg"])
        df.place(x=pad, y=pad + 192)

        self.day_buttons = {}
        for idx, d in enumerate(ALL_DAYS):
            btn = tk.Button(df, text=DAY_LABELS[idx],
                            font=self.font_small,
                            bg=self.theme["elevated_bg"],
                            fg=self.theme["muted"],
                            activebackground=self.theme["btn_active"],
                            bd=0, width=3, pady=4,
                            command=lambda dd=d: self._toggle_day(dd))
            btn.pack(side="left", padx=2)
            self.day_buttons[d] = btn

        # DISPENSE button
        self.detail_dispense_btn = tk.Button(
            self.detail_card, text="DISPENSE",
            font=self.font_btn_lg,
            bg=ACCENT_BLUE, fg="#0A0A0C",
            activebackground="#4A8AE0",
            bd=0, padx=40, pady=12,
            command=self._detail_dispense)
        self.detail_dispense_btn.place(x=pad, y=pad + 244, width=336, height=48)

        # Empty slot message
        self.detail_empty_frame = tk.Frame(self.detail_card,
                                           bg=self.theme["card_bg"])

    def _storage_select(self, key):
        self.selected_pill = key
        self._update_storage_detail()

    def _detail_dispense(self):
        key = self.selected_pill
        if self._is_loaded(key) and self._get_count(key) > 0:
            self._start_dispense(key)

    def _toggle_day(self, day):
        key = self.selected_pill
        md = self.med_data[key]
        days = md.get("schedule_days", [])
        if day in days:
            days.remove(day)
        else:
            days.append(day)
        md["schedule_days"] = days
        self._save_med()
        self._update_day_buttons()

    def _update_day_buttons(self):
        key = self.selected_pill
        md = self.med_data[key]
        accent = SLOT_COLORS[key]
        days = md.get("schedule_days", [])
        for d, btn in self.day_buttons.items():
            if d in days:
                btn.configure(bg=accent, fg="#0A0A0C")
            else:
                btn.configure(bg=self.theme["elevated_bg"],
                              fg=self.theme["muted"])

    def _build_dose_chips(self):
        for w in self.dose_chips_frame.winfo_children():
            w.destroy()

        key = self.selected_pill
        md = self.med_data[key]
        accent = SLOT_COLORS[key]
        doses = md.get("doses", [2])

        self.sched_label.configure(
            text=f"SCHEDULE ({len(doses)}x DAILY)")

        for i, dose_idx in enumerate(doses):
            chip = tk.Frame(self.dose_chips_frame,
                            bg=self.theme["elevated_bg"])
            chip.pack(side="left", padx=3, pady=2)

            tk.Label(chip, text=f"Dose {i + 1}",
                     font=self.font_tiny,
                     bg=self.theme["elevated_bg"],
                     fg=self.theme["muted"]).pack(pady=(4, 0))

            row = tk.Frame(chip, bg=self.theme["elevated_bg"])
            row.pack(pady=(2, 4))

            prev_btn = tk.Label(row, text="‹",
                                font=self.font_small_bold,
                                bg=self.theme["card_bg"],
                                fg=self.theme["fg"],
                                width=2, cursor="hand2")
            prev_btn.pack(side="left", padx=2)
            prev_btn.bind("<Button-1>",
                          lambda e, idx=i: self._adj_dose_time(idx, -1))

            time_str = TIME_PRESETS[dose_idx] if 0 <= dose_idx < len(TIME_PRESETS) else "8:00 AM"
            time_lbl = tk.Label(row, text=time_str,
                                font=self.font_small_bold,
                                bg=self.theme["elevated_bg"],
                                fg=self.theme["fg"], width=8)
            time_lbl.pack(side="left", padx=2)

            next_btn = tk.Label(row, text="›",
                                font=self.font_small_bold,
                                bg=self.theme["card_bg"],
                                fg=self.theme["fg"],
                                width=2, cursor="hand2")
            next_btn.pack(side="left", padx=2)
            next_btn.bind("<Button-1>",
                          lambda e, idx=i: self._adj_dose_time(idx, 1))

    def _adj_dose_time(self, dose_idx, delta):
        key = self.selected_pill
        md = self.med_data[key]
        doses = md.get("doses", [2])
        if dose_idx < len(doses):
            n = len(TIME_PRESETS)
            doses[dose_idx] = (doses[dose_idx] + delta + n) % n
            md["doses"] = doses
            # Also update legacy schedule_time from first dose
            if doses:
                md["schedule_time"] = TIME_PRESETS[doses[0]]
            self._save_med()
            self._build_dose_chips()

    def _update_storage_detail(self):
        key = self.selected_pill
        md = self.med_data[key]
        accent = SLOT_COLORS[key]

        self.detail_empty_frame.place_forget()

        if md.get("loaded") and self._is_qr_present(key):
            self.detail_name.configure(text=md["name"], fg=accent)
            cnt = md.get("count", 0)
            self.detail_count_lbl.configure(
                text=f"{cnt} pill{'s' if cnt != 1 else ''} remaining")
            self._build_dose_chips()
            self._update_day_buttons()

            if cnt > 0:
                self.detail_dispense_btn.configure(
                    state="normal", bg=accent, fg="#0A0A0C")
            else:
                self.detail_dispense_btn.configure(
                    state="disabled", bg=self.theme["btn_bg"],
                    fg=self.theme["muted"])
        else:
            self.detail_name.configure(
                text=f"{key.capitalize()} Slot",
                fg=self.theme["muted"])
            self.detail_count_lbl.configure(text="Empty")
            for w in self.dose_chips_frame.winfo_children():
                w.destroy()
            self.sched_label.configure(text="SCHEDULE")
            self._update_day_buttons()
            self.detail_dispense_btn.configure(
                state="disabled", bg=self.theme["btn_bg"],
                fg=self.theme["muted"])

        # Highlight selected row
        for k in SLOT_KEYS:
            r = self.slot_rows[k]
            if k == key:
                r["row"].configure(
                    highlightbackground=SLOT_COLORS[k],
                    highlightthickness=2)
            else:
                r["row"].configure(highlightthickness=0)

    def _update_storage(self):
        now_visible = tuple(k for k in SLOT_KEYS
                            if self._is_qr_present(k) and self._is_loaded(k))
        layout_changed = (now_visible != self._storage_prev_visible)
        if layout_changed:
            self._storage_prev_visible = now_visible

        for key in SLOT_KEYS:
            r = self.slot_rows[key]
            md = self.med_data[key]
            accent = SLOT_COLORS[key]
            present = self._is_qr_present(key)
            if md.get("loaded") and present:
                r["name_lbl"].configure(text=md["name"])
                doses = md.get("doses", [2])
                if len(doses) > 1:
                    dose_text = f"{len(doses)}x daily"
                else:
                    idx = doses[0] if doses else 2
                    dose_text = f"Next dose {TIME_PRESETS[idx] if 0 <= idx < len(TIME_PRESETS) else '8:00 AM'}"
                r["info_lbl"].configure(text=dose_text)
                if layout_changed:
                    slot_idx = SLOT_KEYS.index(key)
                    r["row"].place(x=10, y=16 + slot_idx * 96,
                                   width=188, height=88)
            else:
                if layout_changed:
                    r["row"].place_forget()

        if now_visible:
            if self.selected_pill not in now_visible:
                self.selected_pill = now_visible[0]
            self._update_storage_detail()
        elif layout_changed:
            self.detail_name.configure(text="No pills in view",
                                       fg=self.theme["muted"])
            self.detail_count_lbl.configure(
                text="Place medications in camera view")
            for w in self.dose_chips_frame.winfo_children():
                w.destroy()
            self.detail_dispense_btn.configure(
                state="disabled", bg=self.theme["btn_bg"],
                fg=self.theme["muted"])

    # ══════════════════════════════════════════════════════════════════════
    #  SETTINGS SCREEN
    # ══════════════════════════════════════════════════════════════════════
    def _build_settings(self):
        if hasattr(self, 'settings_frame'):
            self.settings_frame.destroy()

        self.settings_frame = tk.Frame(self.content_frame,
                                       bg=self.theme["bg"],
                                       width=CONTENT_W, height=SCREEN_H)

        card = tk.Frame(self.settings_frame, bg=self.theme["card_bg"])
        card.place(x=32, y=32, width=608, height=416)

        settings_items = [
            ("Day / Night Mode", "#5FA3F5", "night_mode",
             self.settings.get("night_mode", False), self._toggle_night),
            ("Alarm Sound", "#FF9F43", "alarm_sound",
             self.settings.get("alarm_sound", True), self._toggle_alarm),
            ("Check for Updates", "#30D158", "update", None, None),
            ("Constant QR Scan", "#AF52DE", "constant_scan",
             self.settings.get("constant_scan", False),
             self._toggle_constant_scan),
        ]

        row_h = 416 // len(settings_items)
        for i, (label, icon_color, key, val, cmd) in enumerate(settings_items):
            y = i * row_h
            row = tk.Frame(card, bg=self.theme["card_bg"])
            row.place(x=0, y=y, width=608, height=row_h)

            if i < len(settings_items) - 1:
                tk.Frame(row, bg=self.theme["divider"],
                         height=1).place(x=24, y=row_h - 1,
                                         width=560)

            # Icon circle
            icon_c = tk.Canvas(row, width=40, height=40,
                               bg=self.theme["card_bg"],
                               highlightthickness=0)
            icon_c.place(x=24, y=(row_h - 40) // 2)
            self._draw_rounded_rect(icon_c, 0, 0, 40, 40, 12, icon_color)
            icon_c.create_oval(13, 13, 27, 27, outline="#FFFFFF", width=2)

            # Label
            lbl = tk.Label(row, text=label,
                           font=self.font_settings,
                           bg=self.theme["card_bg"],
                           fg=self.theme["fg"], anchor="w")
            lbl.place(x=80, y=(row_h - 24) // 2)

            if key == "update":
                # UPDATE button
                self.update_btn = tk.Button(
                    row, text="UPDATE",
                    font=self.font_medium,
                    bg=ACCENT_BLUE, fg="#FFFFFF",
                    activebackground="#4A8AE0",
                    bd=0, padx=16, pady=6,
                    command=self._on_update_pressed)
                self.update_btn.place(x=480, y=(row_h - 40) // 2,
                                      width=104, height=40)

                self.update_status = tk.Label(
                    row, text="",
                    font=self.font_small,
                    bg=self.theme["card_bg"],
                    fg=self.theme["muted"], anchor="w")
                self.update_status.place(x=80, y=(row_h - 24) // 2 + 24)
            else:
                # Toggle switch
                toggle = tk.Canvas(row, width=58, height=30,
                                   bg=self.theme["card_bg"],
                                   highlightthickness=0)
                toggle.place(x=520, y=(row_h - 30) // 2)
                self._draw_toggle(toggle, val)
                toggle.bind("<Button-1>",
                            lambda e, c=cmd: c() if c else None)
                if key == "night_mode":
                    self.night_toggle = toggle
                elif key == "alarm_sound":
                    self.alarm_toggle = toggle
                elif key == "constant_scan":
                    self.scan_toggle = toggle

    def _draw_toggle(self, canvas, is_on):
        canvas.delete("all")
        if is_on:
            self._draw_rounded_rect(canvas, 0, 0, 58, 30, 15, ACCENT_BLUE)
            canvas.create_oval(30, 2, 56, 28, fill="#FFFFFF", outline="")
        else:
            self._draw_rounded_rect(canvas, 0, 0, 58, 30, 15,
                                    self.theme["elevated_bg"])
            canvas.create_oval(2, 2, 28, 28, fill="#FFFFFF", outline="")

    def _toggle_night(self):
        self.settings["night_mode"] = not self.settings.get("night_mode", False)
        self._save_config()
        self._apply_theme()

    def _toggle_alarm(self):
        self.settings["alarm_sound"] = not self.settings.get("alarm_sound", True)
        self._save_config()
        self._draw_toggle(self.alarm_toggle, self.settings["alarm_sound"])

    def _toggle_constant_scan(self):
        self.settings["constant_scan"] = not self.settings.get(
            "constant_scan", False)
        self._save_config()
        self._draw_toggle(self.scan_toggle, self.settings["constant_scan"])

    # ── Auto-update ────────────────────────────────────────────────────────
    def _on_update_pressed(self):
        self.update_status.configure(text="Checking for updates…")
        self.update_btn.configure(state="disabled")
        threading.Thread(target=self._do_update_check, daemon=True).start()

    def _do_update_check(self, silent=False):
        try:
            url = RAW_URL + "/dose_app.py"
            resp = urlopen(url, timeout=15)
            remote_data = resp.read()
        except Exception:
            if not silent:
                self.root.after(0, self._update_result,
                                "No internet — try later")
            return

        remote_hash = hashlib.md5(remote_data).hexdigest()
        local_hash = ""
        local_path = os.path.abspath(__file__)
        try:
            with open(local_path, "rb") as f:
                local_hash = hashlib.md5(f.read()).hexdigest()
        except Exception:
            pass

        if remote_hash == local_hash:
            if not silent:
                self.root.after(0, self._update_result, "Up to date")
            return

        self.root.after(0, self._apply_update, remote_data)

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
                local_path = os.path.abspath(__file__)
                with open(local_path, "wb") as f:
                    f.write(remote_data)
                os.makedirs(APP_DIR, exist_ok=True)
                app_copy = os.path.join(APP_DIR, "dose_app.py")
                with open(app_copy, "wb") as f:
                    f.write(remote_data)
                for fname in ["DOSE.sh", "dose_logo.png"]:
                    try:
                        resp = urlopen(RAW_URL + "/" + fname, timeout=15)
                        fdata = resp.read()
                        fpath = os.path.join(APP_DIR, fname)
                        with open(fpath, "wb") as f:
                            f.write(fdata)
                        if fname.endswith(".sh"):
                            os.chmod(fpath, 0o755)
                    except Exception:
                        pass
                self.root.after(0, self._finish_update)
            except Exception as e:
                self.root.after(0, self._update_result,
                                f"Update failed: {e}")

        threading.Thread(target=do_download, daemon=True).start()

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
    #  ADD MEDICATION SCREEN
    # ══════════════════════════════════════════════════════════════════════
    def _build_add_med(self):
        if hasattr(self, 'addmed_frame'):
            self.addmed_frame.destroy()

        self.addmed_frame = tk.Frame(self.content_frame,
                                     bg=self.theme["bg"],
                                     width=CONTENT_W, height=SCREEN_H)

        card = tk.Frame(self.addmed_frame, bg=self.theme["card_bg"])
        card.place(x=32, y=32, width=608, height=416)
        self._addmed_card = card

    def _update_addmed_display(self):
        card = self._addmed_card
        for w in card.winfo_children():
            w.destroy()

        pad = 24
        draft = self._draft

        # Find next available slot color
        used_slots = [md.get("slot_key", k)
                      for k, md in self.med_data.items()
                      if md.get("loaded")]
        order = ["blue", "red", "green", "yellow"]
        next_slot = "blue"
        for s in order:
            if s not in used_slots:
                next_slot = s
                break
        slot_color = SLOT_COLORS[next_slot]
        self._draft_slot = next_slot

        # MEDICATION NAME
        tk.Label(card, text="MEDICATION NAME",
                 font=self.font_label,
                 bg=self.theme["card_bg"],
                 fg=self.theme["muted"]).place(x=pad, y=pad)

        # Slot color dot
        dot_c = tk.Canvas(card, width=28, height=28,
                          bg=self.theme["card_bg"], highlightthickness=0)
        dot_c.place(x=540, y=pad + 24)
        self._draw_rounded_rect(dot_c, 0, 0, 28, 28, 8, slot_color)

        self._name_entry = tk.Entry(
            card, font=self.font_title,
            bg=self.theme["elevated_bg"],
            fg=self.theme["fg"],
            insertbackground=self.theme["fg"],
            bd=0, relief="flat")
        self._name_entry.place(x=pad, y=pad + 24, width=500, height=40)
        self._name_entry.insert(0, draft["name"])

        # QTY and TIMES PER DAY row
        qty_frame = tk.Frame(card, bg=self.theme["elevated_bg"])
        qty_frame.place(x=pad, y=pad + 80, width=270, height=90)

        tk.Label(qty_frame, text="QUANTITY",
                 font=self.font_tiny,
                 bg=self.theme["elevated_bg"],
                 fg=self.theme["muted"]).place(relx=0.5, y=8, anchor="n")

        qty_row = tk.Frame(qty_frame, bg=self.theme["elevated_bg"])
        qty_row.place(relx=0.5, y=40, anchor="n")

        tk.Button(qty_row, text="−", font=self.font_body_bold,
                  bg=self.theme["card_bg"], fg=self.theme["fg"],
                  bd=0, width=3, command=self._dec_draft_qty
                  ).pack(side="left", padx=4)
        self._qty_label = tk.Label(qty_row, text=str(draft["qty"]),
                                   font=self.font_count,
                                   bg=self.theme["elevated_bg"],
                                   fg=self.theme["fg"], width=4)
        self._qty_label.pack(side="left")
        tk.Button(qty_row, text="+", font=self.font_body_bold,
                  bg=self.theme["card_bg"], fg=self.theme["fg"],
                  bd=0, width=3, command=self._inc_draft_qty
                  ).pack(side="left", padx=4)

        tpd_frame = tk.Frame(card, bg=self.theme["elevated_bg"])
        tpd_frame.place(x=pad + 290, y=pad + 80, width=270, height=90)

        tk.Label(tpd_frame, text="TIMES PER DAY",
                 font=self.font_tiny,
                 bg=self.theme["elevated_bg"],
                 fg=self.theme["muted"]).place(relx=0.5, y=8, anchor="n")

        tpd_row = tk.Frame(tpd_frame, bg=self.theme["elevated_bg"])
        tpd_row.place(relx=0.5, y=40, anchor="n")

        tk.Button(tpd_row, text="−", font=self.font_body_bold,
                  bg=self.theme["card_bg"], fg=self.theme["fg"],
                  bd=0, width=3, command=self._dec_draft_doses
                  ).pack(side="left", padx=4)
        self._tpd_label = tk.Label(tpd_row,
                                   text=str(draft["times_per_day"]),
                                   font=self.font_count,
                                   bg=self.theme["elevated_bg"],
                                   fg=self.theme["fg"], width=4)
        self._tpd_label.pack(side="left")
        tk.Button(tpd_row, text="+", font=self.font_body_bold,
                  bg=self.theme["card_bg"], fg=self.theme["fg"],
                  bd=0, width=3, command=self._inc_draft_doses
                  ).pack(side="left", padx=4)

        # DOSE TIMES
        tk.Label(card, text="DOSE TIMES",
                 font=self.font_tiny,
                 bg=self.theme["card_bg"],
                 fg=self.theme["muted"]).place(x=pad, y=pad + 180)

        self._draft_dose_frame = tk.Frame(card, bg=self.theme["card_bg"])
        self._draft_dose_frame.place(x=pad, y=pad + 200, width=560, height=50)
        self._build_draft_dose_chips()

        # DAYS
        tk.Label(card, text="DAYS",
                 font=self.font_tiny,
                 bg=self.theme["card_bg"],
                 fg=self.theme["muted"]).place(x=pad, y=pad + 260)

        days_frame = tk.Frame(card, bg=self.theme["card_bg"])
        days_frame.place(x=pad, y=pad + 280)

        self._draft_day_btns = {}
        for idx, d in enumerate(ALL_DAYS):
            active = draft["days"][idx]
            btn = tk.Button(days_frame, text=DAY_LABELS[idx],
                            font=self.font_small,
                            bg=slot_color if active else self.theme["elevated_bg"],
                            fg="#0A0A0C" if active else self.theme["muted"],
                            bd=0, width=4, pady=4,
                            command=lambda i=idx: self._toggle_draft_day(i))
            btn.pack(side="left", padx=2)
            self._draft_day_btns[idx] = btn

        # ADD MEDICATION button
        tk.Button(card, text="ADD MEDICATION",
                  font=self.font_btn_lg,
                  bg=ACCENT_BLUE, fg="#FFFFFF",
                  activebackground="#4A8AE0",
                  bd=0, pady=12,
                  command=self._submit_add_med
                  ).place(x=pad, y=pad + 330, width=560, height=52)

    def _build_draft_dose_chips(self):
        for w in self._draft_dose_frame.winfo_children():
            w.destroy()
        draft = self._draft
        for i, dose_idx in enumerate(draft["doses"]):
            chip = tk.Frame(self._draft_dose_frame,
                            bg=self.theme["elevated_bg"])
            chip.pack(side="left", padx=4)

            tk.Label(chip, text=f"Dose {i + 1}",
                     font=self.font_tiny,
                     bg=self.theme["elevated_bg"],
                     fg=self.theme["muted"]).pack(pady=(2, 0))

            row = tk.Frame(chip, bg=self.theme["elevated_bg"])
            row.pack(pady=(2, 4))

            tk.Label(row, text="‹", font=self.font_small_bold,
                     bg=self.theme["card_bg"], fg=self.theme["fg"],
                     width=2, cursor="hand2"
                     ).pack(side="left", padx=2)
            row.winfo_children()[-1].bind(
                "<Button-1>",
                lambda e, idx=i: self._adj_draft_dose(idx, -1))

            ts = TIME_PRESETS[dose_idx] if 0 <= dose_idx < len(TIME_PRESETS) else "8:00 AM"
            tk.Label(row, text=ts, font=self.font_small_bold,
                     bg=self.theme["elevated_bg"],
                     fg=self.theme["fg"], width=8).pack(side="left")

            tk.Label(row, text="›", font=self.font_small_bold,
                     bg=self.theme["card_bg"], fg=self.theme["fg"],
                     width=2, cursor="hand2"
                     ).pack(side="left", padx=2)
            row.winfo_children()[-1].bind(
                "<Button-1>",
                lambda e, idx=i: self._adj_draft_dose(idx, 1))

    def _adj_draft_dose(self, idx, delta):
        n = len(TIME_PRESETS)
        self._draft["doses"][idx] = (self._draft["doses"][idx] + delta + n) % n
        self._build_draft_dose_chips()

    def _inc_draft_qty(self):
        self._draft["qty"] = min(300, self._draft["qty"] + 1)
        self._qty_label.configure(text=str(self._draft["qty"]))

    def _dec_draft_qty(self):
        self._draft["qty"] = max(1, self._draft["qty"] - 1)
        self._qty_label.configure(text=str(self._draft["qty"]))

    def _inc_draft_doses(self):
        if self._draft["times_per_day"] >= 4:
            return
        self._draft["times_per_day"] += 1
        last = self._draft["doses"][-1] if self._draft["doses"] else 2
        self._draft["doses"].append(last)
        self._tpd_label.configure(text=str(self._draft["times_per_day"]))
        self._build_draft_dose_chips()

    def _dec_draft_doses(self):
        if self._draft["times_per_day"] <= 1:
            return
        self._draft["times_per_day"] -= 1
        self._draft["doses"] = self._draft["doses"][:self._draft["times_per_day"]]
        self._tpd_label.configure(text=str(self._draft["times_per_day"]))
        self._build_draft_dose_chips()

    def _toggle_draft_day(self, idx):
        self._draft["days"][idx] = 0 if self._draft["days"][idx] else 1
        slot_color = SLOT_COLORS.get(
            getattr(self, '_draft_slot', 'blue'), ACCENT_BLUE)
        active = self._draft["days"][idx]
        self._draft_day_btns[idx].configure(
            bg=slot_color if active else self.theme["elevated_bg"],
            fg="#0A0A0C" if active else self.theme["muted"])

    def _submit_add_med(self):
        name = self._name_entry.get().strip() or "New Medication"
        slot = getattr(self, '_draft_slot', 'blue')

        md = self.med_data[slot]
        md["name"] = name
        md["count"] = self._draft["qty"]
        md["loaded"] = True
        md["doses"] = self._draft["doses"][:]
        md["times_per_day"] = self._draft["times_per_day"]
        md["schedule_time"] = TIME_PRESETS[self._draft["doses"][0]] if self._draft["doses"] else "8:00 AM"

        day_map = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu",
                   4: "Fri", 5: "Sat", 6: "Sun"}
        md["schedule_days"] = [day_map[i] for i in range(7)
                               if self._draft["days"][i]]
        self._save_med()

        # Reset draft
        self._draft = {
            "name": "", "times_per_day": 1,
            "doses": [2], "qty": 30,
            "days": [1, 1, 1, 1, 1, 1, 1],
        }

        self.selected_pill = slot
        self._show_mode("home")

    # ══════════════════════════════════════════════════════════════════════
    #  QTY CONFIRM (overlay for first-time QR load)
    # ══════════════════════════════════════════════════════════════════════
    def _show_qty_confirm(self, slot_key, med_name):
        self._qty_slot = slot_key
        md = self.med_data[slot_key]
        if md.get("loaded") and md["count"] > 0:
            self._qty_value = md["count"]
        else:
            self._qty_value = DEFAULT_QTY

        for w in self.overlay_frame.winfo_children():
            w.destroy()

        accent = SLOT_COLORS[slot_key]

        self.overlay_frame.place(x=0, y=0,
                                 width=CONTENT_W, height=SCREEN_H)
        self._raise_widget(self.overlay_frame)

        card = tk.Frame(self.overlay_frame, bg=self.theme["card_bg"])
        card.place(x=32, y=32, width=608, height=416)

        pad = 24
        tk.Label(card, text="MEDICATION LOADED",
                 fg=self.theme["muted"], bg=self.theme["card_bg"],
                 font=self.font_label).place(x=pad, y=pad)

        tk.Label(card, text=med_name,
                 fg=accent, bg=self.theme["card_bg"],
                 font=self.font_name_lg).place(x=pad, y=pad + 24)

        tk.Label(card, text="HOW MANY PILLS?",
                 fg=self.theme["muted"], bg=self.theme["card_bg"],
                 font=self.font_label).place(x=pad, y=pad + 100)

        self.qty_display = tk.Label(card,
                                    text=str(self._qty_value),
                                    fg=accent, bg=self.theme["card_bg"],
                                    font=self.font_hold_big)
        self.qty_display.place(x=pad, y=pad + 130)

        minus_btn = tk.Button(card, text="−",
                              fg=self.theme["fg"],
                              bg=self.theme["elevated_bg"],
                              font=self.font_count, bd=0,
                              width=3, command=lambda: self._qty_adjust(-1))
        minus_btn.place(x=200, y=pad + 130, height=56)

        plus_btn = tk.Button(card, text="+",
                             fg=self.theme["fg"],
                             bg=self.theme["elevated_bg"],
                             font=self.font_count, bd=0,
                             width=3, command=lambda: self._qty_adjust(1))
        plus_btn.place(x=320, y=pad + 130, height=56)

        confirm_btn = tk.Button(card, text="CONFIRM",
                                fg="#FFFFFF", bg=ACCENT_BLUE,
                                activebackground="#4A8AE0",
                                font=self.font_btn_lg, bd=0,
                                padx=40, pady=12,
                                command=self._qty_commit)
        confirm_btn.place(x=pad, y=pad + 260, width=560, height=52)

    def _qty_adjust(self, delta):
        self._qty_value = max(1, self._qty_value + delta)
        accent = SLOT_COLORS.get(self._qty_slot, ACCENT_BLUE)
        self.qty_display.configure(text=str(self._qty_value), fg=accent)

    def _qty_commit(self):
        key = self._qty_slot
        if key and key in self.med_data:
            self.med_data[key]["count"] = self._qty_value
            self.med_data[key]["loaded"] = True
            self._save_med()
        self.overlay_frame.place_forget()
        self._show_mode(self.mode)

    # ══════════════════════════════════════════════════════════════════════
    #  DISPENSING FLOW — Circular hold progress
    # ══════════════════════════════════════════════════════════════════════
    def _start_dispense(self, pill_key):
        md = self.med_data.get(pill_key, {})
        if not md.get("loaded") or md.get("count", 0) <= 0:
            return
        self.dispense_pill = pill_key
        self.dispense_state = 2
        self._prev_mode = self.mode
        self.overlay_frame.place(x=0, y=0,
                                 width=CONTENT_W, height=SCREEN_H)
        self._raise_widget(self.overlay_frame)
        self._show_dispense_hold()

    def _end_dispense(self):
        self.dispense_state = 0
        self.dispense_pill = None
        if self.hold_after_id:
            self.root.after_cancel(self.hold_after_id)
            self.hold_after_id = None
        self.overlay_frame.place_forget()
        self._show_mode(self._prev_mode if hasattr(self, '_prev_mode') else "home")

    def _show_dispense_hold(self):
        for w in self.overlay_frame.winfo_children():
            w.destroy()

        md = self.med_data[self.dispense_pill]
        accent = SLOT_COLORS[self.dispense_pill]

        # HOLD SCREEN TO DISPENSE
        tk.Label(self.overlay_frame, text="HOLD SCREEN TO DISPENSE",
                 font=self.font_label,
                 bg=self.theme["bg"],
                 fg=self.theme["muted"]).place(relx=0.5, y=70, anchor="n")

        tk.Label(self.overlay_frame, text=md["name"],
                 font=self.font_name_lg,
                 bg=self.theme["bg"],
                 fg=accent).place(relx=0.5, y=96, anchor="n")

        cnt = md["count"]
        tk.Label(self.overlay_frame,
                 text=f"{cnt} pill{'s' if cnt != 1 else ''} remaining",
                 font=self.font_body,
                 bg=self.theme["bg"],
                 fg=self.theme["muted"]).place(relx=0.5, y=132, anchor="n")

        # Circular progress ring
        ring_size = 200
        self.hold_ring = tk.Canvas(self.overlay_frame,
                                   width=ring_size, height=ring_size,
                                   bg=self.theme["bg"],
                                   highlightthickness=0)
        ring_x = (CONTENT_W - ring_size) // 2
        self.hold_ring.place(x=ring_x, y=168)
        self._hold_accent = accent
        self._draw_hold_ring(0)

        # Countdown text in center of ring
        self.hold_countdown = tk.Label(self.overlay_frame, text="3.0",
                                       font=self.font_hold_big,
                                       bg=self.theme["card_bg"],
                                       fg=self.theme["fg"])
        self.hold_countdown.place(x=ring_x + ring_size // 2,
                                  y=168 + ring_size // 2 - 20,
                                  anchor="center")

        self.hold_sec_label = tk.Label(self.overlay_frame, text="SECONDS",
                                       font=self.font_hold_label,
                                       bg=self.theme["card_bg"],
                                       fg=self.theme["muted"])
        self.hold_sec_label.place(x=ring_x + ring_size // 2,
                                  y=168 + ring_size // 2 + 20,
                                  anchor="center")

        # Cancel button
        cancel_btn = tk.Button(self.overlay_frame, text="Cancel",
                               font=self.font_body_bold,
                               bg=self.theme["elevated_bg"],
                               fg=self.theme["fg"],
                               activebackground=self.theme["btn_active"],
                               bd=0, padx=30, pady=10,
                               command=self._end_dispense)
        cancel_btn.place(relx=0.5, y=388, anchor="n", width=160, height=52)

        self._bind_hold_recursive(self.overlay_frame)

    def _draw_hold_ring(self, progress):
        c = self.hold_ring
        c.delete("all")
        size = 200
        pad = 0
        # Background ring
        c.create_oval(pad, pad, size - pad, size - pad,
                      outline=self.theme["elevated_bg"], width=14)
        # Progress arc
        if progress > 0:
            extent = progress * 360
            c.create_arc(pad, pad, size - pad, size - pad,
                         start=90, extent=-extent,
                         outline=self._hold_accent, width=14,
                         style="arc")
        # Center fill
        inner_pad = 14
        c.create_oval(inner_pad, inner_pad, size - inner_pad, size - inner_pad,
                      fill=self.theme["card_bg"], outline="")

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
        try:
            self._draw_hold_ring(0)
            self.hold_countdown.configure(text="3.0")
        except Exception:
            pass

    def _hold_update(self):
        if self.dispense_state != 2 or self.hold_start == 0:
            return
        elapsed = time.time() - self.hold_start
        frac = min(elapsed / HOLD_TIME, 1.0)
        try:
            self._draw_hold_ring(frac)
            remaining = max(0, HOLD_TIME - elapsed)
            self.hold_countdown.configure(text=f"{remaining:.1f}")
        except Exception:
            pass

        if elapsed >= HOLD_TIME:
            self.hold_start = 0
            self._confirm_dispense()
            return

        self.hold_after_id = self.root.after(50, self._hold_update)

    def _confirm_dispense(self):
        key = self.dispense_pill
        md = self.med_data[key]
        if md["count"] > 0:
            md["count"] -= 1
        if md["count"] <= 0:
            md["count"] = 0
        self._save_med()
        self.dispense_state = 4
        self._play_sound()
        self._show_dispense_done()

    def _show_dispense_done(self):
        for w in self.overlay_frame.winfo_children():
            w.destroy()

        md = self.med_data[self.dispense_pill]
        accent = SLOT_COLORS[self.dispense_pill]

        tk.Label(self.overlay_frame, text="DISPENSED",
                 font=self.font_label,
                 bg=self.theme["bg"],
                 fg=self.theme["muted"]).place(relx=0.5, y=68, anchor="n")

        tk.Label(self.overlay_frame, text=md["name"],
                 font=self.font_name_lg,
                 bg=self.theme["bg"],
                 fg=accent).place(relx=0.5, y=92, anchor="n")

        # Checkmark circle
        check_size = 96
        check_c = tk.Canvas(self.overlay_frame, width=check_size,
                            height=check_size, bg=self.theme["bg"],
                            highlightthickness=0)
        check_c.place(x=(CONTENT_W - check_size) // 2, y=140)
        check_c.create_oval(0, 0, check_size, check_size,
                            fill=ACCENT_BLUE, outline="")
        # Draw checkmark
        check_c.create_line(28, 48, 40, 62, fill="#FFFFFF", width=5,
                            capstyle="round")
        check_c.create_line(40, 62, 68, 34, fill="#FFFFFF", width=5,
                            capstyle="round")

        remaining = md["count"]
        text = (f"{remaining} pill{'s' if remaining != 1 else ''} remaining"
                if remaining > 0 else "Slot empty — scan QR to reload")
        tk.Label(self.overlay_frame, text=text,
                 font=self.font_title,
                 bg=self.theme["bg"],
                 fg=self.theme["muted"]).place(relx=0.5, y=280, anchor="n")

        self.root.after(int(DISPENSED_TIME * 1000), self._end_dispense)

    def _play_sound(self):
        if not self.settings.get("alarm_sound", True):
            return
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
    #  PILL BOTTLE ANIMATION
    # ══════════════════════════════════════════════════════════════════════
    def _animate_pill_bottle(self, slot_key, direction):
        self._anim_queue.append((slot_key, direction))
        if not self._anim_running:
            self._run_next_anim()

    def _run_next_anim(self):
        if not self._anim_queue:
            self._anim_running = False
            return
        self._anim_running = True
        slot_key, direction = self._anim_queue.pop(0)
        self._do_bottle_anim(slot_key, direction)

    def _do_bottle_anim(self, slot_key, direction):
        accent = SLOT_COLORS[slot_key]
        md = self.med_data.get(slot_key, {})
        name = md.get("name", slot_key.capitalize())

        c = self.anim_canvas
        c.delete("all")

        # Semi-transparent overlay background
        c.create_rectangle(0, 0, CONTENT_W, SCREEN_H,
                           fill="#000000", stipple="gray25", outline="")

        bw, bh = 120, 180
        cx = CONTENT_W // 2
        r = 16

        if direction == "up":
            start_y = (SCREEN_H - bh) // 2
            end_y = -bh - 20
            label_text = f"{name} removed"
        else:
            start_y = SCREEN_H + 20
            end_y = (SCREEN_H - bh) // 2
            label_text = f"{name} placed"

        x1 = cx - bw // 2
        x2 = cx + bw // 2

        # Bottle body
        bottle = self._canvas_rounded_rect(c, x1, start_y,
                                            x2, start_y + bh,
                                            r, fill=accent, outline="")
        # Cap
        cap_h = 26
        cap = c.create_rectangle(x1, start_y, x2, start_y + cap_h,
                                  fill=self._darken(accent), outline="")
        # Label on bottle
        text_id = c.create_text(cx, start_y + bh // 2 + 20,
                                text=name, fill="#0A0A0C",
                                font=self.font_small_bold)
        # Status text
        status_id = c.create_text(cx, SCREEN_H - 40,
                                   text=label_text,
                                   fill="#FFFFFF",
                                   font=self.font_body_bold)

        c.place(x=0, y=0, width=CONTENT_W, height=SCREEN_H)
        self._raise_widget(c)

        items = [bottle, cap, text_id]
        total_frames = 18
        total_dist = end_y - start_y
        self._anim_frame(items, 0, total_frames, total_dist, c, status_id)

    def _anim_frame(self, items, frame, total, total_dist, canvas,
                    status_id=None):
        if frame > total:
            self.root.after(300, self._anim_cleanup)
            return
        t = frame / total
        ease = t * t * (3 - 2 * t)
        dy = total_dist * ease
        prev_ease = ((frame - 1) / total) if frame > 0 else 0
        prev_ease = prev_ease * prev_ease * (3 - 2 * prev_ease)
        prev_dy = total_dist * prev_ease
        delta = dy - prev_dy
        for item in items:
            canvas.move(item, 0, delta)
        self.root.after(22, self._anim_frame,
                        items, frame + 1, total, total_dist, canvas,
                        status_id)

    def _anim_cleanup(self):
        self.anim_canvas.delete("all")
        self.anim_canvas.place_forget()
        self._run_next_anim()

    @staticmethod
    def _darken(hex_color):
        try:
            r = max(0, int(hex_color[1:3], 16) - 40)
            g = max(0, int(hex_color[3:5], 16) - 40)
            b = max(0, int(hex_color[5:7], 16) - 40)
            return f"#{r:02x}{g:02x}{b:02x}"
        except Exception:
            return "#333333"

    # ══════════════════════════════════════════════════════════════════════
    #  MPR121 TOUCH POLLING
    # ══════════════════════════════════════════════════════════════════════
    def _poll_touch(self):
        if not self.mpr:
            return
        try:
            for key in SLOT_KEYS:
                pad = SLOT_PADS[key]
                touched = self.mpr[pad].value
                was = self.mpr_prev[pad]
                if touched and not was:
                    self._on_pad_press(key)
                self.mpr_prev[pad] = touched
        except Exception:
            pass
        self.root.after(50, self._poll_touch)

    def _on_pad_press(self, key):
        if self.dispense_state == 0:
            if self._is_loaded(key) and self._get_count(key) > 0:
                self._start_dispense(key)

    # ══════════════════════════════════════════════════════════════════════
    #  CAMERA / QR SCANNING
    # ══════════════════════════════════════════════════════════════════════
    def _start_camera(self):
        try:
            self.camera = Picamera2()
            config = self.camera.create_preview_configuration(
                main={"size": (1280, 720), "format": "RGB888"})
            self.camera.configure(config)
            self.camera.start()
            try:
                self.camera.set_controls({"AfMode": 2})
            except Exception:
                pass
            self.camera_running = True
            threading.Thread(target=self._camera_loop, daemon=True).start()
        except Exception:
            self.camera = None
            self.camera_running = False

    def _camera_loop(self):
        while self.camera_running:
            try:
                frame = self.camera.capture_array()
                img = Image.fromarray(frame[:, :, ::-1])
                results = pyzbar_decode(img)
                if results:
                    qr_with_pos = []
                    for r in results:
                        text = r.data.decode("utf-8", errors="ignore").strip()
                        x_pos = r.rect.left if r.rect else 0
                        qr_with_pos.append((x_pos, text))
                    qr_with_pos.sort(key=lambda p: p[0], reverse=True)
                    slot_assignments = []
                    for idx, (x_pos, text) in enumerate(qr_with_pos):
                        if idx < len(SLOT_KEYS):
                            slot_assignments.append(
                                (SLOT_KEYS[idx], text))
                    self.root.after(0, self._handle_qr_batch,
                                   slot_assignments)
                time.sleep(0.5)
            except Exception:
                time.sleep(1)

    def _parse_qr_med_name(self, raw_text):
        try:
            payload = json.loads(raw_text)
            return payload.get("med", "Medication")
        except (json.JSONDecodeError, AttributeError):
            return raw_text.strip().capitalize() or "Medication"

    def _handle_qr_batch(self, slot_assignments):
        now = time.time()
        for slot, raw_text in slot_assignments:
            med_name = self._parse_qr_med_name(raw_text)
            self.qr_last_seen[slot] = now

            md = self.med_data[slot]
            if self.dispense_state == 0:
                if not md.get("loaded"):
                    md["name"] = med_name
                    md["take_with"] = ""
                    self._save_med()
                    self._show_qty_confirm(slot, med_name)
                    return
                elif md.get("count", 0) <= 0:
                    md["name"] = med_name
                    self._save_med()
                    self._show_qty_confirm(slot, med_name)
                    return

    # ── Drawing helpers ────────────────────────────────────────────────────
    @staticmethod
    def _canvas_rounded_rect(canvas, x1, y1, x2, y2, r, **kwargs):
        points = [
            x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1,
            x2, y1, x2, y1 + r, x2, y1 + r, x2, y2 - r,
            x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2,
            x1 + r, y2, x1 + r, y2, x1, y2, x1, y2 - r,
            x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1,
        ]
        return canvas.create_polygon(points, smooth=True, **kwargs)

    def _draw_rounded_rect(self, canvas, x1, y1, x2, y2, r, fill):
        self._canvas_rounded_rect(canvas, x1, y1, x2, y2, r,
                                  fill=fill, outline="")

    # ── Quit ───────────────────────────────────────────────────────────────
    def _quit(self):
        try:
            if self.camera and self.camera_running:
                self.camera_running = False
                self.camera.stop()
                self.camera.close()
        except Exception:
            pass
        self.root.destroy()

    # ── Run ────────────────────────────────────────────────────────────────
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


if __name__ == "__main__":
    try:
        app = DoseApp()
        app.run()
    except Exception:
        import traceback
        err = traceback.format_exc()
        log_path = os.path.join(APP_DIR, "crash.log")
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "w") as f:
                f.write(err)
        except Exception:
            pass
        print(f"\n  DOSE crashed:\n\n{err}")
        print(f"\n  Log: {log_path}")
        print("\n  Press Enter to close...")
        try:
            input()
        except Exception:
            pass
