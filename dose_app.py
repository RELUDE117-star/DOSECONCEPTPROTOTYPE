#!/usr/bin/env python3
"""
DOSE Home Station — Prototype Application
v5.0 — PIL-rendered UI with anti-aliased rounded rectangles

Every visible element is rendered as a PIL image with true rounded corners,
then displayed on a Tkinter Canvas. This gives the smooth, professional look
that native Tkinter widgets cannot achieve.

Modes: Home (default), Storage, Settings, Add Medication
Dispensing overlay: HOLD (circular ring) → DISPENSED

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
APP_DIR = os.path.expanduser("~/dose-home-station")
APP_FILE = os.path.join(APP_DIR, "dose_app.py")
RAW_URL = ("https://raw.githubusercontent.com/relude117-star/"
           "doseconceptprototype/claude/quirky-brown-vkHwi")

SLOT_KEYS = ["blue", "red", "green", "yellow"]
SLOT_COLORS = {
    "blue": "#5FA3F5", "red": "#FF6B6B",
    "green": "#30D158", "yellow": "#FFD60A",
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

# ── PIL Drawing Helpers ───────────────────────────────────────────────────

def _hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def _hex_to_rgba(h, a=255):
    return _hex_to_rgb(h) + (a,)


def _pil_rounded_rect(w, h, r, fill, outline=None, outline_w=0, scale=2):
    """Render an anti-aliased rounded rectangle as a PIL RGBA Image.
    Draws at `scale`x then downscales for smooth edges."""
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
    """Render an anti-aliased filled circle."""
    ss = size * scale
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fc = _hex_to_rgba(fill) if isinstance(fill, str) else fill
    d.ellipse([0, 0, ss - 1, ss - 1], fill=fc)
    resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
    return img.resize((size, size), resample)


def _pil_toggle(w, h, is_on, theme, scale=2):
    """Render a toggle switch as a PIL image."""
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
    """Render a circular progress ring."""
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
    # Cut inner circle
    ic = _hex_to_rgba(inner_color)
    inner_pad = ring_w
    d.ellipse([inner_pad, inner_pad, ss - 1 - inner_pad, ss - 1 - inner_pad],
              fill=ic)
    resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
    return img.resize((size, size), resample)


def _pil_checkmark(size, bg_color, scale=2):
    """Render a checkmark inside a circle."""
    ss = size * scale
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, ss - 1, ss - 1], fill=_hex_to_rgba(bg_color))
    # Draw checkmark lines
    lw = max(3, 5 * scale)
    # Short leg
    x1, y1 = int(ss * 0.28), int(ss * 0.50)
    x2, y2 = int(ss * 0.42), int(ss * 0.65)
    d.line([x1, y1, x2, y2], fill=(255, 255, 255, 255), width=lw)
    # Long leg
    x3, y3 = int(ss * 0.72), int(ss * 0.35)
    d.line([x2, y2, x3, y3], fill=(255, 255, 255, 255), width=lw)
    resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
    return img.resize((size, size), resample)


def _pil_settings_icon(size, color, scale=2):
    """Render a settings icon (circle with ring border)."""
    ss = size * scale
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = _hex_to_rgba(color)
    d.rounded_rectangle([0, 0, ss - 1, ss - 1], radius=int(ss * 0.3), fill=bg)
    # Inner ring
    cx, cy = ss // 2, ss // 2
    rr = int(ss * 0.2)
    lw = max(2, int(ss * 0.06))
    d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
              outline=(255, 255, 255, 255), width=lw)
    resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
    return img.resize((size, size), resample)


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
        self.font_dose_label = tkfont.Font(family=f_xb, size=10, weight="bold")
        self.font_dose_time = tkfont.Font(family=f_xb, size=13, weight="bold")
        self.font_update_btn = tkfont.Font(family=f_xb, size=13, weight="bold")

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
        self._home_prev_keys = None
        self._storage_prev_visible = None
        self._anim_queue = []
        self._anim_running = False
        self._prev_qr_present = {k: False for k in SLOT_KEYS}
        self._prev_mode = "home"

        self._draft = {
            "name": "", "times_per_day": 1,
            "doses": [2], "qty": 30,
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

        # ── Root canvas (full screen) ──────────────────────────────────────
        self.root.configure(bg=self.theme["bg"])

        # Main canvas for all content
        self.canvas = tk.Canvas(self.root, width=SCREEN_W, height=SCREEN_H,
                                bg=self.theme["bg"], highlightthickness=0, bd=0)
        self.canvas.place(x=0, y=0, width=SCREEN_W, height=SCREEN_H)

        # ── Bindings ───────────────────────────────────────────────────────
        self.root.bind("<Escape>", lambda e: self._quit())
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)

        # ── Click zones ────────────────────────────────────────────────────
        self._click_zones = []

        # ── Draw initial frame ─────────────────────────────────────────────
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

    # ── Image caching ──────────────────────────────────────────────────────
    def _get_tk_image(self, key, pil_img):
        """Cache a PIL image as a Tk PhotoImage to prevent garbage collection."""
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
    #  MASTER DRAW — renders the entire UI onto self.canvas
    # ══════════════════════════════════════════════════════════════════════
    def _draw_frame(self):
        """Redraw the entire screen on the canvas."""
        c = self.canvas
        c.delete("all")
        self._click_zones.clear()
        self._img_cache.clear()

        # Background
        c.create_rectangle(0, 0, SCREEN_W, SCREEN_H,
                           fill=self.theme["bg"], outline="")

        # Draw rail
        self._draw_rail(c)

        # Draw content based on mode
        if self.mode == "home":
            self._draw_home(c)
        elif self.mode == "storage":
            self._draw_storage(c)
        elif self.mode == "settings":
            self._draw_settings(c)
        elif self.mode == "addmed":
            self._draw_addmed(c)
        elif self.mode == "hold":
            self._draw_hold(c)
        elif self.mode == "dispensed":
            self._draw_dispensed(c)
        elif self.mode == "qtyconfirm":
            self._draw_qty_confirm(c)

    # ══════════════════════════════════════════════════════════════════════
    #  NAVIGATION RAIL (right 128px)
    # ══════════════════════════════════════════════════════════════════════
    def _draw_rail(self, c):
        t = self.theme
        rx = CONTENT_W
        # Rail background
        c.create_rectangle(rx, 0, SCREEN_W, SCREEN_H,
                           fill=t["rail_bg"], outline="")
        # Divider line
        c.create_line(rx, 0, rx, SCREEN_H, fill=t["divider"])

        active = self.mode
        if active in ("hold", "dispensed", "qtyconfirm"):
            active = self._prev_mode

        # Rail button definitions: (label, mode_key, y_offset)
        buttons = [
            ("Add Med", "addmed", 32),
            ("Storage", "storage", 130),
            ("Settings", "settings", 228),
            ("Home", "home", 326),
        ]

        for label, mode_key, y in buttons:
            bx = rx + 28
            is_active = (active == mode_key)
            btn_bg = ACCENT_BLUE if is_active else t["rail_btn_bg"]

            # Render button background
            btn_img = _pil_rounded_rect(72, 72, 20, btn_bg)
            tk_btn = self._get_tk_image(f"rail_{mode_key}", btn_img)
            c.create_image(bx, y, image=tk_btn, anchor="nw")

            # Draw icon on top
            self._draw_rail_icon(c, mode_key, bx, y, is_active)

            # Label below
            lx = bx + 36
            ly = y + 78
            lbl_color = t["fg"] if mode_key == "home" else t["muted"]
            c.create_text(lx, ly, text=label, font=self.font_rail,
                          fill=lbl_color, anchor="n")

            # Click zone
            self._click_zones.append((bx, y, bx + 72, y + 90,
                                      lambda mk=mode_key: self._nav(mk)))

    def _draw_rail_icon(self, c, mode_key, bx, y, is_active):
        t = self.theme
        cx = bx + 36
        cy = y + 36
        icon_color = "#FFFFFF" if is_active else t["rail_icon_color"]

        if mode_key == "addmed":
            # Plus icon
            c.create_line(cx - 10, cy, cx + 10, cy,
                          fill=icon_color, width=5, capstyle="round")
            c.create_line(cx, cy - 10, cx, cy + 10,
                          fill=icon_color, width=5, capstyle="round")

        elif mode_key == "storage":
            # Pill bottle icon
            pw, ph = 20, 32
            px1, py1 = cx - pw // 2, cy - ph // 2
            # Outline
            c.create_oval(px1, py1, px1 + pw, py1 + ph,
                          outline=icon_color, width=2)
            # Bottom fill
            c.create_arc(px1, py1, px1 + pw, py1 + ph,
                         start=180, extent=180,
                         fill=ACCENT_BLUE, outline=ACCENT_BLUE)

        elif mode_key == "settings":
            # Gear icon — circle with teeth
            r_inner = 7
            c.create_oval(cx - r_inner, cy - r_inner,
                          cx + r_inner, cy + r_inner,
                          outline=icon_color, width=3)
            for angle in range(0, 360, 45):
                rad = math.radians(angle)
                x1 = cx + 10 * math.cos(rad)
                y1 = cy + 10 * math.sin(rad)
                x2 = cx + 14 * math.cos(rad)
                y2 = cy + 14 * math.sin(rad)
                c.create_line(x1, y1, x2, y2,
                              fill=icon_color, width=3, capstyle="round")

        elif mode_key == "home":
            # DOSE logo
            logo_loaded = False
            if PIL_AVAILABLE:
                logo_path = _find_logo()
                if logo_path:
                    try:
                        img = Image.open(logo_path)
                        resample = getattr(Image, 'LANCZOS',
                                           getattr(Image, 'ANTIALIAS', None))
                        img = img.resize((48, 48), resample)
                        tk_img = self._get_tk_image("home_logo", img)
                        c.create_image(cx, cy, image=tk_img, anchor="center")
                        logo_loaded = True
                    except Exception:
                        pass
            if not logo_loaded:
                # Fallback: rounded rect with notch (matching the HTML design)
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

        # Clock + date
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

        # TODAY'S SCHEDULE
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
                # Card background
                card_img = _pil_rounded_rect(608, card_h, 22, t["card_bg"])
                tk_card = self._get_tk_image(f"home_card_{i}", card_img)
                c.create_image(32, y, image=tk_card, anchor="nw")

                # Accent dot (44x44, radius 14)
                dot_img = _pil_rounded_rect(44, 44, 14, entry["accent"])
                tk_dot = self._get_tk_image(f"home_dot_{i}", dot_img)
                c.create_image(52, y + 16, image=tk_dot, anchor="nw")

                # Time
                c.create_text(112, y + 16, text=entry["time"],
                              font=self.font_name, fill=t["fg"], anchor="nw")
                # Name
                c.create_text(112, y + 44, text=entry["name"],
                              font=self.font_small, fill=t["muted"], anchor="nw")
                # Count
                cnt = entry.get("count", 0)
                cnt_text = f"{cnt} pills"
                c.create_text(600, y + 28, text=cnt_text,
                              font=self.font_title, fill=entry["accent"],
                              anchor="e")

                # Click zone for card
                self._click_zones.append(
                    (32, y, 640, y + card_h,
                     lambda k=entry["key"]: self._start_dispense_if_ok(k)))

        # Tap anywhere fallback for home
        self._click_zones.append(
            (0, 0, CONTENT_W, SCREEN_H, self._home_tap))

        # HW status
        hw = []
        if not CAMERA_AVAILABLE:
            hw.append("Camera not connected")
        if not self.has_touch:
            hw.append("Touch: " + (self.touch_error or "not detected"))
        if hw:
            c.create_text(32, SCREEN_H - 20, text="  ·  ".join(hw),
                          font=self.font_small, fill="#444444", anchor="sw")

    def _home_tap(self):
        for key in SLOT_KEYS:
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
        for key in SLOT_KEYS:
            md = self.med_data[key]
            if not md.get("loaded") or not self._is_qr_present(key):
                continue
            days = md.get("schedule_days", ALL_DAYS)
            if today_name not in days:
                continue
            dose_indices = md.get("doses", [2])
            for di, dose_idx in enumerate(dose_indices):
                time_str = TIME_PRESETS[dose_idx] if 0 <= dose_idx < len(TIME_PRESETS) else "8:00 AM"
                try:
                    t = datetime.strptime(time_str, "%I:%M %p").replace(
                        year=now.year, month=now.month, day=now.day)
                except Exception:
                    t = now
                try:
                    display = t.strftime("%-I:%M %p")
                except ValueError:
                    display = t.strftime("%I:%M %p").lstrip("0")
                entries.append({
                    "key": key, "dose_idx": di, "time": display,
                    "name": md["name"], "count": md.get("count", 0),
                    "accent": SLOT_COLORS[key], "sort": t,
                })
        entries.sort(key=lambda e: e["sort"])
        return entries

    # ══════════════════════════════════════════════════════════════════════
    #  STORAGE SCREEN
    # ══════════════════════════════════════════════════════════════════════
    def _draw_storage(self, c):
        t = self.theme

        # Left panel card (208x416, radius 22)
        left_img = _pil_rounded_rect(208, 416, 22, t["card_bg"])
        tk_left = self._get_tk_image("stor_left", left_img)
        c.create_image(32, 32, image=tk_left, anchor="nw")

        # Slot rows
        visible = [k for k in SLOT_KEYS
                   if self._is_loaded(k) and self._is_qr_present(k)]

        for i, key in enumerate(SLOT_KEYS):
            if key not in visible:
                continue
            md = self.med_data[key]
            accent = SLOT_COLORS[key]
            y = 48 + i * 96
            is_sel = (key == self.selected_pill)

            # Row background
            if is_sel:
                row_img = _pil_rounded_rect(188, 88, 16, t["elevated_bg"],
                                            outline=accent, outline_w=2)
            else:
                row_img = _pil_rounded_rect(188, 88, 16, t["elevated_bg"])
            tk_row = self._get_tk_image(f"stor_row_{key}", row_img)
            c.create_image(42, y, image=tk_row, anchor="nw")

            # Accent dot
            dot_img = _pil_rounded_rect(14, 14, 5, accent)
            tk_dot = self._get_tk_image(f"stor_dot_{key}", dot_img)
            c.create_image(56, y + 10, image=tk_dot, anchor="nw")

            # Name
            c.create_text(56, y + 30, text=md["name"],
                          font=self.font_small_bold, fill=t["fg"], anchor="nw")
            # Dose info
            doses = md.get("doses", [2])
            if len(doses) > 1:
                dose_text = f"{len(doses)}x daily"
            else:
                idx = doses[0] if doses else 2
                dose_text = f"Next dose {TIME_PRESETS[idx] if 0 <= idx < len(TIME_PRESETS) else '8:00 AM'}"
            c.create_text(56, y + 54, text=dose_text,
                          font=self.font_tiny, fill=t["muted"], anchor="nw")

            # Click zone
            self._click_zones.append(
                (42, y, 230, y + 88,
                 lambda k=key: self._storage_select(k)))

        # Right panel card (384x416, radius 22)
        right_img = _pil_rounded_rect(384, 416, 22, t["card_bg"])
        tk_right = self._get_tk_image("stor_right", right_img)
        c.create_image(256, 32, image=tk_right, anchor="nw")

        # Detail content
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
        accent = SLOT_COLORS[key]
        px = 280  # detail panel left padding

        # Med name
        c.create_text(px, 56, text=md["name"],
                      font=self.font_name_lg, fill=accent, anchor="nw")
        # Count
        cnt = md.get("count", 0)
        c.create_text(px, 92, text=f"{cnt} pills remaining",
                      font=self.font_body, fill=t["muted"], anchor="nw")
        # Separator
        c.create_line(px, 120, 616, 120, fill=t["divider"])

        # SCHEDULE label
        doses = md.get("doses", [2])
        c.create_text(px, 136, text=f"SCHEDULE ({len(doses)}x DAILY)",
                      font=self.font_label, fill=t["muted"], anchor="nw")

        # Dose chips
        chip_x = px
        for i, dose_idx in enumerate(doses):
            ts = TIME_PRESETS[dose_idx] if 0 <= dose_idx < len(TIME_PRESETS) else "8:00 AM"
            chip_w = 140
            chip_img = _pil_rounded_rect(chip_w, 52, 12, t["elevated_bg"])
            tk_chip = self._get_tk_image(f"dose_chip_{i}", chip_img)
            cy = 160
            c.create_image(chip_x, cy, image=tk_chip, anchor="nw")

            c.create_text(chip_x + chip_w // 2, cy + 10,
                          text=f"DOSE {i + 1}",
                          font=self.font_dose_label, fill=t["muted"],
                          anchor="center")
            c.create_text(chip_x + chip_w // 2, cy + 34,
                          text=ts, font=self.font_dose_time,
                          fill=t["fg"], anchor="center")

            # Prev/next arrows
            arr_y = cy + 34
            # Prev
            prev_img = _pil_rounded_rect(22, 22, 7, t["card_bg"])
            tk_prev = self._get_tk_image(f"dose_prev_{i}", prev_img)
            c.create_image(chip_x + 8, arr_y - 11, image=tk_prev, anchor="nw")
            c.create_text(chip_x + 19, arr_y, text="‹",
                          font=self.font_small_bold, fill=t["fg"],
                          anchor="center")
            self._click_zones.append(
                (chip_x + 8, arr_y - 11, chip_x + 30, arr_y + 11,
                 lambda idx=i: self._adj_dose_time(idx, -1)))

            # Next
            nx = chip_x + chip_w - 30
            tk_next = self._get_tk_image(f"dose_next_{i}", prev_img)
            c.create_image(nx, arr_y - 11, image=tk_next, anchor="nw")
            c.create_text(nx + 11, arr_y, text="›",
                          font=self.font_small_bold, fill=t["fg"],
                          anchor="center")
            self._click_zones.append(
                (nx, arr_y - 11, nx + 22, arr_y + 11,
                 lambda idx=i: self._adj_dose_time(idx, 1)))

            chip_x += chip_w + 8

        # DAYS
        c.create_text(px, 228, text="DAYS",
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
            c.create_image(dx, 252, image=tk_day, anchor="nw")
            c.create_text(dx + 21, 268, text=dl,
                          font=self.font_day, fill=dfg, anchor="center")

            self._click_zones.append(
                (dx, 252, dx + 42, 284,
                 lambda d=day_name: self._toggle_day(d)))
            dx += 48

        # DISPENSE button
        disp_y = 300
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

        # Main card (608x416, radius 22)
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

            # Divider between rows (except last)
            if idx < 3:
                c.create_line(px, y + row_h, 616, y + row_h,
                              fill=t["divider"])

            # Settings icon
            icon_img = _pil_settings_icon(40, SETTINGS_ICON_COLORS[idx])
            tk_icon = self._get_tk_image(f"set_icon_{idx}", icon_img)
            c.create_image(px, y + (row_h - 40) // 2, image=tk_icon, anchor="nw")

            # Label
            c.create_text(px + 56, y + row_h // 2 - 4, text=label,
                          font=self.font_settings, fill=t["fg"], anchor="w")

            if kind == "toggle":
                # Toggle switch
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
                # UPDATE button
                bw, bh = 104, 40
                bx = 520
                by = y + (row_h - bh) // 2
                btn_img = _pil_rounded_rect(bw, bh, 12, ACCENT_BLUE)
                tk_btn = self._get_tk_image("update_btn", btn_img)
                c.create_image(bx, by, image=tk_btn, anchor="nw")
                c.create_text(bx + bw // 2, by + bh // 2, text="UPDATE",
                              font=self.font_update_btn, fill="#FFFFFF",
                              anchor="center")

                # Status text
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
    #  ADD MEDICATION SCREEN
    # ══════════════════════════════════════════════════════════════════════
    def _draw_addmed(self, c):
        t = self.theme
        draft = self._draft

        # Find next available slot
        used = [k for k in SLOT_KEYS if self.med_data[k].get("loaded")]
        next_slot = "blue"
        for s in SLOT_KEYS:
            if s not in used:
                next_slot = s
                break
        self._draft_slot = next_slot
        slot_color = SLOT_COLORS[next_slot]

        # Main card
        card_img = _pil_rounded_rect(608, 416, 22, t["card_bg"])
        tk_card = self._get_tk_image("addmed_card", card_img)
        c.create_image(32, 32, image=tk_card, anchor="nw")

        pad = 56

        # MEDICATION NAME label
        c.create_text(pad, 56, text="MEDICATION NAME",
                      font=self.font_label, fill=t["muted"], anchor="nw")

        # Name input field
        inp_img = _pil_rounded_rect(500, 40, 12, t["elevated_bg"])
        tk_inp = self._get_tk_image("addmed_input", inp_img)
        c.create_image(pad, 76, image=tk_inp, anchor="nw")

        # Show current name or placeholder
        name_text = draft["name"] or "Type medication name"
        name_color = t["fg"] if draft["name"] else t["muted"]
        c.create_text(pad + 14, 96, text=name_text,
                      font=self.font_title, fill=name_color, anchor="w")

        # Slot color dot
        dot_img = _pil_rounded_rect(28, 28, 8, slot_color)
        tk_dot = self._get_tk_image("addmed_dot", dot_img)
        c.create_image(572, 82, image=tk_dot, anchor="nw")

        # Click on name field to enter text
        self._click_zones.append(
            (pad, 76, pad + 500, 116,
             lambda: self._show_name_input()))

        # QUANTITY section
        qy = 130
        q_img = _pil_rounded_rect(270, 90, 16, t["elevated_bg"])
        tk_q = self._get_tk_image("addmed_qty", q_img)
        c.create_image(pad, qy, image=tk_q, anchor="nw")

        c.create_text(pad + 135, qy + 14, text="QUANTITY",
                      font=self.font_tiny, fill=t["muted"], anchor="center")
        c.create_text(pad + 135, qy + 54, text=str(draft["qty"]),
                      font=self.font_count, fill=t["fg"], anchor="center")

        # Minus button
        mb_img = _pil_rounded_rect(40, 40, 12, t["card_bg"])
        tk_mb = self._get_tk_image("addmed_qminus", mb_img)
        c.create_image(pad + 30, qy + 34, image=tk_mb, anchor="nw")
        c.create_text(pad + 50, qy + 54, text="−",
                      font=self.font_body_bold, fill=t["fg"], anchor="center")
        self._click_zones.append(
            (pad + 30, qy + 34, pad + 70, qy + 74, self._dec_draft_qty))

        # Plus button
        pb_img = _pil_rounded_rect(40, 40, 12, t["card_bg"])
        tk_pb = self._get_tk_image("addmed_qplus", pb_img)
        c.create_image(pad + 200, qy + 34, image=tk_pb, anchor="nw")
        c.create_text(pad + 220, qy + 54, text="+",
                      font=self.font_body_bold, fill=t["fg"], anchor="center")
        self._click_zones.append(
            (pad + 200, qy + 34, pad + 240, qy + 74, self._inc_draft_qty))

        # TIMES PER DAY section
        tpd_img = _pil_rounded_rect(270, 90, 16, t["elevated_bg"])
        tk_tpd = self._get_tk_image("addmed_tpd", tpd_img)
        c.create_image(pad + 290, qy, image=tk_tpd, anchor="nw")

        c.create_text(pad + 425, qy + 14, text="TIMES PER DAY",
                      font=self.font_tiny, fill=t["muted"], anchor="center")
        c.create_text(pad + 425, qy + 54, text=str(draft["times_per_day"]),
                      font=self.font_count, fill=t["fg"], anchor="center")

        # TPD minus
        c.create_image(pad + 320, qy + 34, image=tk_mb, anchor="nw")
        c.create_text(pad + 340, qy + 54, text="−",
                      font=self.font_body_bold, fill=t["fg"], anchor="center")
        self._click_zones.append(
            (pad + 320, qy + 34, pad + 360, qy + 74, self._dec_draft_doses))

        # TPD plus
        c.create_image(pad + 490, qy + 34, image=tk_pb, anchor="nw")
        c.create_text(pad + 510, qy + 54, text="+",
                      font=self.font_body_bold, fill=t["fg"], anchor="center")
        self._click_zones.append(
            (pad + 490, qy + 34, pad + 530, qy + 74, self._inc_draft_doses))

        # DOSE TIMES
        c.create_text(pad, 236, text="DOSE TIMES",
                      font=self.font_tiny, fill=t["muted"], anchor="nw")

        chip_x = pad
        for i, dose_idx in enumerate(draft["doses"]):
            ts = TIME_PRESETS[dose_idx] if 0 <= dose_idx < len(TIME_PRESETS) else "8:00 AM"
            cw = min(130, 560 // max(1, len(draft["doses"])))
            chip_img = _pil_rounded_rect(cw, 52, 12, t["elevated_bg"])
            tk_chip = self._get_tk_image(f"addmed_dose_{i}", chip_img)
            c.create_image(chip_x, 256, image=tk_chip, anchor="nw")

            c.create_text(chip_x + cw // 2, 266, text=f"DOSE {i + 1}",
                          font=self.font_dose_label, fill=t["muted"],
                          anchor="center")
            c.create_text(chip_x + cw // 2, 290, text=ts,
                          font=self.font_dose_time, fill=t["fg"],
                          anchor="center")

            # Prev/next
            self._click_zones.append(
                (chip_x, 275, chip_x + cw // 2, 308,
                 lambda idx=i: self._adj_draft_dose(idx, -1)))
            self._click_zones.append(
                (chip_x + cw // 2, 275, chip_x + cw, 308,
                 lambda idx=i: self._adj_draft_dose(idx, 1)))

            chip_x += cw + 8

        # DAYS
        c.create_text(pad, 320, text="DAYS",
                      font=self.font_tiny, fill=t["muted"], anchor="nw")

        dx = pad
        for i, dl in enumerate(DAY_LABELS):
            active = draft["days"][i]
            dbg = slot_color if active else t["elevated_bg"]
            dfg = "#0A0A0C" if active else t["muted"]
            day_img = _pil_rounded_rect(70, 36, 10, dbg)
            tk_day = self._get_tk_image(f"addmed_day_{i}", day_img)
            c.create_image(dx, 340, image=tk_day, anchor="nw")
            c.create_text(dx + 35, 358, text=dl,
                          font=self.font_day, fill=dfg, anchor="center")
            self._click_zones.append(
                (dx, 340, dx + 70, 376,
                 lambda idx=i: self._toggle_draft_day(idx)))
            dx += 76

        # ADD MEDICATION button
        btn_y = 390
        btn_img = _pil_rounded_rect(560, 52, 16, ACCENT_BLUE)
        tk_btn = self._get_tk_image("addmed_submit", btn_img)
        c.create_image(pad, btn_y, image=tk_btn, anchor="nw")
        c.create_text(pad + 280, btn_y + 26, text="ADD MEDICATION",
                      font=self.font_btn_lg, fill="#FFFFFF", anchor="center")
        self._click_zones.append(
            (pad, btn_y, pad + 560, btn_y + 52, self._submit_add_med))

    def _show_name_input(self):
        """Show a Tkinter Entry temporarily over the canvas for text input."""
        if hasattr(self, '_name_entry_widget') and self._name_entry_widget:
            return
        t = self.theme
        entry = tk.Entry(self.root, font=self.font_title,
                         bg=t["elevated_bg"], fg=t["fg"],
                         insertbackground=t["fg"], bd=0, relief="flat")
        entry.place(x=70, y=76, width=486, height=40)
        entry.insert(0, self._draft["name"])
        entry.focus_set()
        entry.bind("<Return>", lambda e: self._commit_name_input())
        entry.bind("<FocusOut>", lambda e: self._commit_name_input())
        self._name_entry_widget = entry

    def _commit_name_input(self):
        if hasattr(self, '_name_entry_widget') and self._name_entry_widget:
            self._draft["name"] = self._name_entry_widget.get().strip()
            self._name_entry_widget.destroy()
            self._name_entry_widget = None
            self._draw_frame()

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
        accent = SLOT_COLORS[self.dispense_pill]

        # HOLD SCREEN TO DISPENSE
        cx = CONTENT_W // 2
        c.create_text(cx, 70, text="HOLD SCREEN TO DISPENSE",
                      font=self.font_label, fill=t["muted"], anchor="center")
        c.create_text(cx, 96, text=md["name"],
                      font=self.font_name_lg, fill=accent, anchor="center")

        cnt = md["count"]
        c.create_text(cx, 132, text=f"{cnt} pill{'s' if cnt != 1 else ''} remaining",
                      font=self.font_body, fill=t["muted"], anchor="center")

        # Circular progress ring
        elapsed = time.time() - self.hold_start if self.hold_start > 0 else 0
        progress = min(elapsed / HOLD_TIME, 1.0) if self.hold_start > 0 else 0

        ring_size = 200
        ring_img = _pil_ring(ring_size, progress, accent,
                             t["elevated_bg"], t["card_bg"])
        tk_ring = self._get_tk_image("hold_ring", ring_img)
        ring_x = cx - ring_size // 2
        ring_y = 168
        c.create_image(ring_x, ring_y, image=tk_ring, anchor="nw")

        # Countdown text
        remaining = max(0, HOLD_TIME - elapsed) if self.hold_start > 0 else HOLD_TIME
        c.create_text(cx, ring_y + ring_size // 2 - 10,
                      text=f"{remaining:.1f}",
                      font=self.font_hold_big, fill=t["fg"], anchor="center")
        c.create_text(cx, ring_y + ring_size // 2 + 22,
                      text="SECONDS", font=self.font_hold_label,
                      fill=t["muted"], anchor="center")

        # Cancel button
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

    # ── Hold press/release on canvas ──────────────────────────────────────
    def _on_canvas_press(self, event):
        if self.mode == "hold" and self.dispense_state == 2:
            # Check if NOT clicking cancel
            cx = CONTENT_W // 2
            cancel_x = cx - 80
            cancel_y = 388
            if not (cancel_x <= event.x <= cancel_x + 160 and
                    cancel_y <= event.y <= cancel_y + 52):
                self.hold_start = time.time()
                self._hold_update()

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
        self.dispense_state = 4
        self._play_sound()
        self.mode = "dispensed"
        self._draw_frame()
        self.root.after(int(DISPENSED_TIME * 1000), self._end_dispense)

    def _draw_dispensed(self, c):
        t = self.theme
        md = self.med_data[self.dispense_pill]
        accent = SLOT_COLORS[self.dispense_pill]
        cx = CONTENT_W // 2

        c.create_text(cx, 68, text="DISPENSED",
                      font=self.font_label, fill=t["muted"], anchor="center")
        c.create_text(cx, 92, text=md["name"],
                      font=self.font_name_lg, fill=accent, anchor="center")

        # Checkmark circle
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
        accent = SLOT_COLORS[self._qty_slot]
        cx = CONTENT_W // 2

        # Card
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

        # Minus button
        mb_img = _pil_rounded_rect(56, 56, 16, t["elevated_bg"])
        tk_mb = self._get_tk_image("qty_minus", mb_img)
        c.create_image(260, 218, image=tk_mb, anchor="nw")
        c.create_text(288, 246, text="−", font=self.font_count,
                      fill=t["fg"], anchor="center")
        self._click_zones.append(
            (260, 218, 316, 274, lambda: self._qty_adjust(-1)))

        # Plus button
        pb_img = _pil_rounded_rect(56, 56, 16, t["elevated_bg"])
        tk_pb = self._get_tk_image("qty_plus", pb_img)
        c.create_image(340, 218, image=tk_pb, anchor="nw")
        c.create_text(368, 246, text="+", font=self.font_count,
                      fill=t["fg"], anchor="center")
        self._click_zones.append(
            (340, 218, 396, 274, lambda: self._qty_adjust(1)))

        # CONFIRM button
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

        # Create bottle image
        bottle_img = _pil_rounded_rect(bw, bh, 16, accent)
        # Draw cap on top
        d = ImageDraw.Draw(bottle_img)
        cap_color = tuple(max(0, c - 40) for c in _hex_to_rgb(accent)) + (255,)
        d.rounded_rectangle([0, 0, bw - 1, 25], radius=16, fill=cap_color)

        self._bottle_tk = self._get_tk_image("bottle_anim", bottle_img)
        self._bottle_x = cx - bw // 2
        self._bottle_y = start_y
        self._bottle_end_y = end_y
        self._bottle_total_dist = end_y - start_y
        self._bottle_label = label_text

        # Create overlay canvas
        self._anim_overlay = tk.Canvas(self.root, width=CONTENT_W,
                                       height=SCREEN_H,
                                       highlightthickness=0,
                                       bg=self.theme["bg"])
        self._anim_overlay.place(x=0, y=0, width=CONTENT_W, height=SCREEN_H)
        self._raise_widget(self._anim_overlay)

        # Semi-transparent overlay
        self._anim_overlay.create_rectangle(0, 0, CONTENT_W, SCREEN_H,
                                            fill="#000000",
                                            stipple="gray25")
        self._anim_bottle_id = self._anim_overlay.create_image(
            self._bottle_x, start_y, image=self._bottle_tk, anchor="nw")
        # Name on bottle
        self._anim_text_id = self._anim_overlay.create_text(
            cx, start_y + bh // 2 + 20, text=name,
            font=self.font_small_bold, fill="#0A0A0C")
        # Status
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
        self._update_status_text = "Checking for updates…"
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
                    slot_assignments = []
                    for idx, (x_pos, text) in enumerate(qr_with_pos):
                        if idx < len(SLOT_KEYS):
                            slot_assignments.append((SLOT_KEYS[idx], text))
                    self.root.after(0, self._handle_qr_batch, slot_assignments)
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
