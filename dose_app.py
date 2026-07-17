#!/usr/bin/env python3
"""
DOSE Home Station — Prototype Application
v3.0 — Standby/Storage/Settings modes + QR-to-load + MPR121 touch + auto-update

Modes: Standby (default), Storage (pill cards + QR load), Settings
Dispensing overlay: READ → HOLD → CONFIRM → DISPENSED

Meds start empty (count 0). Scan a QR to load a slot.
MPR121 pads 0-3 map to blue/red/green/yellow.
Auto-update checks GitHub on launch + UPDATE button in Settings.

Gracefully handles missing hardware.
"""

import json
import hashlib
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

HAVE_MPR121 = False
mpr121_mod = None
try:
    import board
    import busio
    import adafruit_mpr121
    mpr121_mod = adafruit_mpr121
    HAVE_MPR121 = True
except Exception:
    # Auto-install the library if missing
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
MARGIN_LEFT = 52
HOLD_TIME = 3.0
DISPENSED_TIME = 4.0
DEFAULT_QTY = 30
QR_PRESENCE_TIMEOUT = 15.0
ALL_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

CONFIG_PATH = os.path.expanduser("~/.dose_config.json")
DATA_PATH = os.path.expanduser("~/dose-home-station/med_data.json")
APP_DIR = os.path.expanduser("~/dose-home-station")
APP_FILE = os.path.join(APP_DIR, "dose_app.py")
RAW_URL = ("https://raw.githubusercontent.com/relude117-star/"
           "doseconceptprototype/claude/quirky-brown-vkHwi")

SLOT_KEYS = ["blue", "red", "green", "yellow"]

SLOT_DEFS = {
    "blue":   {"accent": "#5B9BFF", "pad": 0},
    "red":    {"accent": "#FF6B6B", "pad": 1},
    "green":  {"accent": "#5BD08A", "pad": 2},
    "yellow": {"accent": "#E6C34A", "pad": 3},
}

DARK_THEME = {
    "bg": "#070708",
    "fg": "#F4F4F2",
    "muted": "#7E8186",
    "card_bg": "#141418",
    "btn_bg": "#1E1E24",
    "btn_active": "#2A2A32",
    "popup_bg": "#1A1A20",
}

LIGHT_THEME = {
    "bg": "#F4F4F2",
    "fg": "#1A1A1C",
    "muted": "#888888",
    "card_bg": "#E8E8E6",
    "btn_bg": "#DCDCDA",
    "btn_active": "#D0D0CE",
    "popup_bg": "#E0E0DE",
}

