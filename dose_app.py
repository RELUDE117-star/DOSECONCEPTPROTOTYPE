#!/usr/bin/env python3
"""
DOSE Home Station — Prototype Application
v6.0 — PIL-rendered UI with User adherence screen, demo QR flow

Modes: Home, Storage, Settings, User (adherence tracking)
New QR ("demo" slot) triggers Add Med popup; old 4 QRs are instant.
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

PIL_AVAILABLE = False
try:
    from PIL import Image, ImageTk, ImageDraw, ImageFont
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
ADHERENCE_PATH = os.path.expanduser("~/dose-home-station/adherence_log.json")
APP_DIR = os.path.expanduser("~/dose-home-station")
APP_FILE = os.path.join(APP_DIR, "dose_app.py")
RAW_URL = ("https://raw.githubusercontent.com/relude117-star/"
           "doseconceptprototype/claude/quirky-brown-vkHwi")

SLOT_KEYS = ["blue", "red", "green", "yellow"]
SLOT_COLORS = {
    "blue": "#5FA3F5", "red": "#FF6B6B",
    "green": "#30D158", "yellow": "#FFD60A",
    "demo": "#C084FC",
}
SLOT_PADS = {"blue": 0, "red": 1, "green": 2, "yellow": 3}

DARK_THEME = {
    "bg": "#0B0B0D", "fg": "#F5F5F7", "muted": "#8E8E93",
    "card_bg": "#1C1C1E", "elevated_bg": "#26262A",
    "rail_bg": "#000000", "divider": "#3A3A3C",
    "rail_btn_bg": "#1E1E20", "rail_icon_color": "#FFFFFF",
    "btn_bg": "#1E1E24", "btn_active": "#2A2A32",
}

LIGHT_THEME = {
    "bg": "#EDEBE7", "fg": "#1C1C1E", "muted": "#8A8A8E",
    "card_bg": "#FAF9F6", "elevated_bg": "#EFEDE9",
    "rail_bg": "#F3F1ED", "divider": "#C8C8CA",
    "rail_btn_bg": "#E2DFD9", "rail_icon_color": "#2A2A2E",
    "btn_bg": "#DCDCDA", "btn_active": "#D0D0CE",
}

ACCENT_BLUE = "#5FA3F5"
SETTINGS_ICON_COLORS = ["#5FA3F5", "#FF9F43", "#30D158", "#AF52DE"]

KNOWN_QR_PAYLOADS = {
    '{"med":"Sertraline","slot":"blue"}': ("Sertraline", "blue"),
    '{"med":"Lisinopril","slot":"red"}': ("Lisinopril", "red"),
    '{"med":"Metformin","slot":"green"}': ("Metformin", "green"),
    '{"med":"Atorvastatin","slot":"yellow"}': ("Atorvastatin", "yellow"),
}

KEYBOARD_ROWS = [
    list("QWERTYUIOP"),
    list("ASDFGHJKL"),
    ["SHIFT"] + list("ZXCVBNM") + ["DEL"],
    ["SPACE"],
]


# ── PIL Drawing Helpers ───────────────────────────────────────────────────

def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def _hex_to_rgba(h, a=255):
    return _hex_to_rgb(h) + (a,)


def _pil_rounded_rect(w, h, r, fill, outline=None, outline_w=0, scale=2):
    sw, sh, sr, so = w * scale, h * scale, r * scale, outline_w * scale
    img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fc = _hex_to_rgba(fill) if isinstance(fill, str) else fill
    d.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=sr, fill=fc)
    if outline and outline_w > 0:
        oc = _hex_to_rgba(outline) if isinstance(outline, str) else outline
        d.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=sr,
                            fill=None, outline=oc, width=so)
    resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
    return img.resize((w, h), resample)


def _pil_circle(size, fill, scale=2):
    ss = size * scale
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fc = _hex_to_rgba(fill) if isinstance(fill, str) else fill
    d.ellipse([0, 0, ss - 1, ss - 1], fill=fc)
    resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
    return img.resize((size, size), resample)


def _pil_toggle(w, h, is_on, theme, scale=2):
    sw, sh = w * scale, h * scale
    sr = (h // 2) * scale
    img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = _hex_to_rgba(ACCENT_BLUE) if is_on else _hex_to_rgba(theme["elevated_bg"])
    d.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=sr, fill=bg)
    knob_r = int((h - 4) * scale / 2)
    cx = sw - knob_r - 2 * scale if is_on else knob_r + 2 * scale
    cy = sh // 2
    d.ellipse([cx - knob_r, cy - knob_r, cx + knob_r, cy + knob_r],
              fill=(255, 255, 255, 255))
    resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
    return img.resize((w, h), resample)


def _pil_ring(size, progress, accent, bg_color, inner_color, scale=2):
    ss = size * scale
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ring_w = 14 * scale
    bc = _hex_to_rgba(bg_color)
    d.ellipse([0, 0, ss - 1, ss - 1], fill=bc)
    if progress > 0:
        ac = _hex_to_rgba(accent)
        start_angle = -90
        sweep = progress * 360
        d.pieslice([0, 0, ss - 1, ss - 1], start_angle,
                   start_angle + sweep, fill=ac)
    ic = _hex_to_rgba(inner_color)
    inner_pad = ring_w
    d.ellipse([inner_pad, inner_pad, ss - 1 - inner_pad, ss - 1 - inner_pad],
              fill=ic)
    resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
    return img.resize((size, size), resample)


def _pil_checkmark(size, bg_color, scale=2):
    ss = size * scale
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, ss - 1, ss - 1], fill=_hex_to_rgba(bg_color))
    lw = max(3, 5 * scale)
    x1, y1 = int(ss * 0.28), int(ss * 0.50)
    x2, y2 = int(ss * 0.42), int(ss * 0.65)
    d.line([x1, y1, x2, y2], fill=(255, 255, 255, 255), width=lw)
    x3, y3 = int(ss * 0.72), int(ss * 0.35)
    d.line([x2, y2, x3, y3], fill=(255, 255, 255, 255), width=lw)
    resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
    return img.resize((size, size), resample)


def _pil_settings_icon(size, color, scale=2):
    ss = size * scale
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = _hex_to_rgba(color)
    d.rounded_rectangle([0, 0, ss - 1, ss - 1], radius=int(ss * 0.3), fill=bg)
    cx, cy = ss // 2, ss // 2
    rr = int(ss * 0.2)
    lw = max(2, int(ss * 0.06))
    d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
              outline=(255, 255, 255, 255), width=lw)
    resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
    return img.resize((size, size), resample)


def _pil_white_logo(path, size):
    """Load the DOSE logo and tint it white for the active state."""
    img = Image.open(path).convert("RGBA")
    resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
    img = img.resize((size, size), resample)
    r, g, b, a = img.split()
    white = Image.new("L", img.size, 255)
    return Image.merge("RGBA", (white, white, white, a))


def _pil_bar_chart(w, h, values, colors, bg_color, bar_color, scale=2):
    """Render a simple bar chart as a PIL image."""
    sw, sh = w * scale, h * scale
    img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=12 * scale,
                        fill=_hex_to_rgba(bg_color))
    if not values:
        resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
        return img.resize((w, h), resample)
    n = len(values)
    max_val = max(values) if max(values) > 0 else 1
    padding = 16 * scale
    bar_area_w = sw - 2 * padding
    bar_area_h = sh - 2 * padding
    gap = max(2 * scale, bar_area_w // (n * 4))
    bar_w = max(4 * scale, (bar_area_w - gap * (n + 1)) // n)
    for i, v in enumerate(values):
        bh = max(2 * scale, int(bar_area_h * v / max_val))
        bx = padding + gap + i * (bar_w + gap)
        by = padding + bar_area_h - bh
        c = colors[i % len(colors)] if isinstance(colors, list) else colors
        bc = _hex_to_rgba(c) if isinstance(c, str) else c
        d.rounded_rectangle([bx, by, bx + bar_w, padding + bar_area_h],
                            radius=max(2, 4 * scale), fill=bc)
    resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
    return img.resize((w, h), resample)


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


def _load_adherence_log():
    try:
        with open(ADHERENCE_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {"events": [], "missed": [], "late": []}


def _save_adherence_log(log):
    try:
        os.makedirs(os.path.dirname(ADHERENCE_PATH), exist_ok=True)
        with open(ADHERENCE_PATH, "w") as f:
            json.dump(log, f, indent=2)
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
        families = tkfont.families(self.root)
        def _pick(candidates):
            for c in candidates:
                if c in families:
                    return c
            return "DejaVu Sans"

        f_xb = _pick(["Inter ExtraBold", "Inter Black", "Inter", "DejaVu Sans"])
        f_b = _pick(["Inter", "DejaVu Sans"])
        f_sb = _pick(["Inter SemiBold", "Inter Medium", "Inter", "DejaVu Sans"])
        f_r = _pick(["Inter Light", "Inter", "DejaVu Sans"])

        self.font_clock = tkfont.Font(family=f_xb, size=20, weight="bold")
        self.font_date = tkfont.Font(family=f_r, size=13)
        self.font_name_lg = tkfont.Font(family=f_xb, size=28, weight="bold")
        self.font_name = tkfont.Font(family=f_b, size=22, weight="bold")
        self.font_body = tkfont.Font(family=f_r, size=16)
        self.font_body_bold = tkfont.Font(family=f_b, size=16, weight="bold")
        self.font_label = tkfont.Font(family=f_xb, size=11, weight="bold")
        self.font_small = tkfont.Font(family=f_r, size=13)
        self.font_small_bold = tkfont.Font(family=f_b, size=13, weight="bold")
        self.font_btn = tkfont.Font(family=f_xb, size=16, weight="bold")
        self.font_btn_lg = tkfont.Font(family=f_xb, size=17, weight="bold")
        self.font_title = tkfont.Font(family=f_b, size=18, weight="bold")
        self.font_medium = tkfont.Font(family=f_xb, size=14, weight="bold")
        self.font_count = tkfont.Font(family=f_xb, size=24, weight="bold")
        self.font_hold_big = tkfont.Font(family=f_xb, size=40, weight="bold")
        self.font_hold_label = tkfont.Font(family=f_r, size=12)
        self.font_settings = tkfont.Font(family=f_sb, size=17)
        self.font_rail = tkfont.Font(family=f_b, size=10, weight="bold")
        self.font_tiny = tkfont.Font(family=f_xb, size=9, weight="bold")
        self.font_dispense_btn = tkfont.Font(family=f_xb, size=15, weight="bold")
        self.font_day = tkfont.Font(family=f_b, size=12, weight="bold")
        self.font_dose_label = tkfont.Font(family=f_xb, size=11, weight="bold")
        self.font_dose_time = tkfont.Font(family=f_xb, size=16, weight="bold")
        self.font_update_btn = tkfont.Font(family=f_xb, size=13, weight="bold")
        self.font_pct = tkfont.Font(family=f_xb, size=48, weight="bold")
        self.font_pct_label = tkfont.Font(family=f_r, size=14)
        self.font_graph_label = tkfont.Font(family=f_r, size=10)
        self.font_kbd = tkfont.Font(family=f_b, size=14, weight="bold")
        self.font_kbd_special = tkfont.Font(family=f_b, size=11, weight="bold")

        # ── Image cache ────────────────────────────────────────────────────
        self._img_cache = {}

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
        self.qr_last_seen["demo"] = 0.0
        self._home_prev_keys = None
        self._storage_prev_visible = None
        self._anim_queue = []
        self._anim_running = False
        self._prev_qr_present = {k: False for k in SLOT_KEYS}
        self._prev_qr_present["demo"] = False
        self._prev_mode = "home"
        self._kbd_shift = False

        # Demo slot state — resets each launch
        self._demo_registered = False

        self._draft = {
            "name": "", "times_per_day": 1,
            "doses": [2], "qty": 30,
            "days": [1, 1, 1, 1, 1, 1, 1],
        }

        # Adherence log
        self.adherence = _load_adherence_log()

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

        # ── Root canvas ────────────────────────────────────────────────────
        self.root.configure(bg=self.theme["bg"])
        self.canvas = tk.Canvas(self.root, width=SCREEN_W, height=SCREEN_H,
                                bg=self.theme["bg"], highlightthickness=0, bd=0)
        self.canvas.place(x=0, y=0, width=SCREEN_W, height=SCREEN_H)

        # ── Bindings ───────────────────────────────────────────────────────
        self.root.bind("<Escape>", lambda e: self._quit())
        self.canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)

        self._click_zones = []

        # ── Draw + start ───────────────────────────────────────────────────
        self._draw_frame()
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

    def _get_tk_image(self, key, pil_img):
        tk_img = ImageTk.PhotoImage(pil_img)
        self._img_cache[key] = tk_img
        return tk_img

    # ── Med data management ────────────────────────────────────────────────
    def _init_med_data(self):
        saved = _load_med_data()
        for key in SLOT_KEYS:
            if key in saved:
                self.med_data[key] = saved[key]
            else:
                self.med_data[key] = {
                    "name": key.capitalize(), "count": 0, "loaded": False,
                    "take_with": "", "schedule_time": "8:00 AM",
                    "doses": [2], "times_per_day": 1,
                    "schedule_days": list(ALL_DAYS),
                }
        # Demo slot — always starts fresh
        self.med_data["demo"] = {
            "name": "Demo", "count": 0, "loaded": False,
            "take_with": "", "schedule_time": "8:00 AM",
            "doses": [2], "times_per_day": 1,
            "schedule_days": list(ALL_DAYS),
        }

    def _save_med(self):
        save_copy = {k: v for k, v in self.med_data.items() if k != "demo"}
        _save_med_data(save_copy)

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
                for k in self.settings:
                    if k in loaded.get("settings", {}):
                        self.settings[k] = loaded["settings"][k]
            except Exception:
                pass

    def _save_config(self):
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w") as f:
                json.dump({"settings": self.settings}, f, indent=2)
        except Exception:
            pass

    # ── Theme ──────────────────────────────────────────────────────────────
    def _apply_theme_colors(self):
        self.theme = dict(LIGHT_THEME if self.settings.get("night_mode") else DARK_THEME)

    def _apply_theme(self):
        self._apply_theme_colors()
        self._img_cache.clear()
        self.root.configure(bg=self.theme["bg"])
        self.canvas.configure(bg=self.theme["bg"])
        self._draw_frame()

    # ── QR Presence ────────────────────────────────────────────────────────
    def _is_qr_present(self, key):
        return (time.time() - self.qr_last_seen.get(key, 0)) < QR_PRESENCE_TIMEOUT

    # ══════════════════════════════════════════════════════════════════════
    #  MASTER DRAW
    # ══════════════════════════════════════════════════════════════════════
    def _draw_frame(self):
        c = self.canvas
        c.delete("all")
        self._click_zones.clear()
        self._img_cache.clear()

        c.create_rectangle(0, 0, SCREEN_W, SCREEN_H,
                           fill=self.theme["bg"], outline="")
        self._draw_rail(c)

        if self.mode == "home":
            self._draw_home(c)
        elif self.mode == "storage":
            self._draw_storage(c)
        elif self.mode == "settings":
            self._draw_settings(c)
        elif self.mode == "user":
            self._draw_user(c)
        elif self.mode == "addmed":
            self._draw_addmed(c)
        elif self.mode == "hold":
            self._draw_hold(c)
        elif self.mode == "dispensed":
            self._draw_dispensed(c)
        elif self.mode == "qtyconfirm":
            self._draw_qty_confirm(c)

    # ══════════════════════════════════════════════════════════════════════
    #  NAVIGATION RAIL — spread out, Home at bottom
    # ══════════════════════════════════════════════════════════════════════
    def _draw_rail(self, c):
        t = self.theme
        rx = CONTENT_W
        c.create_rectangle(rx, 0, SCREEN_W, SCREEN_H,
                           fill=t["rail_bg"], outline="")
        c.create_line(rx, 0, rx, SCREEN_H, fill=t["divider"])

        active = self.mode
        if active in ("hold", "dispensed", "qtyconfirm", "addmed"):
            active = self._prev_mode

        # Spread buttons: User at top, Storage, Settings spaced, Home at bottom
        # Content area is 32..448 (416px tall), Home aligns with bottom
        buttons = [
            ("User", "user", 24),
            ("Storage", "storage", 136),
            ("Settings", "settings", 248),
            ("Home", "home", 374),
        ]

        for label, mode_key, y in buttons:
            bx = rx + 28
            is_active = (active == mode_key)
            btn_bg = ACCENT_BLUE if is_active else t["rail_btn_bg"]

            btn_img = _pil_rounded_rect(72, 72, 20, btn_bg)
            tk_btn = self._get_tk_image(f"rail_{mode_key}", btn_img)
            c.create_image(bx, y, image=tk_btn, anchor="nw")

            self._draw_rail_icon(c, mode_key, bx, y, is_active)

            # All labels same color
            lx = bx + 36
            ly = y + 78
            c.create_text(lx, ly, text=label, font=self.font_rail,
                          fill=t["fg"], anchor="n")

            self._click_zones.append((bx, y, bx + 72, y + 90,
                                      lambda mk=mode_key: self._nav(mk)))

    def _draw_rail_icon(self, c, mode_key, bx, y, is_active):
        t = self.theme
        cx = bx + 36
        cy = y + 36
        icon_color = "#FFFFFF" if is_active else t["rail_icon_color"]

        if mode_key == "user":
            # Person icon
            c.create_oval(cx - 8, cy - 14, cx + 8, cy + 2,
                          outline=icon_color, width=2.5)
            c.create_arc(cx - 14, cy + 2, cx + 14, cy + 22,
                         start=0, extent=180,
                         outline=icon_color, width=2.5, style="arc")

        elif mode_key == "storage":
            # Straight pill capsule
            pw, ph = 28, 12
            px1, py1 = cx - pw // 2, cy - ph // 2
            r = ph // 2
            # Left half
            left_color = icon_color if not is_active else "#FFFFFF"
            c.create_arc(px1, py1, px1 + ph, py1 + ph,
                         start=90, extent=180,
                         fill=left_color, outline=left_color)
            c.create_rectangle(px1 + r, py1, cx, py1 + ph,
                               fill=left_color, outline=left_color)
            # Right half (accent colored, or white when active)
            right_color = ACCENT_BLUE if not is_active else "#FFFFFF"
            c.create_rectangle(cx, py1, px1 + pw - r, py1 + ph,
                               fill=right_color, outline=right_color)
            c.create_arc(px1 + pw - ph, py1, px1 + pw, py1 + ph,
                         start=270, extent=180,
                         fill=right_color, outline=right_color)

        elif mode_key == "settings":
            # Gear with 6 teeth
            r_outer = 16
            r_mid = 12
            r_inner = 5
            tooth_w = 6
            # Draw gear teeth as rectangles
            for angle in range(0, 360, 60):
                rad = math.radians(angle)
                rad_perp = math.radians(angle + 90)
                # Tooth center at r_mid
                tcx = cx + r_mid * math.cos(rad)
                tcy = cy + r_mid * math.sin(rad)
                hw = tooth_w / 2
                pts = []
                for dr, dp in [(-4, -hw), (-4, hw), (4, hw), (4, -hw)]:
                    px = tcx + dr * math.cos(rad) + dp * math.cos(rad_perp)
                    py = tcy + dr * math.sin(rad) + dp * math.sin(rad_perp)
                    pts.extend([px, py])
                c.create_polygon(pts, fill=icon_color, outline=icon_color)
            # Main body circle
            c.create_oval(cx - r_mid + 2, cy - r_mid + 2,
                          cx + r_mid - 2, cy + r_mid - 2,
                          fill=icon_color, outline=icon_color)
            # Inner hole
            hole_color = ACCENT_BLUE if is_active else t["rail_btn_bg"]
            c.create_oval(cx - r_inner, cy - r_inner,
                          cx + r_inner, cy + r_inner,
                          fill=hole_color, outline=hole_color)

        elif mode_key == "home":
            logo_loaded = False
            if PIL_AVAILABLE:
                logo_path = _find_logo()
                if logo_path:
                    try:
                        if is_active:
                            img = _pil_white_logo(logo_path, 48)
                        else:
                            resample = getattr(Image, 'LANCZOS',
                                               getattr(Image, 'ANTIALIAS', None))
                            img = Image.open(logo_path).convert("RGBA")
                            img = img.resize((48, 48), resample)
                        tk_img = self._get_tk_image("home_logo", img)
                        c.create_image(cx, cy, image=tk_img, anchor="center")
                        logo_loaded = True
                    except Exception:
                        pass
            if not logo_loaded:
                pill_img = _pil_rounded_rect(32, 32, 10, icon_color)
                tk_pill = self._get_tk_image("home_icon", pill_img)
                c.create_image(cx, cy, image=tk_pill, anchor="center")

    def _nav(self, mode_key):
        if self.dispense_state > 0:
            return
        self._prev_mode = self.mode
        self.mode = mode_key
        self._draw_frame()

    # ══════════════════════════════════════════════════════════════════════
    #  HOME SCREEN
    # ══════════════════════════════════════════════════════════════════════
    def _draw_home(self, c):
        t = self.theme

        now = datetime.now()
        try:
            clock_str = now.strftime("%-I:%M %p")
        except ValueError:
            clock_str = now.strftime("%I:%M %p").lstrip("0")
        months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        dows = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
        date_str = f"{dows[now.isoweekday() % 7]}, {months[now.month - 1]} {now.day}"

        c.create_text(32, 32, text=clock_str, font=self.font_clock,
                      fill=t["fg"], anchor="nw")
        c.create_text(200, 37, text=date_str, font=self.font_date,
                      fill=t["muted"], anchor="nw")

        c.create_text(32, 64, text="TODAY'S SCHEDULE", font=self.font_label,
                      fill=t["muted"], anchor="nw")

        sched = self._get_today_schedule()

        if not sched:
            c.create_text(32, 200, text="Place medications in view to see schedule",
                          font=self.font_body, fill=t["muted"], anchor="nw")
        else:
            card_gap = 12
            card_h = 76
            for i, entry in enumerate(sched[:4]):
                y = 92 + i * (card_h + card_gap)
                card_img = _pil_rounded_rect(608, card_h, 22, t["card_bg"])
                tk_card = self._get_tk_image(f"home_card_{i}", card_img)
                c.create_image(32, y, image=tk_card, anchor="nw")

                dot_img = _pil_rounded_rect(44, 44, 14, entry["accent"])
                tk_dot = self._get_tk_image(f"home_dot_{i}", dot_img)
                c.create_image(52, y + 16, image=tk_dot, anchor="nw")

                c.create_text(112, y + 16, text=entry["time"],
                              font=self.font_name, fill=t["fg"], anchor="nw")
                c.create_text(112, y + 44, text=entry["name"],
                              font=self.font_small, fill=t["muted"], anchor="nw")

                cnt = entry.get("count", 0)
                c.create_text(600, y + 28, text=f"{cnt} pills",
                              font=self.font_title, fill=entry["accent"],
                              anchor="e")

                self._click_zones.append(
                    (32, y, 640, y + card_h,
                     lambda k=entry["key"]: self._start_dispense_if_ok(k)))

        self._click_zones.append((0, 0, CONTENT_W, SCREEN_H, self._home_tap))

        hw = []
        if not CAMERA_AVAILABLE:
            hw.append("Camera not connected")
        if not self.has_touch:
            hw.append("Touch: " + (self.touch_error or "not detected"))
        if hw:
            c.create_text(32, SCREEN_H - 20, text="  ·  ".join(hw),
                          font=self.font_small, fill="#444444", anchor="sw")

    def _home_tap(self):
        for key in SLOT_KEYS + ["demo"]:
            if (self._is_loaded(key) and self._get_count(key) > 0
                    and self._is_qr_present(key)):
                self._start_dispense(key)
                return

    def _start_dispense_if_ok(self, key):
        if self._is_loaded(key) and self._get_count(key) > 0:
            self._start_dispense(key)

    def _get_today_schedule(self):
        now = datetime.now()
        today_name = now.strftime("%a")
        entries = []
        for key in SLOT_KEYS + ["demo"]:
            md = self.med_data.get(key)
            if not md or not md.get("loaded") or not self._is_qr_present(key):
                continue
            days = md.get("schedule_days", ALL_DAYS)
            if today_name not in days:
                continue
            dose_indices = md.get("doses", [2])
            for di, dose_idx in enumerate(dose_indices):
                time_str = TIME_PRESETS[dose_idx] if 0 <= dose_idx < len(TIME_PRESETS) else "8:00 AM"
                try:
                    t_obj = datetime.strptime(time_str, "%I:%M %p").replace(
                        year=now.year, month=now.month, day=now.day)
                except Exception:
                    t_obj = now
                try:
                    display = t_obj.strftime("%-I:%M %p")
                except ValueError:
                    display = t_obj.strftime("%I:%M %p").lstrip("0")
                accent = SLOT_COLORS.get(key, "#C084FC")
                entries.append({
                    "key": key, "dose_idx": di, "time": display,
                    "name": md["name"], "count": md.get("count", 0),
                    "accent": accent, "sort": t_obj,
                })
        entries.sort(key=lambda e: e["sort"])
        return entries

    # ══════════════════════════════════════════════════════════════════════
    #  STORAGE SCREEN
    # ══════════════════════════════════════════════════════════════════════
    def _draw_storage(self, c):
        t = self.theme

        left_img = _pil_rounded_rect(208, 416, 22, t["card_bg"])
        tk_left = self._get_tk_image("stor_left", left_img)
        c.create_image(32, 32, image=tk_left, anchor="nw")

        all_keys = SLOT_KEYS + (["demo"] if self._demo_registered else [])
        visible = [k for k in all_keys
                   if self._is_loaded(k) and self._is_qr_present(k)]

        for i, key in enumerate(all_keys):
            if key not in visible:
                continue
            md = self.med_data[key]
            accent = SLOT_COLORS.get(key, "#C084FC")
            y = 48 + visible.index(key) * 96
            is_sel = (key == self.selected_pill)

            if is_sel:
                row_img = _pil_rounded_rect(188, 88, 16, t["elevated_bg"],
                                            outline=accent, outline_w=2)
            else:
                row_img = _pil_rounded_rect(188, 88, 16, t["elevated_bg"])
            tk_row = self._get_tk_image(f"stor_row_{key}", row_img)
            c.create_image(42, y, image=tk_row, anchor="nw")

            dot_img = _pil_rounded_rect(14, 14, 5, accent)
            tk_dot = self._get_tk_image(f"stor_dot_{key}", dot_img)
            c.create_image(56, y + 10, image=tk_dot, anchor="nw")

            c.create_text(56, y + 30, text=md["name"],
                          font=self.font_small_bold, fill=t["fg"], anchor="nw")
            doses = md.get("doses", [2])
            if len(doses) > 1:
                dose_text = f"{len(doses)}x daily"
            else:
                idx = doses[0] if doses else 2
                dose_text = f"Next dose {TIME_PRESETS[idx] if 0 <= idx < len(TIME_PRESETS) else '8:00 AM'}"
            c.create_text(56, y + 54, text=dose_text,
                          font=self.font_tiny, fill=t["muted"], anchor="nw")

            self._click_zones.append(
                (42, y, 230, y + 88,
                 lambda k=key: self._storage_select(k)))

        right_img = _pil_rounded_rect(384, 416, 22, t["card_bg"])
        tk_right = self._get_tk_image("stor_right", right_img)
        c.create_image(256, 32, image=tk_right, anchor="nw")

        if visible and self.selected_pill in visible:
            self._draw_storage_detail(c, visible)
        elif visible:
            self.selected_pill = visible[0]
            self._draw_storage_detail(c, visible)
        else:
            c.create_text(448, 240, text="No pills in view",
                          font=self.font_name, fill=t["muted"], anchor="center")

    def _draw_storage_detail(self, c, visible):
        t = self.theme
        key = self.selected_pill
        md = self.med_data[key]
        accent = SLOT_COLORS.get(key, "#C084FC")
        px = 280

        c.create_text(px, 56, text=md["name"],
                      font=self.font_name_lg, fill=accent, anchor="nw")
        cnt = md.get("count", 0)
        c.create_text(px, 92, text=f"{cnt} pills remaining",
                      font=self.font_body, fill=t["muted"], anchor="nw")
        c.create_line(px, 120, 616, 120, fill=t["divider"])

        doses = md.get("doses", [2])
        c.create_text(px, 136, text=f"SCHEDULE ({len(doses)}x DAILY)",
                      font=self.font_label, fill=t["muted"], anchor="nw")

        chip_x = px
        for i, dose_idx in enumerate(doses):
            ts = TIME_PRESETS[dose_idx] if 0 <= dose_idx < len(TIME_PRESETS) else "8:00 AM"
            chip_w = 150
            chip_img = _pil_rounded_rect(chip_w, 60, 14, t["elevated_bg"])
            tk_chip = self._get_tk_image(f"dose_chip_{i}", chip_img)
            cy = 160
            c.create_image(chip_x, cy, image=tk_chip, anchor="nw")

            c.create_text(chip_x + chip_w // 2, cy + 12,
                          text=f"DOSE {i + 1}",
                          font=self.font_dose_label, fill=t["muted"],
                          anchor="center")
            c.create_text(chip_x + chip_w // 2, cy + 38,
                          text=ts, font=self.font_dose_time,
                          fill=t["fg"], anchor="center")

            # Prev arrow
            arr_y = cy + 38
            prev_img = _pil_rounded_rect(26, 26, 9, t["card_bg"])
            tk_prev = self._get_tk_image(f"dose_prev_{i}", prev_img)
            c.create_image(chip_x + 8, arr_y - 13, image=tk_prev, anchor="nw")
            c.create_text(chip_x + 21, arr_y, text="‹",
                          font=self.font_body_bold, fill=t["fg"],
                          anchor="center")
            self._click_zones.append(
                (chip_x + 8, arr_y - 13, chip_x + 34, arr_y + 13,
                 lambda idx=i: self._adj_dose_time(idx, -1)))

            # Next arrow
            nx = chip_x + chip_w - 34
            tk_next = self._get_tk_image(f"dose_next_{i}", prev_img)
            c.create_image(nx, arr_y - 13, image=tk_next, anchor="nw")
            c.create_text(nx + 13, arr_y, text="›",
                          font=self.font_body_bold, fill=t["fg"],
                          anchor="center")
            self._click_zones.append(
                (nx, arr_y - 13, nx + 26, arr_y + 13,
                 lambda idx=i: self._adj_dose_time(idx, 1)))

            chip_x += chip_w + 10

        # DAYS
        c.create_text(px, 240, text="DAYS",
                      font=self.font_label, fill=t["muted"], anchor="nw")

        sched_days = md.get("schedule_days", ALL_DAYS)
        dx = px
        for i, dl in enumerate(DAY_LABELS):
            day_name = ALL_DAYS[i]
            is_active = day_name in sched_days
            dbg = accent if is_active else t["elevated_bg"]
            dfg = "#0A0A0C" if is_active else t["muted"]

            day_img = _pil_rounded_rect(42, 32, 9, dbg)
            tk_day = self._get_tk_image(f"day_{key}_{i}", day_img)
            c.create_image(dx, 264, image=tk_day, anchor="nw")
            c.create_text(dx + 21, 280, text=dl,
                          font=self.font_day, fill=dfg, anchor="center")

            self._click_zones.append(
                (dx, 264, dx + 42, 296,
                 lambda d=day_name: self._toggle_day(d)))
            dx += 48

        # DISPENSE button
        disp_y = 316
        disp_w = 336
        disp_h = 48
        disp_bg = accent if cnt > 0 else t["btn_bg"]
        disp_img = _pil_rounded_rect(disp_w, disp_h, 14, disp_bg)
        tk_disp = self._get_tk_image("stor_dispense", disp_img)
        c.create_image(px, disp_y, image=tk_disp, anchor="nw")
        c.create_text(px + disp_w // 2, disp_y + disp_h // 2,
                      text="DISPENSE", font=self.font_dispense_btn,
                      fill="#0A0A0C" if cnt > 0 else t["muted"],
                      anchor="center")

        if cnt > 0:
            self._click_zones.append(
                (px, disp_y, px + disp_w, disp_y + disp_h,
                 lambda: self._start_dispense(self.selected_pill)))

    def _storage_select(self, key):
        self.selected_pill = key
        self._draw_frame()

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
        self._draw_frame()

    def _adj_dose_time(self, dose_idx, delta):
        key = self.selected_pill
        md = self.med_data[key]
        doses = md.get("doses", [2])
        if dose_idx < len(doses):
            n = len(TIME_PRESETS)
            doses[dose_idx] = (doses[dose_idx] + delta + n) % n
            md["doses"] = doses
            if doses:
                md["schedule_time"] = TIME_PRESETS[doses[0]]
            self._save_med()
            self._draw_frame()

    # ══════════════════════════════════════════════════════════════════════
    #  SETTINGS SCREEN
    # ══════════════════════════════════════════════════════════════════════
    def _draw_settings(self, c):
        t = self.theme

        card_img = _pil_rounded_rect(608, 416, 22, t["card_bg"])
        tk_card = self._get_tk_image("settings_card", card_img)
        c.create_image(32, 32, image=tk_card, anchor="nw")

        items = [
            ("Day / Night Mode", 0, "toggle", "night_mode",
             self.settings.get("night_mode", False)),
            ("Alarm Sound", 1, "toggle", "alarm_sound",
             self.settings.get("alarm_sound", True)),
            ("Check for Updates", 2, "button", "update", None),
            ("Constant QR Scan", 3, "toggle", "constant_scan",
             self.settings.get("constant_scan", False)),
        ]

        row_h = 104
        for label, idx, kind, key, val in items:
            y = 32 + idx * row_h
            px = 56

            if idx < 3:
                c.create_line(px, y + row_h, 616, y + row_h,
                              fill=t["divider"])

            icon_img = _pil_settings_icon(40, SETTINGS_ICON_COLORS[idx])
            tk_icon = self._get_tk_image(f"set_icon_{idx}", icon_img)
            c.create_image(px, y + (row_h - 40) // 2, image=tk_icon, anchor="nw")

            c.create_text(px + 56, y + row_h // 2 - 4, text=label,
                          font=self.font_settings, fill=t["fg"], anchor="w")

            if kind == "toggle":
                tw, th = 58, 30
                tx = 570
                ty = y + (row_h - th) // 2
                tog_img = _pil_toggle(tw, th, val, t)
                tk_tog = self._get_tk_image(f"toggle_{key}", tog_img)
                c.create_image(tx, ty, image=tk_tog, anchor="nw")

                self._click_zones.append(
                    (tx, ty, tx + tw, ty + th,
                     lambda k=key: self._toggle_setting(k)))

            elif kind == "button":
                bw, bh = 104, 40
                bx = 520
                by = y + (row_h - bh) // 2
                btn_img = _pil_rounded_rect(bw, bh, 12, ACCENT_BLUE)
                tk_btn = self._get_tk_image("update_btn", btn_img)
                c.create_image(bx, by, image=tk_btn, anchor="nw")
                c.create_text(bx + bw // 2, by + bh // 2, text="UPDATE",
                              font=self.font_update_btn, fill="#FFFFFF",
                              anchor="center")

                status = getattr(self, '_update_status_text', '')
                if status:
                    c.create_text(px + 56, y + row_h // 2 + 14, text=status,
                                  font=self.font_small, fill=t["muted"],
                                  anchor="w")

                self._click_zones.append(
                    (bx, by, bx + bw, by + bh,
                     self._on_update_pressed))

    def _toggle_setting(self, key):
        if key == "night_mode":
            self.settings["night_mode"] = not self.settings.get("night_mode", False)
            self._save_config()
            self._apply_theme()
        elif key == "alarm_sound":
            self.settings["alarm_sound"] = not self.settings.get("alarm_sound", True)
            self._save_config()
            self._draw_frame()
        elif key == "constant_scan":
            self.settings["constant_scan"] = not self.settings.get("constant_scan", False)
            self._save_config()
            self._draw_frame()

    # ══════════════════════════════════════════════════════════════════════
    #  USER / ADHERENCE SCREEN
    # ══════════════════════════════════════════════════════════════════════
    def _draw_user(self, c):
        t = self.theme

        # Main card
        card_img = _pil_rounded_rect(608, 416, 22, t["card_bg"])
        tk_card = self._get_tk_image("user_card", card_img)
        c.create_image(32, 32, image=tk_card, anchor="nw")

        c.create_text(56, 52, text="YOUR ADHERENCE",
                      font=self.font_label, fill=t["muted"], anchor="nw")

        # Calculate adherence percentage
        log = self.adherence
        total_events = len(log.get("events", []))
        missed_count = len(log.get("missed", []))
        late_count = len(log.get("late", []))
        on_time = max(0, total_events - missed_count - late_count)

        if total_events > 0:
            pct = int((on_time / total_events) * 100)
        else:
            pct = 100

        # Big percentage circle
        circle_size = 140
        circle_x = 100
        circle_y = 86

        pct_color = "#30D158" if pct >= 80 else ("#FFD60A" if pct >= 50 else "#FF6B6B")
        progress = pct / 100.0
        ring_img = _pil_ring(circle_size, progress, pct_color,
                             t["elevated_bg"], t["card_bg"])
        tk_ring = self._get_tk_image("user_ring", ring_img)
        c.create_image(circle_x, circle_y, image=tk_ring, anchor="nw")

        c.create_text(circle_x + circle_size // 2,
                      circle_y + circle_size // 2 - 12,
                      text=f"{pct}%", font=self.font_pct,
                      fill=pct_color, anchor="center")
        c.create_text(circle_x + circle_size // 2,
                      circle_y + circle_size // 2 + 22,
                      text="on time", font=self.font_pct_label,
                      fill=t["muted"], anchor="center")

        # Stats column to the right
        stats_x = 290
        stat_items = [
            ("Total Doses", str(total_events), t["fg"]),
            ("On Time", str(on_time), "#30D158"),
            ("Late", str(late_count), "#FFD60A"),
            ("Missed", str(missed_count), "#FF6B6B"),
        ]

        for i, (label, val, color) in enumerate(stat_items):
            sy = 90 + i * 50
            stat_bg = _pil_rounded_rect(300, 42, 12, t["elevated_bg"])
            tk_stat = self._get_tk_image(f"user_stat_{i}", stat_bg)
            c.create_image(stats_x, sy, image=tk_stat, anchor="nw")

            c.create_text(stats_x + 14, sy + 21, text=label,
                          font=self.font_small, fill=t["muted"], anchor="w")
            c.create_text(stats_x + 286, sy + 21, text=val,
                          font=self.font_body_bold, fill=color, anchor="e")

        # Weekly bar chart
        c.create_text(56, 310, text="LAST 7 DAYS",
                      font=self.font_label, fill=t["muted"], anchor="nw")

        # Build daily data from adherence log
        day_labels_short = ["M", "T", "W", "T", "F", "S", "S"]
        daily_vals = self._get_weekly_adherence()
        bar_colors = [("#30D158" if v >= 80 else
                       "#FFD60A" if v >= 50 else
                       "#FF6B6B") for v in daily_vals]

        chart_img = _pil_bar_chart(540, 100, daily_vals, bar_colors,
                                    t["elevated_bg"], ACCENT_BLUE)
        tk_chart = self._get_tk_image("user_chart", chart_img)
        c.create_image(56, 332, image=tk_chart, anchor="nw")

        # Day labels under chart
        chart_w = 540
        bar_gap = chart_w // 7
        for i, dl in enumerate(day_labels_short):
            lx = 56 + bar_gap // 2 + i * bar_gap
            c.create_text(lx, 438, text=dl, font=self.font_graph_label,
                          fill=t["muted"], anchor="center")

    def _get_weekly_adherence(self):
        """Return 7 values (0-100) for Mon-Sun adherence."""
        log = self.adherence
        events = log.get("events", [])
        missed = log.get("missed", [])
        late = log.get("late", [])

        now = datetime.now()
        daily = [0] * 7
        daily_total = [0] * 7

        for ev in events:
            try:
                dt = datetime.fromisoformat(ev.get("time", ""))
                diff = (now - dt).days
                if 0 <= diff < 7:
                    dow = dt.weekday()
                    daily_total[dow] += 1
                    missed_this = any(
                        m.get("time") == ev.get("time") and m.get("key") == ev.get("key")
                        for m in missed
                    )
                    if not missed_this:
                        daily[dow] += 1
            except Exception:
                continue

        result = []
        for i in range(7):
            if daily_total[i] > 0:
                result.append(int(100 * daily[i] / daily_total[i]))
            else:
                result.append(0)
        return result

    # ══════════════════════════════════════════════════════════════════════
    #  ADD MEDICATION SCREEN (triggered by new/demo QR)
    # ══════════════════════════════════════════════════════════════════════
    def _draw_addmed(self, c):
        t = self.theme
        draft = self._draft

        slot_color = SLOT_COLORS.get(self._draft_slot, "#C084FC")

        card_img = _pil_rounded_rect(608, 416, 22, t["card_bg"])
        tk_card = self._get_tk_image("addmed_card", card_img)
        c.create_image(32, 32, image=tk_card, anchor="nw")

        pad = 56

        c.create_text(pad, 50, text="NEW MEDICATION DETECTED",
                      font=self.font_label, fill=t["muted"], anchor="nw")

        # Name input field
        inp_img = _pil_rounded_rect(500, 40, 12, t["elevated_bg"])
        tk_inp = self._get_tk_image("addmed_input", inp_img)
        c.create_image(pad, 72, image=tk_inp, anchor="nw")

        name_text = draft["name"] or "Tap to type medication name"
        name_color = t["fg"] if draft["name"] else t["muted"]
        c.create_text(pad + 14, 92, text=name_text,
                      font=self.font_title, fill=name_color, anchor="w")

        dot_img = _pil_rounded_rect(28, 28, 8, slot_color)
        tk_dot = self._get_tk_image("addmed_dot", dot_img)
        c.create_image(572, 78, image=tk_dot, anchor="nw")

        self._click_zones.append(
            (pad, 72, pad + 500, 112,
             lambda: self._show_keyboard()))

        # QUANTITY section
        qy = 124
        q_img = _pil_rounded_rect(270, 80, 16, t["elevated_bg"])
        tk_q = self._get_tk_image("addmed_qty", q_img)
        c.create_image(pad, qy, image=tk_q, anchor="nw")

        c.create_text(pad + 135, qy + 12, text="QUANTITY",
                      font=self.font_tiny, fill=t["muted"], anchor="center")
        c.create_text(pad + 135, qy + 48, text=str(draft["qty"]),
                      font=self.font_count, fill=t["fg"], anchor="center")

        mb_img = _pil_rounded_rect(40, 40, 12, t["card_bg"])
        tk_mb = self._get_tk_image("addmed_qminus", mb_img)
        c.create_image(pad + 30, qy + 28, image=tk_mb, anchor="nw")
        c.create_text(pad + 50, qy + 48, text="−",
                      font=self.font_body_bold, fill=t["fg"], anchor="center")
        self._click_zones.append(
            (pad + 30, qy + 28, pad + 70, qy + 68, self._dec_draft_qty))

        pb_img = _pil_rounded_rect(40, 40, 12, t["card_bg"])
        tk_pb = self._get_tk_image("addmed_qplus", pb_img)
        c.create_image(pad + 200, qy + 28, image=tk_pb, anchor="nw")
        c.create_text(pad + 220, qy + 48, text="+",
                      font=self.font_body_bold, fill=t["fg"], anchor="center")
        self._click_zones.append(
            (pad + 200, qy + 28, pad + 240, qy + 68, self._inc_draft_qty))

        # TIMES PER DAY section
        tpd_img = _pil_rounded_rect(270, 80, 16, t["elevated_bg"])
        tk_tpd = self._get_tk_image("addmed_tpd", tpd_img)
        c.create_image(pad + 290, qy, image=tk_tpd, anchor="nw")

        c.create_text(pad + 425, qy + 12, text="TIMES PER DAY",
                      font=self.font_tiny, fill=t["muted"], anchor="center")
        c.create_text(pad + 425, qy + 48, text=str(draft["times_per_day"]),
                      font=self.font_count, fill=t["fg"], anchor="center")

        c.create_image(pad + 320, qy + 28, image=tk_mb, anchor="nw")
        c.create_text(pad + 340, qy + 48, text="−",
                      font=self.font_body_bold, fill=t["fg"], anchor="center")
        self._click_zones.append(
            (pad + 320, qy + 28, pad + 360, qy + 68, self._dec_draft_doses))

        c.create_image(pad + 490, qy + 28, image=tk_pb, anchor="nw")
        c.create_text(pad + 510, qy + 48, text="+",
                      font=self.font_body_bold, fill=t["fg"], anchor="center")
        self._click_zones.append(
            (pad + 490, qy + 28, pad + 530, qy + 68, self._inc_draft_doses))

        # DOSE TIMES
        c.create_text(pad, 216, text="DOSE TIMES",
                      font=self.font_tiny, fill=t["muted"], anchor="nw")

        chip_x = pad
        for i, dose_idx in enumerate(draft["doses"]):
            ts = TIME_PRESETS[dose_idx] if 0 <= dose_idx < len(TIME_PRESETS) else "8:00 AM"
            cw = min(130, 560 // max(1, len(draft["doses"])))
            chip_img = _pil_rounded_rect(cw, 48, 12, t["elevated_bg"])
            tk_chip = self._get_tk_image(f"addmed_dose_{i}", chip_img)
            c.create_image(chip_x, 234, image=tk_chip, anchor="nw")

            c.create_text(chip_x + cw // 2, 244, text=f"DOSE {i + 1}",
                          font=self.font_dose_label, fill=t["muted"],
                          anchor="center")
            c.create_text(chip_x + cw // 2, 264, text=ts,
                          font=self.font_dose_time, fill=t["fg"],
                          anchor="center")

            self._click_zones.append(
                (chip_x, 254, chip_x + cw // 2, 282,
                 lambda idx=i: self._adj_draft_dose(idx, -1)))
            self._click_zones.append(
                (chip_x + cw // 2, 254, chip_x + cw, 282,
                 lambda idx=i: self._adj_draft_dose(idx, 1)))

            chip_x += cw + 8

        # DAYS
        c.create_text(pad, 294, text="DAYS",
                      font=self.font_tiny, fill=t["muted"], anchor="nw")

        dx = pad
        for i, dl in enumerate(DAY_LABELS):
            active = draft["days"][i]
            dbg = slot_color if active else t["elevated_bg"]
            dfg = "#0A0A0C" if active else t["muted"]
            day_img = _pil_rounded_rect(70, 36, 10, dbg)
            tk_day = self._get_tk_image(f"addmed_day_{i}", day_img)
            c.create_image(dx, 312, image=tk_day, anchor="nw")
            c.create_text(dx + 35, 330, text=dl,
                          font=self.font_day, fill=dfg, anchor="center")
            self._click_zones.append(
                (dx, 312, dx + 70, 348,
                 lambda idx=i: self._toggle_draft_day(idx)))
            dx += 76

        # ADD MEDICATION button
        btn_y = 362
        btn_img = _pil_rounded_rect(560, 48, 16, ACCENT_BLUE)
        tk_btn = self._get_tk_image("addmed_submit", btn_img)
        c.create_image(pad, btn_y, image=tk_btn, anchor="nw")
        c.create_text(pad + 280, btn_y + 24, text="ADD MEDICATION",
                      font=self.font_btn_lg, fill="#FFFFFF", anchor="center")
        self._click_zones.append(
            (pad, btn_y, pad + 560, btn_y + 48, self._submit_add_med))

        # Cancel button
        cancel_y = btn_y + 2
        c.create_text(pad + 280, btn_y + 58, text="Cancel",
                      font=self.font_small, fill=t["muted"], anchor="center")
        self._click_zones.append(
            (pad + 200, btn_y + 48, pad + 360, btn_y + 72,
             lambda: self._nav("home")))

    # ── On-screen keyboard ─────────────────────────────────────────────────
    def _show_keyboard(self):
        if hasattr(self, '_kbd_overlay') and self._kbd_overlay:
            return
        self._kbd_text = self._draft["name"]
        self._kbd_shift = True
        self._draw_keyboard()

    def _draw_keyboard(self):
        if hasattr(self, '_kbd_overlay') and self._kbd_overlay:
            self._kbd_overlay.destroy()
            self._kbd_overlay = None

        t = self.theme
        kbd_h = 280
        self._kbd_overlay = tk.Canvas(self.root, width=CONTENT_W,
                                       height=kbd_h,
                                       highlightthickness=0,
                                       bg=t["bg"])
        self._kbd_overlay.place(x=0, y=SCREEN_H - kbd_h,
                                width=CONTENT_W, height=kbd_h)
        self._raise_widget(self._kbd_overlay)
        oc = self._kbd_overlay

        # Text display
        disp_img = _pil_rounded_rect(CONTENT_W - 32, 44, 12, t["elevated_bg"])
        tk_disp = self._get_tk_image("kbd_disp", disp_img)
        oc.create_image(16, 8, image=tk_disp, anchor="nw")
        display_text = self._kbd_text or "Type medication name..."
        display_color = t["fg"] if self._kbd_text else t["muted"]
        oc.create_text(30, 30, text=display_text,
                       font=self.font_title, fill=display_color, anchor="w")

        # Done button
        done_img = _pil_rounded_rect(80, 36, 10, ACCENT_BLUE)
        tk_done = self._get_tk_image("kbd_done", done_img)
        oc.create_image(CONTENT_W - 96, 12, image=tk_done, anchor="nw")
        oc.create_text(CONTENT_W - 56, 30, text="Done",
                       font=self.font_small_bold, fill="#FFFFFF", anchor="center")

        # Key rows
        key_h = 44
        key_gap = 4
        start_y = 60

        for row_idx, row in enumerate(KEYBOARD_ROWS):
            row_y = start_y + row_idx * (key_h + key_gap)
            if len(row) == 1 and row[0] == "SPACE":
                # Space bar
                sw = CONTENT_W - 140
                sx = 70
                key_img = _pil_rounded_rect(sw, key_h, 10, t["elevated_bg"])
                tk_key = self._get_tk_image("kbd_space", key_img)
                oc.create_image(sx, row_y, image=tk_key, anchor="nw")
                oc.create_text(sx + sw // 2, row_y + key_h // 2,
                               text="SPACE", font=self.font_kbd,
                               fill=t["muted"], anchor="center")
                continue

            total_keys = len(row)
            key_w = max(36, (CONTENT_W - 32 - key_gap * (total_keys - 1)) // total_keys)
            row_w = total_keys * key_w + (total_keys - 1) * key_gap
            kx = (CONTENT_W - row_w) // 2

            for ki, key_label in enumerate(row):
                is_special = key_label in ("SHIFT", "DEL")
                kw = key_w + 10 if is_special else key_w
                bg = t["btn_bg"] if is_special else t["elevated_bg"]
                key_img = _pil_rounded_rect(kw, key_h, 10, bg)
                tk_key = self._get_tk_image(f"kbd_{row_idx}_{ki}", key_img)
                oc.create_image(kx, row_y, image=tk_key, anchor="nw")

                display = key_label
                if not is_special:
                    display = key_label if self._kbd_shift else key_label.lower()
                font = self.font_kbd_special if is_special else self.font_kbd
                oc.create_text(kx + kw // 2, row_y + key_h // 2,
                               text=display, font=font,
                               fill=t["fg"], anchor="center")

                kx += kw + key_gap

        # Bind clicks on keyboard overlay
        oc.bind("<Button-1>", self._on_kbd_click)

    def _on_kbd_click(self, event):
        x, y = event.x, event.y
        t = self.theme

        # Check Done button
        if CONTENT_W - 96 <= x <= CONTENT_W - 16 and 12 <= y <= 48:
            self._draft["name"] = self._kbd_text
            self._kbd_overlay.destroy()
            self._kbd_overlay = None
            self._draw_frame()
            return

        key_h = 44
        key_gap = 4
        start_y = 60

        for row_idx, row in enumerate(KEYBOARD_ROWS):
            row_y = start_y + row_idx * (key_h + key_gap)
            if not (row_y <= y <= row_y + key_h):
                continue

            if len(row) == 1 and row[0] == "SPACE":
                sw = CONTENT_W - 140
                sx = 70
                if sx <= x <= sx + sw:
                    self._kbd_text += " "
                    self._draw_keyboard()
                return

            total_keys = len(row)
            key_w = max(36, (CONTENT_W - 32 - key_gap * (total_keys - 1)) // total_keys)
            row_w = total_keys * key_w + (total_keys - 1) * key_gap
            kx = (CONTENT_W - row_w) // 2

            for ki, key_label in enumerate(row):
                is_special = key_label in ("SHIFT", "DEL")
                kw = key_w + 10 if is_special else key_w
                if kx <= x <= kx + kw:
                    if key_label == "SHIFT":
                        self._kbd_shift = not self._kbd_shift
                        self._draw_keyboard()
                    elif key_label == "DEL":
                        self._kbd_text = self._kbd_text[:-1]
                        self._draw_keyboard()
                    else:
                        ch = key_label if self._kbd_shift else key_label.lower()
                        self._kbd_text += ch
                        if self._kbd_shift:
                            self._kbd_shift = False
                        self._draw_keyboard()
                    return
                kx += kw + key_gap
            return

    def _inc_draft_qty(self):
        self._draft["qty"] = min(300, self._draft["qty"] + 1)
        self._draw_frame()

    def _dec_draft_qty(self):
        self._draft["qty"] = max(1, self._draft["qty"] - 1)
        self._draw_frame()

    def _inc_draft_doses(self):
        if self._draft["times_per_day"] >= 4:
            return
        self._draft["times_per_day"] += 1
        last = self._draft["doses"][-1] if self._draft["doses"] else 2
        self._draft["doses"].append(last)
        self._draw_frame()

    def _dec_draft_doses(self):
        if self._draft["times_per_day"] <= 1:
            return
        self._draft["times_per_day"] -= 1
        self._draft["doses"] = self._draft["doses"][:self._draft["times_per_day"]]
        self._draw_frame()

    def _adj_draft_dose(self, idx, delta):
        n = len(TIME_PRESETS)
        self._draft["doses"][idx] = (self._draft["doses"][idx] + delta + n) % n
        self._draw_frame()

    def _toggle_draft_day(self, idx):
        self._draft["days"][idx] = 0 if self._draft["days"][idx] else 1
        self._draw_frame()

    def _submit_add_med(self):
        name = self._draft["name"].strip() or "New Medication"
        slot = getattr(self, '_draft_slot', 'demo')
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
        if slot == "demo":
            self._demo_registered = True
        self._save_med()
        self._draft = {"name": "", "times_per_day": 1, "doses": [2],
                       "qty": 30, "days": [1, 1, 1, 1, 1, 1, 1]}
        self.selected_pill = slot
        self._nav("home")

    # ══════════════════════════════════════════════════════════════════════
    #  HOLD TO DISPENSE
    # ══════════════════════════════════════════════════════════════════════
    def _start_dispense(self, pill_key):
        md = self.med_data.get(pill_key, {})
        if not md.get("loaded") or md.get("count", 0) <= 0:
            return
        self.dispense_pill = pill_key
        self.dispense_state = 2
        self._prev_mode = self.mode
        self.mode = "hold"
        self.hold_start = 0
        self._draw_frame()

    def _draw_hold(self, c):
        t = self.theme
        md = self.med_data[self.dispense_pill]
        accent = SLOT_COLORS.get(self.dispense_pill, "#C084FC")
        cx = CONTENT_W // 2

        c.create_text(cx, 70, text="HOLD SCREEN TO DISPENSE",
                      font=self.font_label, fill=t["muted"], anchor="center")
        c.create_text(cx, 96, text=md["name"],
                      font=self.font_name_lg, fill=accent, anchor="center")

        cnt = md["count"]
        c.create_text(cx, 132, text=f"{cnt} pill{'s' if cnt != 1 else ''} remaining",
                      font=self.font_body, fill=t["muted"], anchor="center")

        elapsed = time.time() - self.hold_start if self.hold_start > 0 else 0
        progress = min(elapsed / HOLD_TIME, 1.0) if self.hold_start > 0 else 0

        ring_size = 200
        ring_img = _pil_ring(ring_size, progress, accent,
                             t["elevated_bg"], t["card_bg"])
        tk_ring = self._get_tk_image("hold_ring", ring_img)
        ring_x = cx - ring_size // 2
        ring_y = 168
        c.create_image(ring_x, ring_y, image=tk_ring, anchor="nw")

        remaining = max(0, HOLD_TIME - elapsed) if self.hold_start > 0 else HOLD_TIME
        c.create_text(cx, ring_y + ring_size // 2 - 10,
                      text=f"{remaining:.1f}",
                      font=self.font_hold_big, fill=t["fg"], anchor="center")
        c.create_text(cx, ring_y + ring_size // 2 + 22,
                      text="SECONDS", font=self.font_hold_label,
                      fill=t["muted"], anchor="center")

        cancel_w, cancel_h = 160, 52
        cancel_img = _pil_rounded_rect(cancel_w, cancel_h, 16, t["elevated_bg"])
        tk_cancel = self._get_tk_image("hold_cancel", cancel_img)
        cancel_x = cx - cancel_w // 2
        cancel_y = 388
        c.create_image(cancel_x, cancel_y, image=tk_cancel, anchor="nw")
        c.create_text(cx, cancel_y + cancel_h // 2, text="Cancel",
                      font=self.font_body_bold, fill=t["fg"], anchor="center")
        self._click_zones.append(
            (cancel_x, cancel_y, cancel_x + cancel_w, cancel_y + cancel_h,
             self._cancel_hold))

    def _cancel_hold(self):
        self.dispense_state = 0
        self.dispense_pill = None
        self.hold_start = 0
        if self.hold_after_id:
            self.root.after_cancel(self.hold_after_id)
            self.hold_after_id = None
        self.mode = self._prev_mode
        self._draw_frame()

    def _end_dispense(self):
        self.dispense_state = 0
        self.dispense_pill = None
        self.hold_start = 0
        if self.hold_after_id:
            self.root.after_cancel(self.hold_after_id)
            self.hold_after_id = None
        self.mode = self._prev_mode
        self._draw_frame()

    def _on_canvas_press(self, event):
        if self.mode == "hold" and self.dispense_state == 2:
            cx = CONTENT_W // 2
            cancel_x = cx - 80
            cancel_y = 388
            if cancel_x <= event.x <= cancel_x + 160 and cancel_y <= event.y <= cancel_y + 52:
                self._cancel_hold()
            else:
                self.hold_start = time.time()
                self._hold_update()
            return
        self._on_canvas_click(event)

    def _on_canvas_release(self, event):
        if self.mode == "hold" and self.dispense_state == 2:
            if self.hold_start > 0:
                elapsed = time.time() - self.hold_start
                if elapsed < HOLD_TIME:
                    self.hold_start = 0
                    if self.hold_after_id:
                        self.root.after_cancel(self.hold_after_id)
                        self.hold_after_id = None
                    self._draw_frame()

    def _hold_update(self):
        if self.mode != "hold" or self.dispense_state != 2 or self.hold_start == 0:
            return
        elapsed = time.time() - self.hold_start
        self._draw_frame()

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
        self._save_med()

        # Log adherence event
        now = datetime.now()
        self.adherence.setdefault("events", []).append({
            "key": key, "name": md["name"],
            "time": now.isoformat(),
        })
        sched_time_str = md.get("schedule_time", "8:00 AM")
        try:
            sched_dt = datetime.strptime(sched_time_str, "%I:%M %p").replace(
                year=now.year, month=now.month, day=now.day)
            diff_min = abs((now - sched_dt).total_seconds()) / 60
            if diff_min > 60:
                self.adherence.setdefault("late", []).append({
                    "key": key, "name": md["name"],
                    "time": now.isoformat(),
                })
        except Exception:
            pass
        _save_adherence_log(self.adherence)

        self.dispense_state = 4
        self._play_sound()
        self.mode = "dispensed"
        self._draw_frame()
        self.root.after(int(DISPENSED_TIME * 1000), self._end_dispense)

    def _draw_dispensed(self, c):
        t = self.theme
        md = self.med_data[self.dispense_pill]
        accent = SLOT_COLORS.get(self.dispense_pill, "#C084FC")
        cx = CONTENT_W // 2

        c.create_text(cx, 68, text="DISPENSED",
                      font=self.font_label, fill=t["muted"], anchor="center")
        c.create_text(cx, 92, text=md["name"],
                      font=self.font_name_lg, fill=accent, anchor="center")

        check_img = _pil_checkmark(96, ACCENT_BLUE)
        tk_check = self._get_tk_image("dispensed_check", check_img)
        c.create_image(cx - 48, 140, image=tk_check, anchor="nw")

        remaining = md["count"]
        text = (f"{remaining} pill{'s' if remaining != 1 else ''} remaining"
                if remaining > 0 else "Slot empty — scan QR to reload")
        c.create_text(cx, SCREEN_H - 80, text=text,
                      font=self.font_title, fill=t["muted"], anchor="center")

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
    #  QTY CONFIRM OVERLAY
    # ══════════════════════════════════════════════════════════════════════
    def _show_qty_confirm(self, slot_key, med_name):
        self._qty_slot = slot_key
        md = self.med_data[slot_key]
        self._qty_value = md["count"] if md.get("loaded") and md["count"] > 0 else DEFAULT_QTY
        self._qty_name = med_name
        self._prev_mode = self.mode
        self.mode = "qtyconfirm"
        self._draw_frame()

    def _draw_qty_confirm(self, c):
        t = self.theme
        accent = SLOT_COLORS.get(self._qty_slot, "#C084FC")
        cx = CONTENT_W // 2

        card_img = _pil_rounded_rect(560, 340, 22, t["card_bg"])
        tk_card = self._get_tk_image("qty_card", card_img)
        c.create_image(56, 70, image=tk_card, anchor="nw")

        c.create_text(80, 94, text="MEDICATION LOADED",
                      font=self.font_label, fill=t["muted"], anchor="nw")
        c.create_text(80, 118, text=self._qty_name,
                      font=self.font_name_lg, fill=accent, anchor="nw")
        c.create_text(80, 190, text="HOW MANY PILLS?",
                      font=self.font_label, fill=t["muted"], anchor="nw")
        c.create_text(80, 220, text=str(self._qty_value),
                      font=self.font_hold_big, fill=accent, anchor="nw")

        mb_img = _pil_rounded_rect(56, 56, 16, t["elevated_bg"])
        tk_mb = self._get_tk_image("qty_minus", mb_img)
        c.create_image(260, 218, image=tk_mb, anchor="nw")
        c.create_text(288, 246, text="−", font=self.font_count,
                      fill=t["fg"], anchor="center")
        self._click_zones.append(
            (260, 218, 316, 274, lambda: self._qty_adjust(-1)))

        pb_img = _pil_rounded_rect(56, 56, 16, t["elevated_bg"])
        tk_pb = self._get_tk_image("qty_plus", pb_img)
        c.create_image(340, 218, image=tk_pb, anchor="nw")
        c.create_text(368, 246, text="+", font=self.font_count,
                      fill=t["fg"], anchor="center")
        self._click_zones.append(
            (340, 218, 396, 274, lambda: self._qty_adjust(1)))

        btn_img = _pil_rounded_rect(496, 52, 16, ACCENT_BLUE)
        tk_btn = self._get_tk_image("qty_confirm", btn_img)
        c.create_image(80, 340, image=tk_btn, anchor="nw")
        c.create_text(328, 366, text="CONFIRM",
                      font=self.font_btn_lg, fill="#FFFFFF", anchor="center")
        self._click_zones.append(
            (80, 340, 576, 392, self._qty_commit))

    def _qty_adjust(self, delta):
        self._qty_value = max(1, self._qty_value + delta)
        self._draw_frame()

    def _qty_commit(self):
        key = self._qty_slot
        if key and key in self.med_data:
            self.med_data[key]["count"] = self._qty_value
            self.med_data[key]["loaded"] = True
            self._save_med()
        self.mode = self._prev_mode
        self._draw_frame()

    # ══════════════════════════════════════════════════════════════════════
    #  CLICK HANDLING
    # ══════════════════════════════════════════════════════════════════════
    def _on_canvas_click(self, event):
        x, y = event.x, event.y
        for (x1, y1, x2, y2, callback) in reversed(self._click_zones):
            if x1 <= x <= x2 and y1 <= y <= y2:
                callback()
                return

    # ══════════════════════════════════════════════════════════════════════
    #  CLOCK TICK
    # ══════════════════════════════════════════════════════════════════════
    def _tick_clock(self):
        self._check_presence_changes()
        if self.mode in ("home", "storage"):
            self._draw_frame()
        self.root.after(1000, self._tick_clock)

    def _check_presence_changes(self):
        if self.dispense_state > 0:
            return
        for key in SLOT_KEYS + ["demo"]:
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
        accent = SLOT_COLORS.get(slot_key, "#C084FC")
        md = self.med_data.get(slot_key, {})
        name = md.get("name", slot_key.capitalize())

        bw, bh = 120, 180
        cx = CONTENT_W // 2

        if direction == "up":
            start_y = (SCREEN_H - bh) // 2
            end_y = -bh - 20
            label_text = f"{name} removed"
        else:
            start_y = SCREEN_H + 20
            end_y = (SCREEN_H - bh) // 2
            label_text = f"{name} placed"

        bottle_img = _pil_rounded_rect(bw, bh, 16, accent)
        d = ImageDraw.Draw(bottle_img)
        cap_color = tuple(max(0, c - 40) for c in _hex_to_rgb(accent)) + (255,)
        d.rounded_rectangle([0, 0, bw - 1, 25], radius=16, fill=cap_color)

        self._bottle_tk = self._get_tk_image("bottle_anim", bottle_img)
        self._bottle_x = cx - bw // 2
        self._bottle_y = start_y
        self._bottle_end_y = end_y
        self._bottle_total_dist = end_y - start_y
        self._bottle_label = label_text

        self._anim_overlay = tk.Canvas(self.root, width=CONTENT_W,
                                       height=SCREEN_H,
                                       highlightthickness=0,
                                       bg=self.theme["bg"])
        self._anim_overlay.place(x=0, y=0, width=CONTENT_W, height=SCREEN_H)
        self._raise_widget(self._anim_overlay)

        self._anim_overlay.create_rectangle(0, 0, CONTENT_W, SCREEN_H,
                                            fill="#000000",
                                            stipple="gray25")
        self._anim_bottle_id = self._anim_overlay.create_image(
            self._bottle_x, start_y, image=self._bottle_tk, anchor="nw")
        self._anim_text_id = self._anim_overlay.create_text(
            cx, start_y + bh // 2 + 20, text=name,
            font=self.font_small_bold, fill="#0A0A0C")
        self._anim_status_id = self._anim_overlay.create_text(
            cx, SCREEN_H - 40, text=label_text,
            font=self.font_body_bold, fill="#FFFFFF")

        self._anim_frame_num = 0
        self._anim_total_frames = 18
        self._do_anim_step()

    def _do_anim_step(self):
        if self._anim_frame_num > self._anim_total_frames:
            self.root.after(300, self._anim_cleanup)
            return
        t = self._anim_frame_num / self._anim_total_frames
        ease = t * t * (3 - 2 * t)
        dy = self._bottle_total_dist * ease
        prev_t = max(0, (self._anim_frame_num - 1) / self._anim_total_frames)
        prev_ease = prev_t * prev_t * (3 - 2 * prev_t)
        prev_dy = self._bottle_total_dist * prev_ease
        delta = dy - prev_dy
        self._anim_overlay.move(self._anim_bottle_id, 0, delta)
        self._anim_overlay.move(self._anim_text_id, 0, delta)
        self._anim_frame_num += 1
        self.root.after(22, self._do_anim_step)

    def _anim_cleanup(self):
        if hasattr(self, '_anim_overlay') and self._anim_overlay:
            self._anim_overlay.destroy()
            self._anim_overlay = None
        self._run_next_anim()

    # ══════════════════════════════════════════════════════════════════════
    #  AUTO-UPDATE
    # ══════════════════════════════════════════════════════════════════════
    def _on_update_pressed(self):
        self._update_status_text = "Checking for updates..."
        self._draw_frame()
        threading.Thread(target=self._do_update_check, daemon=True).start()

    def _do_update_check(self, silent=False):
        try:
            url = RAW_URL + "/dose_app.py"
            resp = urlopen(url, timeout=15)
            remote_data = resp.read()
        except Exception:
            if not silent:
                self.root.after(0, self._update_result, "No internet — try later")
            return

        remote_hash = hashlib.md5(remote_data).hexdigest()
        local_hash = ""
        try:
            with open(os.path.abspath(__file__), "rb") as f:
                local_hash = hashlib.md5(f.read()).hexdigest()
        except Exception:
            pass

        if remote_hash == local_hash:
            if not silent:
                self.root.after(0, self._update_result, "Up to date")
            return
        self.root.after(0, self._apply_update, remote_data)

    def _update_result(self, msg):
        self._update_status_text = msg
        if self.mode == "settings":
            self._draw_frame()

    def _apply_update(self, remote_data):
        self._update_status_text = "Downloading..."
        if self.mode == "settings":
            self._draw_frame()

        def do_download():
            try:
                local_path = os.path.abspath(__file__)
                with open(local_path, "wb") as f:
                    f.write(remote_data)
                os.makedirs(APP_DIR, exist_ok=True)
                with open(os.path.join(APP_DIR, "dose_app.py"), "wb") as f:
                    f.write(remote_data)
                for fname in ["DOSE.sh", "dose_logo.png", "demo_qr.png"]:
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
                self.root.after(0, self._update_result, f"Update failed: {e}")

        threading.Thread(target=do_download, daemon=True).start()

    def _finish_update(self):
        self._update_status_text = "Updated! Restarting..."
        if self.mode == "settings":
            self._draw_frame()
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
                    self.root.after(0, self._handle_qr_results, qr_with_pos)
                time.sleep(0.5)
            except Exception:
                time.sleep(1)

    def _handle_qr_results(self, qr_with_pos):
        now = time.time()

        for x_pos, raw_text in qr_with_pos:
            raw_stripped = raw_text.strip()

            # Check if this is one of the 4 known QRs
            if raw_stripped in KNOWN_QR_PAYLOADS:
                med_name, slot = KNOWN_QR_PAYLOADS[raw_stripped]
                self.qr_last_seen[slot] = now
                md = self.med_data[slot]
                if not md.get("loaded"):
                    md["name"] = med_name
                    md["loaded"] = True
                    md["count"] = md.get("count", 0) or DEFAULT_QTY
                    self._save_med()
                continue

            # Unknown QR — check if it's the demo payload
            try:
                payload = json.loads(raw_stripped)
                qr_slot = payload.get("slot", "")
                qr_med = payload.get("med", "New Medication")
            except (json.JSONDecodeError, AttributeError):
                qr_med = raw_stripped.strip().capitalize() or "New Medication"
                qr_slot = "demo"

            if qr_slot == "demo" or qr_slot not in SLOT_KEYS:
                self.qr_last_seen["demo"] = now
                if not self._demo_registered:
                    # Show Add Med screen for new demo QR
                    self._draft_slot = "demo"
                    self._draft["name"] = qr_med if qr_med != "New Medication" else ""
                    if self.mode != "addmed" and self.dispense_state == 0:
                        self._prev_mode = self.mode
                        self.mode = "addmed"
                        self._draw_frame()
                else:
                    md = self.med_data["demo"]
                    if not md.get("loaded"):
                        md["name"] = qr_med
                        md["loaded"] = True
                        md["count"] = DEFAULT_QTY
                continue

        # Handle known QR slot assignments by position (rightmost = blue, etc.)
        known_seen = []
        for x_pos, raw_text in qr_with_pos:
            raw_stripped = raw_text.strip()
            if raw_stripped in KNOWN_QR_PAYLOADS:
                _, slot = KNOWN_QR_PAYLOADS[raw_stripped]
                known_seen.append(slot)

        # Re-scan known ones and handle empty slot refill
        for slot in known_seen:
            md = self.med_data[slot]
            if self.dispense_state == 0 and md.get("loaded") and md.get("count", 0) <= 0:
                self._show_qty_confirm(slot, md["name"])
                return

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