def _find_logo():
    """Find dose_logo.png — check app dir, script dir, then cwd."""
    candidates = [
        os.path.join(APP_DIR, "dose_logo.png"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "dose_logo.png"),
        "dose_logo.png",
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


# ── Helpers ────────────────────────────────────────────────────────────────
def _raise(widget):
    """Raise widget in stacking order — safe for Canvas too."""
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


# ═══════════════════════════════════════════════════════════════════════════
#  DoseApp
# ═══════════════════════════════════════════════════════════════════════════
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
        self.font_count = tkfont.Font(family=base, size=48, weight="bold")

        # ── State ──────────────────────────────────────────────────────────
        self.med_data = {}
        self.settings = {"night_mode": False, "alarm_sound": True,
                         "constant_scan": False}
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
        self.mpr = None
        self.has_touch = False
        self.mpr_prev = [False] * 12
        self.qr_last_seen = {k: 0.0 for k in SLOT_KEYS}
        self._standby_prev_keys = None
        self._storage_prev_visible = None

        # ── Config + Med data ──────────────────────────────────────────────
        self._load_config()
        self._init_med_data()
        self._apply_theme_colors()

        # ── MPR121 init ────────────────────────────────────────────────────
        self.touch_error = ""
        if HAVE_MPR121:
            # Try to load i2c-dev kernel module
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

        # ── Root frames ────────────────────────────────────────────────────
        self.main_frame = tk.Frame(self.root, bg=self.theme["bg"],
                                   width=SCREEN_W, height=SCREEN_H)
        self.main_frame.place(x=0, y=0, width=SCREEN_W, height=SCREEN_H)

        self.overlay_frame = tk.Frame(self.root, bg=self.theme["bg"],
                                      width=SCREEN_W, height=SCREEN_H)

        # D button canvas — 80x80 in bottom-right
        self.d_btn_size = 80
        self.d_btn_canvas = tk.Canvas(self.root,
                                      width=self.d_btn_size,
                                      height=self.d_btn_size,
                                      highlightthickness=0,
                                      bg=self.theme["bg"], bd=0)
        self.d_btn_canvas.place(x=SCREEN_W - self.d_btn_size - 10,
                                y=SCREEN_H - self.d_btn_size - 10)
        self._draw_d_button()
        self.d_btn_canvas.bind("<Button-1>", self._on_d_pressed)

        # Popup menu frame
        self.popup_frame = tk.Frame(self.root, bg=self.theme["popup_bg"],
                                    highlightbackground=self.theme["muted"],
                                    highlightthickness=1)

        # ── Bindings ───────────────────────────────────────────────────────
        self.root.bind("<Escape>", lambda e: self._quit())

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
        if self.has_touch:
            self._poll_touch()

        # Auto-check for updates on launch
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
        self.main_frame.configure(bg=self.theme["bg"])
        self.overlay_frame.configure(bg=self.theme["bg"])
        self.d_btn_canvas.configure(bg=self.theme["bg"])
        self.popup_frame.configure(bg=self.theme["popup_bg"],
                                   highlightbackground=self.theme["muted"])
        self._draw_d_button()
        self._build_standby()
        self._build_storage()
        self._build_settings()
        self._build_overlay()
        self._show_mode(self.mode)

    # ── Canvas helper ──────────────────────────────────────────────────────
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

    # ── D Button drawing ───────────────────────────────────────────────────
    def _draw_d_button(self):
        s = self.d_btn_size
        self.d_btn_canvas.delete("all")
        loaded = False
        try:
            if PIL_AVAILABLE:
                logo_path = _find_logo()
                if logo_path:
                    img = Image.open(logo_path)
                    resample = getattr(Image, 'LANCZOS',
                                       getattr(Image, 'ANTIALIAS', None))
                    img = img.resize((s, s), resample)
                    self._d_logo_img = ImageTk.PhotoImage(img)
                    self.d_btn_canvas.create_image(s // 2, s // 2,
                                                   image=self._d_logo_img)
                    loaded = True
        except Exception:
            pass
        if not loaded:
            self._canvas_rounded_rect(self.d_btn_canvas, 2, 2, s - 2, s - 2,
                                      16, fill="#4A90D9", outline="")
            self.d_btn_canvas.create_text(s // 2, s // 2, text="D",
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

        self.popup_frame.place(x=620, y=290, width=160)
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
            self._standby_prev_keys = None
            self._update_standby()
        elif mode == "storage":
            self.storage_frame.place(x=0, y=0,
                                     width=SCREEN_W, height=SCREEN_H)
            self._storage_prev_visible = None
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
        if self.mode == "standby":
            self._update_standby()
        elif self.mode == "storage":
            self._update_storage()
        self.root.after(1000, self._tick_clock)

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
        self.clock_label.place(x=MARGIN_LEFT, y=20)

        # WiFi icon
        wifi_canvas = tk.Canvas(self.standby_frame, width=40, height=32,
                                bg=self.theme["bg"], highlightthickness=0)
        wifi_canvas.place(x=SCREEN_W - MARGIN_LEFT - 40, y=25)
        self._draw_wifi(wifi_canvas, self.theme["muted"])

        # TODAY'S SCHEDULE label
        tk.Label(self.standby_frame, text="TODAY'S SCHEDULE",
                 font=self.font_label,
                 bg=self.theme["bg"],
                 fg=self.theme["muted"]).place(x=MARGIN_LEFT, y=80)

        # Schedule list — up to 4 card rows
        self.standby_sched_rows = []
        for i in range(4):
            y = 108 + i * 82
            card = tk.Frame(self.standby_frame, bg=self.theme["card_bg"])
            card.place(x=MARGIN_LEFT, y=y, width=620, height=72)

            color_bar = tk.Frame(card, bg="#5B9BFF", width=5)
            color_bar.place(x=0, y=0, width=5, height=72)

            time_lbl = tk.Label(card, text="",
                                font=self.font_title,
                                bg=self.theme["card_bg"],
                                fg=self.theme["fg"], anchor="w")
            time_lbl.place(x=20, y=8)

            name_lbl = tk.Label(card, text="",
                                font=self.font_small,
                                bg=self.theme["card_bg"],
                                fg=self.theme["muted"], anchor="w")
            name_lbl.place(x=20, y=40)

            count_lbl = tk.Label(card, text="",
                                 font=self.font_body_bold,
                                 bg=self.theme["card_bg"],
                                 fg=self.theme["fg"], anchor="e")
            count_lbl.place(x=480, y=20)

            self.standby_sched_rows.append({
                "card": card, "color_bar": color_bar,
                "time_lbl": time_lbl, "name_lbl": name_lbl,
                "count_lbl": count_lbl,
            })

        # Empty state / scan hint
        self.standby_empty_label = tk.Label(self.standby_frame, text="",
                                            font=self.font_body,
                                            bg=self.theme["bg"],
                                            fg=self.theme["muted"])
        self.standby_empty_label.place(x=MARGIN_LEFT, y=200)

        # Hardware status at bottom
        hw_parts = []
        if not CAMERA_AVAILABLE:
            hw_parts.append("Camera not connected")
        if not self.has_touch:
            msg = "Touch: " + (self.touch_error or "not detected")
            hw_parts.append(msg)
        if hw_parts:
            tk.Label(self.standby_frame, text="  ·  ".join(hw_parts),
                     font=self.font_small,
                     bg=self.theme["bg"],
                     fg="#444444", wraplength=600, justify="left"
                     ).place(x=MARGIN_LEFT, y=SCREEN_H - 50)

        self.standby_frame.bind("<Button-1>", self._standby_tap)

    def _standby_tap(self, event=None):
        for key in SLOT_KEYS:
            if (self._is_loaded(key) and self._get_count(key) > 0
                    and self._is_qr_present(key)):
                self._start_dispense(key)
                return

    def _update_standby(self):
        today_sched = self._get_today_schedule()
        visible_keys = tuple(e["key"] for e in today_sched[:4])
        layout_changed = (visible_keys != self._standby_prev_keys)

        if layout_changed:
            self._standby_prev_keys = visible_keys
            for row in self.standby_sched_rows:
                row["card"].place_forget()

        if not today_sched:
            if layout_changed:
                self.standby_empty_label.configure(
                    text="Place medications in view to see schedule")
                self.standby_empty_label.place(x=MARGIN_LEFT, y=200)
        else:
            if layout_changed:
                self.standby_empty_label.place_forget()
            for i, entry in enumerate(today_sched[:4]):
                row = self.standby_sched_rows[i]
                if layout_changed:
                    y = 108 + i * 82
                    row["card"].place(x=MARGIN_LEFT, y=y,
                                      width=620, height=72)
                row["color_bar"].configure(bg=entry["accent"])
                row["time_lbl"].configure(text=entry["time"])
                row["name_lbl"].configure(text=entry["name"])
                row["count_lbl"].configure(
                    text=f"{entry['count']} pills",
                    fg=entry["accent"])

    def _is_qr_present(self, key):
        """True if this slot's QR was seen within the last 15 seconds."""
        return (time.time() - self.qr_last_seen.get(key, 0)) < QR_PRESENCE_TIMEOUT

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
            sched = md.get("schedule_time", "8:00 AM")
            try:
                t = datetime.strptime(sched, "%I:%M %p").replace(
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
                "time": display,
                "name": md["name"],
                "count": md.get("count", 0),
                "accent": SLOT_DEFS[key]["accent"],
                "sort": sort_key,
            })
        entries.sort(key=lambda e: e["sort"])
        return entries

    # ══════════════════════════════════════════════════════════════════════
    #  STORAGE MODE — clean vertical list + detail panel
    # ══════════════════════════════════════════════════════════════════════
    def _build_storage(self):
        if hasattr(self, 'storage_frame'):
            self.storage_frame.destroy()

        self.storage_frame = tk.Frame(self.main_frame,
                                      bg=self.theme["bg"],
                                      width=SCREEN_W, height=SCREEN_H)

        # ── Left: vertical pill list (big touch targets) ──
        left_w = 260
        left = tk.Frame(self.storage_frame, bg=self.theme["bg"],
                        width=left_w, height=SCREEN_H)
        left.place(x=0, y=0, width=left_w, height=SCREEN_H)

        tk.Label(left, text="MEDICATIONS", font=self.font_label,
                 bg=self.theme["bg"],
                 fg=self.theme["muted"]).place(x=20, y=16)

        self.slot_rows = {}
        for i, key in enumerate(SLOT_KEYS):
            y = 48 + i * 108
            accent = SLOT_DEFS[key]["accent"]

            row = tk.Frame(left, bg=self.theme["card_bg"],
                           highlightthickness=0, cursor="hand2")

            # Thick color bar on left
            tk.Frame(row, bg=accent, width=5).place(
                x=0, y=0, width=5, height=96)

            # Color dot
            dot = tk.Canvas(row, width=14, height=14,
                            bg=self.theme["card_bg"],
                            highlightthickness=0)
            dot.place(x=18, y=14)
            dot.create_oval(1, 1, 13, 13, fill=accent, outline="")

            name_lbl = tk.Label(row, text="—", fg=self.theme["fg"],
                                bg=self.theme["card_bg"],
                                font=self.font_small_bold, anchor="w")
            name_lbl.place(x=40, y=8)

            info_lbl = tk.Label(row, text="Not loaded",
                                fg=self.theme["muted"],
                                bg=self.theme["card_bg"],
                                font=self.font_small, anchor="w")
            info_lbl.place(x=40, y=32)

            count_lbl = tk.Label(row, text="", fg=accent,
                                 bg=self.theme["card_bg"],
                                 font=self.font_body_bold, anchor="w")
            count_lbl.place(x=40, y=58)

            for w in [row, name_lbl, info_lbl, count_lbl, dot]:
                w.bind("<Button-1>",
                       lambda e, k=key: self._storage_select(k))

            self.slot_rows[key] = {
                "row": row, "name_lbl": name_lbl,
                "info_lbl": info_lbl, "count_lbl": count_lbl,
            }

        # ── Right: detail + schedule editor ──
        rx = left_w + 4
        rw = SCREEN_W - rx
        self.detail_panel = tk.Frame(self.storage_frame,
                                     bg=self.theme["card_bg"],
                                     width=rw, height=SCREEN_H)
        self.detail_panel.place(x=rx, y=0, width=rw, height=SCREEN_H)

        pad = 28

        self.detail_name = tk.Label(self.detail_panel, text="",
                                    fg=self.theme["fg"],
                                    bg=self.theme["card_bg"],
                                    font=self.font_name, anchor="w")
        self.detail_name.place(x=pad, y=18)

        self.detail_count_lbl = tk.Label(self.detail_panel, text="",
                                         fg=self.theme["muted"],
                                         bg=self.theme["card_bg"],
                                         font=self.font_body, anchor="w")
        self.detail_count_lbl.place(x=pad, y=66)

        self.detail_take_lbl = tk.Label(self.detail_panel, text="",
                                        fg=self.theme["fg"],
                                        bg=self.theme["card_bg"],
                                        font=self.font_small, anchor="w",
                                        wraplength=480)
        self.detail_take_lbl.place(x=pad, y=96)

        # Thin separator
        tk.Frame(self.detail_panel, bg=self.theme["muted"],
                 height=1).place(x=pad, y=128, width=rw - 2 * pad)

        # ── Schedule time ──
        tk.Label(self.detail_panel, text="SCHEDULE",
                 font=self.font_label,
                 bg=self.theme["card_bg"],
                 fg=self.theme["muted"]).place(x=pad, y=140)

        tf = tk.Frame(self.detail_panel, bg=self.theme["card_bg"])
        tf.place(x=pad, y=162)

        self._sched_hour = 8
        self._sched_min = 0
        self._sched_ampm = "AM"

        btn_cfg = dict(font=self.font_small,
                       bg=self.theme["btn_bg"], fg=self.theme["fg"],
                       activebackground=self.theme["btn_active"],
                       bd=0, width=3, pady=4)

        tk.Button(tf, text="▲", command=lambda: self._adj_sched("hour", 1),
                  **btn_cfg).grid(row=0, column=0, padx=3, pady=1)
        self.hour_label = tk.Label(tf, text="8",
                                   font=self.font_body_bold,
                                   bg=self.theme["card_bg"],
                                   fg=self.theme["fg"], width=3)
        self.hour_label.grid(row=1, column=0, padx=3)
        tk.Button(tf, text="▼", command=lambda: self._adj_sched("hour", -1),
                  **btn_cfg).grid(row=2, column=0, padx=3, pady=1)

        tk.Label(tf, text=":", font=self.font_body_bold,
                 bg=self.theme["card_bg"],
                 fg=self.theme["fg"]).grid(row=1, column=1)

        tk.Button(tf, text="▲", command=lambda: self._adj_sched("min", 1),
                  **btn_cfg).grid(row=0, column=2, padx=3, pady=1)
        self.min_label = tk.Label(tf, text="00",
                                  font=self.font_body_bold,
                                  bg=self.theme["card_bg"],
                                  fg=self.theme["fg"], width=3)
        self.min_label.grid(row=1, column=2, padx=3)
        tk.Button(tf, text="▼", command=lambda: self._adj_sched("min", -1),
                  **btn_cfg).grid(row=2, column=2, padx=3, pady=1)

        tk.Button(tf, text="▲", command=lambda: self._adj_sched("ampm", 1),
                  **btn_cfg).grid(row=0, column=3, padx=(12, 3), pady=1)
        self.ampm_label = tk.Label(tf, text="AM",
                                   font=self.font_body_bold,
                                   bg=self.theme["card_bg"],
                                   fg=self.theme["fg"], width=3)
        self.ampm_label.grid(row=1, column=3, padx=(12, 3))
        tk.Button(tf, text="▼", command=lambda: self._adj_sched("ampm", -1),
                  **btn_cfg).grid(row=2, column=3, padx=(12, 3), pady=1)

        # ── Day toggles ──
        tk.Label(self.detail_panel, text="DAYS",
                 font=self.font_label,
                 bg=self.theme["card_bg"],
                 fg=self.theme["muted"]).place(x=pad, y=270)

        df = tk.Frame(self.detail_panel, bg=self.theme["card_bg"])
        df.place(x=pad, y=294)

        self.day_buttons = {}
        for d in ALL_DAYS:
            btn = tk.Button(df, text=d[:2],
                            font=self.font_small_bold,
                            bg=self.theme["btn_bg"],
                            fg=self.theme["fg"],
                            activebackground=self.theme["btn_active"],
                            bd=0, width=3, pady=6,
                            command=lambda dd=d: self._toggle_day(dd))
            btn.pack(side="left", padx=3)
            self.day_buttons[d] = btn

        # ── Dispense ──
        self.detail_dispense_btn = tk.Button(
            self.detail_panel, text="DISPENSE",
            font=self.font_btn_lg, bg="#4A90D9", fg="#FFFFFF",
            activebackground="#3A7BC8", activeforeground="#FFFFFF",
            bd=0, padx=50, pady=14,
            command=self._detail_dispense)
        self.detail_dispense_btn.place(x=pad, y=370)

        self.detail_hint = tk.Label(self.detail_panel, text="",
                                    fg=self.theme["muted"],
                                    bg=self.theme["card_bg"],
                                    font=self.font_small, anchor="w")
        self.detail_hint.place(x=pad, y=430)

    def _storage_select(self, key):
        self.selected_pill = key
        self._update_storage_detail()

    def _detail_dispense(self):
        key = self.selected_pill
        if self._is_loaded(key) and self._get_count(key) > 0:
            self._start_dispense(key)

    def _adj_sched(self, field, delta):
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

        new_time = (f"{self._sched_hour}:{self._sched_min:02d} "
                    f"{self._sched_ampm}")
        key = self.selected_pill
        self.med_data[key]["schedule_time"] = new_time
        self._save_med()

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
        accent = SLOT_DEFS[key]["accent"]
        days = md.get("schedule_days", [])
        for d, btn in self.day_buttons.items():
            if d in days:
                btn.configure(bg=accent, fg="#FFFFFF")
            else:
                btn.configure(bg=self.theme["btn_bg"],
                              fg=self.theme["fg"])

    def _update_storage_detail(self):
        key = self.selected_pill
        md = self.med_data[key]
        accent = SLOT_DEFS[key]["accent"]

        if md.get("loaded") and self._is_qr_present(key):
            self.detail_name.configure(text=md["name"], fg=accent)
            self.detail_count_lbl.configure(
                text=f"{md['count']} pills remaining")
            take = md.get("take_with", "")
            self.detail_take_lbl.configure(
                text=take if take else "")

            sched = md.get("schedule_time", "8:00 AM")
            try:
                parts = sched.split()
                tp = parts[0].split(":")
                self._sched_hour = int(tp[0])
                self._sched_min = int(tp[1])
                self._sched_ampm = parts[1] if len(parts) > 1 else "AM"
            except Exception:
                self._sched_hour, self._sched_min = 8, 0
                self._sched_ampm = "AM"
            self.hour_label.configure(text=str(self._sched_hour))
            self.min_label.configure(text=f"{self._sched_min:02d}")
            self.ampm_label.configure(text=self._sched_ampm)
            self._update_day_buttons()

            if md["count"] > 0:
                self.detail_dispense_btn.configure(
                    state="normal", bg=accent)
                self.detail_hint.configure(text="")
            else:
                self.detail_dispense_btn.configure(
                    state="disabled", bg=self.theme["btn_bg"])
                self.detail_hint.configure(text="Scan QR to reload")
        else:
            self.detail_name.configure(
                text=f"{key.capitalize()} Slot",
                fg=self.theme["muted"])
            self.detail_count_lbl.configure(text="Not loaded")
            self.detail_take_lbl.configure(
                text="Scan a QR code to load this slot")
            self.hour_label.configure(text="8")
            self.min_label.configure(text="00")
            self.ampm_label.configure(text="AM")
            self._update_day_buttons()
            self.detail_dispense_btn.configure(
                state="disabled", bg=self.theme["btn_bg"])
            self.detail_hint.configure(text="")

        # Highlight selected row
        for k in SLOT_KEYS:
            r = self.slot_rows[k]
            if k == key:
                r["row"].configure(
                    highlightbackground=SLOT_DEFS[k]["accent"],
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
            accent = SLOT_DEFS[key]["accent"]
            present = self._is_qr_present(key)
            if md.get("loaded") and present:
                r["name_lbl"].configure(text=md["name"])
                r["info_lbl"].configure(
                    text=md.get("schedule_time", ""),
                    fg=self.theme["fg"])
                r["count_lbl"].configure(
                    text=f"{md['count']} pills", fg=accent)
                if layout_changed:
                    r["row"].place(x=12, y=48 + SLOT_KEYS.index(key) * 108,
                                   width=236, height=96)
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
            self.detail_take_lbl.configure(text="")
            self.detail_dispense_btn.configure(
                state="disabled", bg=self.theme["btn_bg"])
            self.detail_hint.configure(text="")

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

        accent = SLOT_DEFS[slot_key]["accent"]

        self.overlay_frame.place(x=0, y=0,
                                 width=SCREEN_W, height=SCREEN_H)
        self._raise_widget(self.overlay_frame)
        self._raise_widget(self.d_btn_canvas)

        tk.Label(self.overlay_frame, text="MEDICATION LOADED",
                 fg=self.theme["muted"], bg=self.theme["bg"],
                 font=self.font_label).place(x=MARGIN_LEFT, y=40)

        tk.Label(self.overlay_frame, text=med_name,
                 fg=accent, bg=self.theme["bg"],
                 font=self.font_name).place(x=MARGIN_LEFT, y=70)

        tk.Label(self.overlay_frame, text="HOW MANY PILLS?",
                 fg=self.theme["muted"], bg=self.theme["bg"],
                 font=self.font_label).place(x=MARGIN_LEFT, y=150)

        self.qty_display = tk.Label(self.overlay_frame,
                                    text=str(self._qty_value),
                                    fg=accent, bg=self.theme["bg"],
                                    font=self.font_count)
        self.qty_display.place(x=MARGIN_LEFT, y=175)

        minus_btn = tk.Label(self.overlay_frame, text="−",
                             fg=self.theme["fg"], bg=self.theme["btn_bg"],
                             font=self.font_bold_lg, width=3, cursor="hand2")
        minus_btn.place(x=250, y=180, height=70)
        minus_btn.bind("<Button-1>", lambda _: self._qty_adjust(-1))

        plus_btn = tk.Label(self.overlay_frame, text="+",
                            fg=self.theme["fg"], bg=self.theme["btn_bg"],
                            font=self.font_bold_lg, width=3, cursor="hand2")
        plus_btn.place(x=370, y=180, height=70)
        plus_btn.bind("<Button-1>", lambda _: self._qty_adjust(1))

        confirm_btn = tk.Button(self.overlay_frame, text="CONFIRM",
                                fg="#FFFFFF", bg="#3478F6",
                                activebackground="#2A60C8",
                                activeforeground="#FFFFFF",
                                font=self.font_btn_lg, bd=0,
                                padx=40, pady=12,
                                command=self._qty_commit)
        confirm_btn.place(x=MARGIN_LEFT, y=320)

    def _qty_adjust(self, delta):
        self._qty_value = max(1, self._qty_value + delta)
        accent = SLOT_DEFS.get(self._qty_slot, {}).get("accent", "#5B9BFF")
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
    #  SETTINGS MODE
    # ══════════════════════════════════════════════════════════════════════
    def _build_settings(self):
        if hasattr(self, 'settings_frame'):
            self.settings_frame.destroy()

        self.settings_frame = tk.Frame(self.main_frame,
                                       bg=self.theme["bg"],
                                       width=SCREEN_W, height=SCREEN_H)

        tk.Label(self.settings_frame, text="SETTINGS",
                 font=self.font_label,
                 bg=self.theme["bg"],
                 fg=self.theme["muted"]).place(x=MARGIN_LEFT, y=30)

        # Night mode toggle
        tk.Label(self.settings_frame, text="Day / Night Mode",
                 font=self.font_body,
                 bg=self.theme["bg"],
                 fg=self.theme["fg"]).place(x=MARGIN_LEFT, y=80)

        self.night_toggle = tk.Canvas(self.settings_frame,
                                      width=60, height=30,
                                      bg=self.theme["bg"],
                                      highlightthickness=0)
        self.night_toggle.place(x=400, y=80)
        self._draw_toggle(self.night_toggle,
                          self.settings.get("night_mode", False))
        self.night_toggle.bind("<Button-1>", self._toggle_night)

        # Alarm sound toggle
        tk.Label(self.settings_frame, text="Alarm Sound",
                 font=self.font_body,
                 bg=self.theme["bg"],
                 fg=self.theme["fg"]).place(x=MARGIN_LEFT, y=130)

        self.alarm_toggle = tk.Canvas(self.settings_frame,
                                      width=60, height=30,
                                      bg=self.theme["bg"],
                                      highlightthickness=0)
        self.alarm_toggle.place(x=400, y=130)
        self._draw_toggle(self.alarm_toggle,
                          self.settings.get("alarm_sound", True))
        self.alarm_toggle.bind("<Button-1>", self._toggle_alarm)

        # Update section
        tk.Label(self.settings_frame, text="Check for Updates",
                 font=self.font_body,
                 bg=self.theme["bg"],
                 fg=self.theme["fg"]).place(x=MARGIN_LEFT, y=200)

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

        # Constant scan toggle
        tk.Label(self.settings_frame, text="Constant QR Scan",
                 font=self.font_body,
                 bg=self.theme["bg"],
                 fg=self.theme["fg"]).place(x=MARGIN_LEFT, y=310)

        self.scan_toggle = tk.Canvas(self.settings_frame,
                                     width=60, height=30,
                                     bg=self.theme["bg"],
                                     highlightthickness=0)
        self.scan_toggle.place(x=400, y=310)
        self._draw_toggle(self.scan_toggle,
                          self.settings.get("constant_scan", False))
        self.scan_toggle.bind("<Button-1>", self._toggle_constant_scan)

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
        self._draw_toggle(self.alarm_toggle, self.settings["alarm_sound"])

    def _toggle_constant_scan(self, event=None):
        self.settings["constant_scan"] = not self.settings.get(
            "constant_scan", False)
        self._save_config()
        self._draw_toggle(self.scan_toggle, self.settings["constant_scan"])

    # ── Auto-update ────────────────────────────────────────────────────────
    def _on_update_pressed(self):
        self.update_status.configure(text="Checking...")
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
                self.root.after(0, self._update_result,
                                "Already up to date")
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

                # Also update in APP_DIR
                os.makedirs(APP_DIR, exist_ok=True)
                app_copy = os.path.join(APP_DIR, "dose_app.py")
                with open(app_copy, "wb") as f:
                    f.write(remote_data)

                # Fetch DOSE.sh and logo
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
    #  DISPENSING FLOW (Overlay)
    # ══════════════════════════════════════════════════════════════════════
    def _build_overlay(self):
        for w in self.overlay_frame.winfo_children():
            w.destroy()
        self.overlay_frame.configure(bg=self.theme["bg"])

    def _start_dispense(self, pill_key):
        md = self.med_data.get(pill_key, {})
        if not md.get("loaded") or md.get("count", 0) <= 0:
            return
        self.dispense_pill = pill_key
        self.dispense_state = 2
        self.overlay_frame.place(x=0, y=0,
                                 width=SCREEN_W, height=SCREEN_H)
        self._raise_widget(self.overlay_frame)
        self._raise_widget(self.d_btn_canvas)
        self._show_dispense_hold()

    def _end_dispense(self):
        self.dispense_state = 0
        self.dispense_pill = None
        if self.hold_after_id:
            self.root.after_cancel(self.hold_after_id)
            self.hold_after_id = None
        self.overlay_frame.place_forget()
        self._show_mode(self.mode)

    # ── Stage: HOLD TO DISPENSE ──────────────────────────────────────────────
    def _show_dispense_hold(self):
        for w in self.overlay_frame.winfo_children():
            w.destroy()

        md = self.med_data[self.dispense_pill]
        accent = SLOT_DEFS[self.dispense_pill]["accent"]

        tk.Label(self.overlay_frame, text="HOLD SCREEN TO DISPENSE",
                 font=self.font_label,
                 bg=self.theme["bg"],
                 fg=self.theme["muted"]).place(x=MARGIN_LEFT, y=60)

        lbl_name = tk.Label(self.overlay_frame, text=md["name"],
                            font=self.font_name,
                            bg=self.theme["bg"],
                            fg=accent)
        lbl_name.place(x=MARGIN_LEFT, y=90)

        tk.Label(self.overlay_frame,
                 text=f"{md['count']} pills remaining",
                 font=self.font_body,
                 bg=self.theme["bg"],
                 fg=self.theme["muted"]).place(x=MARGIN_LEFT, y=145)

        self.hold_canvas = tk.Canvas(self.overlay_frame,
                                     width=SCREEN_W - 2 * MARGIN_LEFT,
                                     height=40,
                                     bg=self.theme["card_bg"],
                                     highlightthickness=0)
        self.hold_canvas.place(x=MARGIN_LEFT, y=220)

        self.hold_progress_bar = self.hold_canvas.create_rectangle(
            0, 0, 0, 40, fill=accent, outline="")

        self.hold_label = tk.Label(self.overlay_frame,
                                   text="Hold for 3 seconds...",
                                   font=self.font_body,
                                   bg=self.theme["bg"],
                                   fg=self.theme["fg"])
        self.hold_label.place(x=MARGIN_LEFT, y=280)

        cancel_btn = tk.Button(self.overlay_frame, text="Cancel",
                               font=self.font_btn,
                               bg=self.theme["btn_bg"],
                               fg=self.theme["fg"],
                               activebackground=self.theme["btn_active"],
                               activeforeground=self.theme["fg"],
                               bd=0, padx=30, pady=10,
                               command=self._end_dispense)
        cancel_btn.place(x=MARGIN_LEFT, y=360)

        self.hold_area = tk.Frame(self.overlay_frame,
                                  bg=self.theme["bg"],
                                  width=SCREEN_W, height=SCREEN_H)
        self.hold_area.place(x=0, y=0, width=SCREEN_W, height=SCREEN_H)
        self._raise_widget(self.hold_canvas)
        self._raise_widget(lbl_name)
        self._raise_widget(self.hold_label)
        self._raise_widget(cancel_btn)
        self._raise_widget(self.d_btn_canvas)

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

    # ── Stage 4: DISPENSED ─────────────────────────────────────────────────
    def _show_dispense_done(self):
        for w in self.overlay_frame.winfo_children():
            w.destroy()

        md = self.med_data[self.dispense_pill]
        accent = SLOT_DEFS[self.dispense_pill]["accent"]

        tk.Label(self.overlay_frame, text="DISPENSED",
                 font=self.font_label,
                 bg=self.theme["bg"],
                 fg=self.theme["muted"]).place(x=MARGIN_LEFT, y=80)

        tk.Label(self.overlay_frame, text=md["name"],
                 font=self.font_name,
                 bg=self.theme["bg"],
                 fg=accent).place(x=MARGIN_LEFT, y=120)

        tk.Label(self.overlay_frame, text="✓",
                 font=tkfont.Font(family="DejaVu Sans", size=72),
                 bg=self.theme["bg"],
                 fg="#4AD97A").place(x=SCREEN_W // 2 - 40, y=200)

        remaining = md["count"]
        if remaining > 0:
            text = f"{remaining} pills remaining"
        else:
            text = "Slot empty — scan QR to reload"
        tk.Label(self.overlay_frame, text=text,
                 font=self.font_body,
                 bg=self.theme["bg"],
                 fg=self.theme["muted"]).place(x=MARGIN_LEFT, y=380)

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
    #  MPR121 TOUCH POLLING
    # ══════════════════════════════════════════════════════════════════════
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
        """Scan for QR codes continuously. Handles multiple QR codes
        in a single frame and assigns slot positions by x-coordinate:
        rightmost QR = top slot (blue), leftmost = bottom slot (yellow)."""
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
        """Extract medication name from QR text."""
        try:
            payload = json.loads(raw_text)
            return payload.get("med", "Medication")
        except (json.JSONDecodeError, AttributeError):
            return raw_text.strip().capitalize() or "Medication"

    def _handle_qr_batch(self, slot_assignments):
        """Process all QR codes from a single frame.
        slot_assignments: list of (slot_key, raw_text) sorted by
        position — rightmost QR first (= top storage slot)."""
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

    # ── WiFi icon ──────────────────────────────────────────────────────────
    def _draw_wifi(self, canvas, color):
        cx, cy = 19, 26
        for r in (16, 11, 6):
            canvas.create_arc(cx - r, cy - r, cx + r, cy + r,
                              start=55, extent=70, style="arc",
                              outline=color, width=3)
        canvas.create_oval(cx - 2, cy - 2, cx + 2, cy + 2,
                           fill=color, outline=color)

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


# ═══════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════
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
