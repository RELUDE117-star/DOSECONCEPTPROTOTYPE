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
from datetime import datetime, timedelta
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
    try:
        from pyzbar.pyzbar import ZBarSymbol
        _QR_ONLY = [ZBarSymbol.QRCODE]
    except Exception:
        _QR_ONLY = None
    if PIL_AVAILABLE:
        CAMERA_AVAILABLE = True
except Exception:
    pass


def _scan_qr(img):
    """Decode ONLY QR codes — never other barcode types, which is what
    caused random objects to register as 'codes'."""
    if _QR_ONLY is not None:
        return pyzbar_decode(img, symbols=_QR_ONLY)
    return pyzbar_decode(img)

TOUCH_SENSOR_ENABLED = False  # set True to re-enable MPR121 touch sensor

HAVE_MPR121 = False
mpr121_mod = None
if TOUCH_SENSOR_ENABLED:
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
SPIN_TIME = 4.0
DISPENSED_TIME = 4.0
DEFAULT_QTY = 30
QR_PRESENCE_TIMEOUT = 2.5   # removal shows within ~2.5s of pickup
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
DOSE_BLUE = "#6AA7F8"      # website dose-blue (buttons, rings, counts)
DOSE_BLUE_LT = "#8FBAFA"   # website light blue (countdown text, spin arrow)
DOSE_GREEN = "#5BD07B"     # website success green
SETTINGS_ICON_COLORS = ["#5FA3F5", "#FF9F43", "#30D158", "#AF52DE"]

KNOWN_SLOTS = {
    "blue": "Sertraline",
    "red": "Lisinopril",
    "green": "Metformin",
    "yellow": "Atorvastatin",
}

# Real-world guidance for the demo medications (per common prescribing info)
MED_INFO = {
    "sertraline": [
        "Antidepressant (SSRI)",
        "Take with a full glass of water",
        "With or without food — keep a consistent time",
    ],
    "lisinopril": [
        "Blood pressure (ACE inhibitor)",
        "With or without food, plus water",
        "Stand up slowly — can cause dizziness",
    ],
    "metformin": [
        "Type 2 diabetes",
        "Take WITH a meal to protect your stomach",
        "Swallow whole with a glass of water",
    ],
    "atorvastatin": [
        "Cholesterol (statin)",
        "With or without food, plus water",
        "Avoid grapefruit juice",
    ],
}
MED_INFO_DEFAULT = ["Follow the directions on your prescription label"]


def _parse_time12(s):
    """'8:05 PM' -> (20, 5). Falls back to 8:00 AM."""
    try:
        dt = datetime.strptime(s.strip(), "%I:%M %p")
        return dt.hour, dt.minute
    except Exception:
        return 8, 0


def _fmt_time12(h24, m):
    """(20, 5) -> '8:05 PM'"""
    ap = "AM" if h24 < 12 else "PM"
    h = h24 % 12
    if h == 0:
        h = 12
    return f"{h}:{m:02d} {ap}"


def _day_part(hour):
    """Rough part of day: morning / midday / evening."""
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "midday"
    return "evening"

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


def _pil_stroke_ring(size, progress, scale=2):
    """Website-style hold ring: faint white track + clean blue arc."""
    ss = size * scale
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    lw = 15 * scale
    pad = lw // 2 + scale
    box = [pad, pad, ss - 1 - pad, ss - 1 - pad]
    d.arc(box, 0, 360, fill=(255, 255, 255, 31), width=lw)
    if progress > 0:
        d.arc(box, -90, -90 + progress * 360,
              fill=_hex_to_rgba(DOSE_BLUE), width=lw)
    resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
    return img.resize((size, size), resample)


def _pil_spinner(size, angle, scale=2):
    """Website-style spin dial: rotating dashed blue circle with a
    light-blue circular arrow in the middle (matches dose.html)."""
    import math
    ss = size * scale
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ac = _hex_to_rgba(DOSE_BLUE)
    lt = _hex_to_rgba(DOSE_BLUE_LT)

    # Dashed outer circle (dash ~14px on / 12px off at website scale)
    lw = 6 * scale
    pad = lw // 2 + scale
    r = (ss - 2 * pad) / 2
    circumference = 2 * math.pi * r
    dash_deg = 360 * (14 * scale) / circumference
    gap_deg = 360 * (12 * scale) / circumference
    box = [pad, pad, ss - 1 - pad, ss - 1 - pad]
    a = angle
    while a < angle + 360:
        end = min(a + dash_deg, angle + 360)
        d.arc(box, a, end, fill=ac, width=lw)
        a += dash_deg + gap_deg

    # Center circular arrow (↻): open arc + arrowhead, rotates with dial
    cx = cy = ss / 2
    ar = ss * 0.17
    aw = 9 * scale
    abox = [cx - ar, cy - ar, cx + ar, cy + ar]
    arc_start = angle - 60
    arc_end = arc_start + 290
    d.arc(abox, arc_start, arc_end, fill=lt, width=aw)
    # arrowhead at the arc end, pointing along direction of travel
    te = math.radians(arc_end)
    tx, ty = cx + ar * math.cos(te), cy + ar * math.sin(te)
    tang = te + math.pi / 2  # tangent (clockwise travel)
    ah = 22 * scale
    hw = 13 * scale
    tipx, tipy = tx + ah * math.cos(tang), ty + ah * math.sin(tang)
    perp = tang + math.pi / 2
    b1 = (tx + hw * math.cos(perp), ty + hw * math.sin(perp))
    b2 = (tx - hw * math.cos(perp), ty - hw * math.sin(perp))
    d.polygon([b1, b2, (tipx, tipy)], fill=lt)
    resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
    return img.resize((size, size), resample)



def _pil_clock_icon(size, color, scale=2):
    """Small clock face: circle outline + hour/minute hands."""
    ss = size * scale
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    col = _hex_to_rgba(color)
    lw = max(2, 2 * scale)
    pad = lw
    d.ellipse([pad, pad, ss - 1 - pad, ss - 1 - pad], outline=col, width=lw)
    cx = cy = ss / 2
    d.line([cx, cy, cx, cy - ss * 0.28], fill=col, width=lw)       # minute
    d.line([cx, cy, cx + ss * 0.20, cy + ss * 0.06], fill=col, width=lw)  # hour
    resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
    return img.resize((size, size), resample)


def _pil_status_icon(size, kind, scale=2):
    """Small per-dose status mark for home cards:
    'taken'  — filled blue circle with a white check
    'missed' — red-outlined circle with a red X"""
    ss = size * scale
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    lw = max(2, 3 * scale)
    if kind == "taken":
        blue = _hex_to_rgba(DOSE_BLUE)
        d.ellipse([0, 0, ss - 1, ss - 1], fill=blue)
        w = (255, 255, 255, 255)
        d.line([ss * 0.26, ss * 0.52, ss * 0.44, ss * 0.68], fill=w,
               width=lw)
        d.line([ss * 0.44, ss * 0.68, ss * 0.74, ss * 0.34], fill=w,
               width=lw)
    else:
        red = _hex_to_rgba("#FF6B6B")
        pad = lw // 2 + scale
        d.ellipse([pad, pad, ss - 1 - pad, ss - 1 - pad], outline=red,
                  width=lw)
        d.line([ss * 0.34, ss * 0.34, ss * 0.66, ss * 0.66], fill=red,
               width=lw)
        d.line([ss * 0.66, ss * 0.34, ss * 0.34, ss * 0.66], fill=red,
               width=lw)
    resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
    return img.resize((size, size), resample)


def _pil_soft_check(size, scale=2):
    """Success mark: translucent circle + check in the signature blue."""
    ss = size * scale
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    g = _hex_to_rgba(DOSE_BLUE)
    d.ellipse([0, 0, ss - 1, ss - 1], fill=(g[0], g[1], g[2], 38))
    lw = max(3, 5 * scale)
    x1, y1 = int(ss * 0.30), int(ss * 0.52)
    x2, y2 = int(ss * 0.44), int(ss * 0.66)
    x3, y3 = int(ss * 0.72), int(ss * 0.36)
    d.line([x1, y1, x2, y2], fill=g, width=lw)
    d.line([x2, y2, x3, y3], fill=g, width=lw)
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
        self.font_time_big = tkfont.Font(family=f_xb, size=60, weight="bold")
        self.font_time_huge = tkfont.Font(family=f_xb, size=66, weight="bold")
        self.font_day_lg = tkfont.Font(family=f_b, size=15, weight="bold")
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
        self.spin_start = 0
        self.spin_after_id = None
        self._hold_ring_id = None
        self._hold_secs_id = None
        self._spin_ring_id = None
        self._due_keys = {}
        self._due_prev = set()
        self._banner_dismissed = False
        self._alert_key = None
        self._alert_return = "home"
        self._addmed_cancel_time = 0.0
        self._qty_cancel_time = 0.0
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

        # Camera debug view
        self._camera_view = False
        self._camera_frame = None
        self._camera_qr_results = []
        self._settings_tap_count = 0
        self._settings_tap_time = 0

        self._draft = {
            "name": "", "times_per_day": 1,
            "doses": [2], "dose_times": ["8:00 AM"], "qty": 30,
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
        if TOUCH_SENSOR_ENABLED and HAVE_MPR121:
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
        elif TOUCH_SENSOR_ENABLED:
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
        for md in self.med_data.values():
            self._ensure_dose_times(md)

    @staticmethod
    def _ensure_dose_times(md):
        """Migrate hour-preset indices to minute-precision time strings."""
        if not md.get("dose_times"):
            md["dose_times"] = [
                TIME_PRESETS[i] if 0 <= i < len(TIME_PRESETS) else "8:00 AM"
                for i in md.get("doses", [2])
            ]
        if not md.get("tracking_since"):
            md["tracking_since"] = datetime.now().isoformat()

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
        elif self.mode == "camview":
            self._draw_camview(c)
        elif self.mode == "hold":
            self._draw_hold(c)
        elif self.mode == "spin":
            self._draw_spin(c)
        elif self.mode == "confirmdisp":
            self._draw_confirm_dispense(c)
        elif self.mode == "dispensed":
            self._draw_dispensed(c)
        elif self.mode == "qtyconfirm":
            self._draw_qty_confirm(c)
        elif self.mode == "timeedit":
            self._draw_time_edit(c)
        elif self.mode == "dosealert":
            self._draw_dose_alert(c)

        # Due-dose notification banner on the main screens
        if (self._due_keys and not self._banner_dismissed
                and self.mode in ("home", "storage", "settings", "user")):
            self._draw_due_banner(c)

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
        if active == "timeedit":
            active = getattr(self, "_te_return", "storage")
        if active == "dosealert":
            active = getattr(self, "_alert_return", "home")
        if active in ("hold", "spin", "confirmdisp", "dispensed",
                      "qtyconfirm", "addmed"):
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
        self._hide_keyboard(save=True)
        if self.mode == "timeedit":
            self._te_commit()  # rail nav keeps schedule edits, like keyboard
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
            # Cards fill from y=88 to y=456 (368px), 4 cards with gaps
            num = min(len(sched), 4)
            card_gap = 10
            total_h = 370
            card_h = (total_h - card_gap * (num - 1)) // num
            card_w = 620

            for i, entry in enumerate(sched[:num]):
                y = 88 + i * (card_h + card_gap)
                due = entry["key"] in self._due_keys
                card_img = _pil_rounded_rect(
                    card_w, card_h, 22, t["card_bg"],
                    outline=DOSE_BLUE if due else None,
                    outline_w=3 if due else 0)
                tk_card = self._get_tk_image(f"home_card_{i}", card_img)
                c.create_image(26, y, image=tk_card, anchor="nw")

                # Website-style card: name on top, time below, count right
                text_x = 26 + 30
                c.create_text(text_x, y + card_h // 2 - 13,
                              text=self._fit_text(entry["name"],
                                                  self.font_name, 420),
                              font=self.font_name, fill=t["fg"], anchor="w")
                if due:
                    c.create_text(text_x, y + card_h // 2 + 14,
                                  text=f"Time to take · {entry['time']}",
                                  font=self.font_small_bold, fill=DOSE_BLUE,
                                  anchor="w")
                else:
                    c.create_text(text_x, y + card_h // 2 + 14,
                                  text=entry["time"], font=self.font_small,
                                  fill=t["muted"], anchor="w")

                cnt = entry.get("count", 0)
                cnt_text = f"{cnt} pills left"
                count_right = 26 + card_w - 28
                c.create_text(count_right, y + card_h // 2,
                              text=cnt_text,
                              font=self.font_title, fill=DOSE_BLUE,
                              anchor="e")

                # Per-dose status: blue check = taken in its window,
                # red circled X = window passed without taking it right
                status = self._dose_status(entry["key"], entry["time"])
                if status:
                    icon_size = 30
                    icon_img = _pil_status_icon(
                        icon_size, "taken" if status == "taken" else "missed")
                    tk_icon = self._get_tk_image(f"dose_status_{i}",
                                                 icon_img)
                    try:
                        tw = self.font_title.measure(cnt_text)
                    except Exception:
                        tw = 120
                    c.create_image(count_right - tw - 14 - icon_size,
                                   y + card_h // 2 - icon_size // 2,
                                   image=tk_icon, anchor="nw")

                self._click_zones.append(
                    (26, y, 26 + card_w, y + card_h,
                     lambda k=entry["key"]: self._start_dispense_if_ok(k)))

        self._click_zones.append((0, 0, CONTENT_W, SCREEN_H, self._home_tap))

        hw = []
        if not CAMERA_AVAILABLE:
            hw.append("Camera not connected")
        if TOUCH_SENSOR_ENABLED and not self.has_touch:
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
            self._ensure_dose_times(md)
            for di, time_str in enumerate(md["dose_times"]):
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

        left_img = _pil_rounded_rect(212, 440, 22, t["card_bg"])
        tk_left = self._get_tk_image("stor_left", left_img)
        c.create_image(26, 20, image=tk_left, anchor="nw")

        all_keys = SLOT_KEYS + (["demo"] if self._demo_registered else [])
        visible = [k for k in all_keys
                   if self._is_loaded(k) and self._is_qr_present(k)]

        for i, key in enumerate(all_keys):
            if key not in visible:
                continue
            md = self.med_data[key]
            accent = SLOT_COLORS.get(key, "#C084FC")
            vi = visible.index(key)
            y = 36 + vi * 100
            is_sel = (key == self.selected_pill)

            if is_sel:
                row_img = _pil_rounded_rect(192, 92, 16, t["elevated_bg"],
                                            outline=accent, outline_w=2)
            else:
                row_img = _pil_rounded_rect(192, 92, 16, t["elevated_bg"])
            tk_row = self._get_tk_image(f"stor_row_{key}", row_img)
            c.create_image(36, y, image=tk_row, anchor="nw")

            dot_img = _pil_rounded_rect(16, 16, 6, accent)
            tk_dot = self._get_tk_image(f"stor_dot_{key}", dot_img)
            c.create_image(50, y + 12, image=tk_dot, anchor="nw")

            c.create_text(50, y + 34,
                          text=self._fit_text(md["name"],
                                              self.font_small_bold, 168),
                          font=self.font_small_bold, fill=t["fg"], anchor="nw")
            self._ensure_dose_times(md)
            times = md["dose_times"]
            if len(times) == 1:
                dose_text = f"Next dose {times[0]}"
            elif len(times) == 2:
                dose_text = " · ".join(times)
            else:
                dose_text = f"{len(times)}x daily"
            c.create_text(50, y + 58, text=dose_text,
                          font=self.font_tiny, fill=t["muted"], anchor="nw")

            self._click_zones.append(
                (36, y, 228, y + 92,
                 lambda k=key: self._storage_select(k)))

        right_img = _pil_rounded_rect(400, 440, 22, t["card_bg"])
        tk_right = self._get_tk_image("stor_right", right_img)
        c.create_image(248, 20, image=tk_right, anchor="nw")

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
        px = 272

        c.create_text(px, 40,
                      text=self._fit_text(md["name"],
                                          self.font_name_lg, 330),
                      font=self.font_name_lg, fill=accent, anchor="nw")
        cnt = md.get("count", 0)
        c.create_text(px, 76, text=f"{cnt} pills remaining",
                      font=self.font_body, fill=t["muted"], anchor="nw")
        c.create_line(px, 104, 630, 104, fill=t["divider"])

        self._ensure_dose_times(md)

        # SCHEDULE header + small clock button (expands to a big editor)
        c.create_text(px, 116, text="SCHEDULE",
                      font=self.font_label, fill=t["muted"], anchor="nw")
        clk_size = 36
        clk_x, clk_y = 594, 108
        clk_bg = _pil_rounded_rect(clk_size, clk_size, 10, t["elevated_bg"])
        tk_clk_bg = self._get_tk_image("sched_clk_bg", clk_bg)
        c.create_image(clk_x, clk_y, image=tk_clk_bg, anchor="nw")
        clk_icon = _pil_clock_icon(20, t["fg"])
        tk_clk = self._get_tk_image("sched_clk", clk_icon)
        c.create_image(clk_x + 8, clk_y + 8, image=tk_clk, anchor="nw")
        self._click_zones.append(
            (clk_x - 6, clk_y - 6, clk_x + clk_size + 6, clk_y + clk_size + 6,
             self._open_time_edit))

        # Schedule in plain words, e.g. "Take at 7:00 PM on Sat & Sun"
        c.create_text(px, 140, text=self._schedule_sentence(md),
                      font=self.font_body, fill=t["fg"], anchor="nw",
                      width=350)


        # MEDICATION INFO — real guidance for the loaded drug
        info_y = 196
        c.create_text(px, info_y, text="MEDICATION INFO",
                      font=self.font_label, fill=t["muted"], anchor="nw")
        info_lines = MED_INFO.get(md.get("name", "").strip().lower(),
                                  MED_INFO_DEFAULT)
        ly = info_y + 28
        try:
            line_h = self.font_body.metrics("linespace")
        except Exception:
            line_h = 26
        for line in info_lines[:3]:
            txt = "·  " + line
            c.create_text(px, ly, text=txt,
                          font=self.font_body, fill=t["fg"], anchor="nw",
                          width=354)
            # advance by however many rows the text actually wraps to,
            # so wrapped lines never draw over the next one
            try:
                rows = max(1, math.ceil(self.font_body.measure(txt) / 354))
            except Exception:
                rows = 1
            ly += rows * line_h + 8

        # DISPENSE button — pinned to the bottom of the card
        disp_y = 396
        disp_w = 370
        disp_h = 52
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

    def _schedule_sentence(self, md):
        """Schedule in plain words: 'Take at 7:00 PM on Sat & Sun'."""
        self._ensure_dose_times(md)
        times = md["dose_times"]
        if len(times) == 1:
            time_part = times[0]
        else:
            time_part = " & ".join(times) if len(times) == 2 else \
                ", ".join(times[:-1]) + " & " + times[-1]

        days = md.get("schedule_days", list(ALL_DAYS))
        if len(days) >= 7:
            day_part = "every day"
        elif sorted(days) == sorted(["Mon", "Tue", "Wed", "Thu", "Fri"]):
            day_part = "on weekdays"
        elif sorted(days) == sorted(["Sat", "Sun"]):
            day_part = "on weekends"
        elif not days:
            day_part = "(no days selected)"
        else:
            ordered = [d for d in ALL_DAYS if d in days]
            day_part = "on " + (" & ".join(ordered) if len(ordered) <= 2
                                else ", ".join(ordered[:-1]) + " & " + ordered[-1])
        return f"Take at {time_part} {day_part}"

    # ── Big time editor (opened from the small clock button) ──────────────
    def _open_time_edit(self, target="med"):
        self._te_target = target
        if target == "draft":
            if not self._draft.get("dose_times"):
                self._draft["dose_times"] = [
                    TIME_PRESETS[i] if 0 <= i < len(TIME_PRESETS)
                    else "8:00 AM" for i in self._draft.get("doses", [2])]
            times = self._draft["dose_times"]
        else:
            md = self.med_data[self.selected_pill]
            self._ensure_dose_times(md)
            times = md["dose_times"]
        self._hide_keyboard(save=True)
        self._te_times = [list(_parse_time12(ts)) for ts in times]
        self._te_sel = 0
        self._te_return = self.mode
        self.mode = "timeedit"
        self._draw_frame()

    def _te_day_flags(self):
        """Active flag per ALL_DAYS for the current edit target."""
        if self._te_target == "draft":
            return [bool(v) for v in self._draft["days"]]
        days = self.med_data[self.selected_pill].get(
            "schedule_days", list(ALL_DAYS))
        return [d in days for d in ALL_DAYS]

    def _te_toggle_day(self, i):
        if self._te_target == "draft":
            self._draft["days"][i] = 0 if self._draft["days"][i] else 1
        else:
            self._toggle_day(ALL_DAYS[i])
            return  # _toggle_day already saves + redraws
        self._draw_frame()

    def _draw_time_edit(self, c):
        t = self.theme
        if self._te_target == "draft":
            title_name = self._draft["name"] or "New Medication"
            accent = SLOT_COLORS.get(
                getattr(self, "_draft_slot", "demo"), "#C084FC")
        else:
            md = self.med_data[self.selected_pill]
            title_name = md["name"]
            accent = SLOT_COLORS.get(self.selected_pill, "#C084FC")

        card_img = _pil_rounded_rect(620, 440, 22, t["card_bg"])
        tk_card = self._get_tk_image("te_card", card_img)
        c.create_image(26, 20, image=tk_card, anchor="nw")

        n = len(self._te_times)
        if self._te_sel >= n:
            self._te_sel = n - 1
        sel = self._te_sel

        # ── Top bar: label left, medication name centered, DONE right ──
        c.create_text(56, 58, text="SCHEDULE",
                      font=self.font_label, fill=t["muted"], anchor="w")
        c.create_text(336, 58,
                      text=self._fit_text(title_name, self.font_title, 250),
                      font=self.font_title, fill=accent, anchor="center")
        done_w, done_h = 110, 44
        done_x, done_y = 616 - done_w, 36
        done_img = _pil_rounded_rect(done_w, done_h, done_h // 2, DOSE_BLUE)
        tk_done = self._get_tk_image("te_done", done_img)
        c.create_image(done_x, done_y, image=tk_done, anchor="nw")
        c.create_text(done_x + done_w // 2, done_y + done_h // 2,
                      text="DONE", font=self.font_btn, fill="#06101E",
                      anchor="center")
        self._click_zones.append(
            (done_x, done_y, done_x + done_w, done_y + done_h,
             self._te_done))

        # ── Time chips with iOS-style red X badges on top-right ──
        tab_w, tab_h, tab_gap = 158, 56, 16
        total = n * tab_w + (n - 1) * tab_gap + (tab_h + tab_gap if n < 3
                                                 else 0)
        tx = max(56, (26 + 646 - total) // 2)
        ty = 102
        badge = 26
        for i, (h24, mi) in enumerate(self._te_times):
            active = (i == sel)
            tbg = DOSE_BLUE if active else t["elevated_bg"]
            tfg = "#06101E" if active else t["fg"]
            tab_img = _pil_rounded_rect(tab_w, tab_h, 16, tbg)
            tk_tab = self._get_tk_image(f"te_tab_{i}", tab_img)
            c.create_image(tx, ty, image=tk_tab, anchor="nw")
            c.create_text(tx + tab_w // 2, ty + tab_h // 2,
                          text=_fmt_time12(h24, mi),
                          font=self.font_title, fill=tfg,
                          anchor="center")
            self._click_zones.append(
                (tx, ty, tx + tab_w, ty + tab_h,
                 lambda idx=i: self._te_select(idx)))

            if n > 1:
                # red circled X badge, overlapping the top-right corner
                bx = tx + tab_w - badge // 2 - 6
                by = ty - badge // 2 + 6
                b_img = _pil_status_icon(badge, "missed")
                tk_b = self._get_tk_image(f"te_badge_{i}", b_img)
                c.create_image(bx, by, image=tk_b, anchor="nw")
                self._click_zones.append(
                    (bx - 6, by - 6, bx + badge + 6, by + badge + 6,
                     lambda idx=i: self._te_remove(idx)))
            tx += tab_w + tab_gap

        if n < 3:
            add_img = _pil_rounded_rect(tab_h, tab_h, 16, t["elevated_bg"])
            tk_add = self._get_tk_image("te_tab_add", add_img)
            c.create_image(tx, ty, image=tk_add, anchor="nw")
            c.create_text(tx + tab_h // 2, ty + tab_h // 2, text="+",
                          font=self.font_count, fill=DOSE_BLUE,
                          anchor="center")
            self._click_zones.append(
                (tx, ty, tx + tab_h, ty + tab_h, self._te_add))

        # ── Selected time, huge, with a big AM/PM toggle ──
        h24, mi = self._te_times[sel]
        h12 = h24 % 12
        if h12 == 0:
            h12 = 12
        c.create_text(290, 212, text=f"{h12}:{mi:02d}",
                      font=self.font_time_huge, fill=t["fg"],
                      anchor="center")
        ap = "AM" if h24 < 12 else "PM"
        ap_w, ap_h = 88, 58
        ap_x, ap_y = 452, 184
        ap_img = _pil_rounded_rect(ap_w, ap_h, 16, t["elevated_bg"],
                                   outline=DOSE_BLUE, outline_w=2)
        tk_ap = self._get_tk_image("te_ap", ap_img)
        c.create_image(ap_x, ap_y, image=tk_ap, anchor="nw")
        c.create_text(ap_x + ap_w // 2, ap_y + ap_h // 2, text=ap,
                      font=self.font_btn_lg, fill=DOSE_BLUE,
                      anchor="center")
        self._click_zones.append(
            (ap_x, ap_y, ap_x + ap_w, ap_y + ap_h,
             lambda: self._te_adj(self._te_sel, "h", 12)))

        # ── Big steppers: hour pair left, minute pair right ──
        btn = 76
        sy = 262
        for label, x0, field, step in (("HOUR", 100, "h", 1),
                                       ("MINUTES", 412, "m", 5)):
            for j, (sym, sign) in enumerate((("−", -1), ("+", 1))):
                bx = x0 + j * (btn + 8)
                b_img = _pil_rounded_rect(btn, btn, 18, t["elevated_bg"])
                tk_b = self._get_tk_image(f"te_{field}{j}", b_img)
                c.create_image(bx, sy, image=tk_b, anchor="nw")
                c.create_text(bx + btn // 2, sy + btn // 2, text=sym,
                              font=self.font_hold_big, fill=t["fg"],
                              anchor="center")
                self._click_zones.append(
                    (bx, sy, bx + btn, sy + btn,
                     lambda f=field, s=sign * step:
                     self._te_adj(self._te_sel, f, s)))
            c.create_text(x0 + btn + 4, sy + btn + 16, text=label,
                          font=self.font_label, fill=t["muted"],
                          anchor="center")

        # ── Bigger day chips ──
        flags = self._te_day_flags()
        chip_w, chip_h, chip_gap = 58, 44, 8
        total_w = 7 * chip_w + 6 * chip_gap
        dx = (26 + 646 - total_w) // 2
        day_y = 388
        for i, dl in enumerate(DAY_LABELS):
            dbg = accent if flags[i] else t["elevated_bg"]
            dfg = "#0A0A0C" if flags[i] else t["muted"]
            day_img = _pil_rounded_rect(chip_w, chip_h, 12, dbg)
            tk_day = self._get_tk_image(f"te_day_{i}", day_img)
            c.create_image(dx, day_y, image=tk_day, anchor="nw")
            c.create_text(dx + chip_w // 2, day_y + chip_h // 2, text=dl,
                          font=self.font_day_lg, fill=dfg, anchor="center")
            self._click_zones.append(
                (dx, day_y, dx + chip_w, day_y + chip_h,
                 lambda idx=i: self._te_toggle_day(idx)))
            dx += chip_w + chip_gap


    def _te_select(self, idx):
        if 0 <= idx < len(self._te_times):
            self._te_sel = idx
            self._draw_frame()

    def _te_adj(self, idx, field, delta):
        if idx >= len(self._te_times):
            return
        h, m = self._te_times[idx]
        if field == "h":
            h = (h + delta) % 24
        else:
            m = (m + delta) % 60
        self._te_times[idx] = [h, m]
        self._draw_frame()

    def _te_add(self):
        if len(self._te_times) < 3:
            last = self._te_times[-1]
            self._te_times.append([(last[0] + 6) % 24, last[1]])
            self._te_sel = len(self._te_times) - 1
            self._draw_frame()

    def _te_remove(self, idx):
        if len(self._te_times) > 1 and idx < len(self._te_times):
            self._te_times.pop(idx)
            self._te_sel = max(0, min(self._te_sel, len(self._te_times) - 1))
            self._draw_frame()

    def _te_commit(self):
        times = sorted(self._te_times,
                       key=lambda hm: hm[0] * 60 + hm[1])
        time_strs = [_fmt_time12(h, m) for h, m in times]
        if self._te_target == "draft":
            self._draft["dose_times"] = time_strs
            self._draft["times_per_day"] = len(time_strs)
        else:
            md = self.med_data[self.selected_pill]
            md["dose_times"] = time_strs
            md["times_per_day"] = len(time_strs)
            md["schedule_time"] = time_strs[0]
            self._save_med()

    def _te_done(self):
        self._te_commit()
        self.mode = self._te_return
        self._draw_frame()

    # ══════════════════════════════════════════════════════════════════════
    #  SETTINGS SCREEN
    # ══════════════════════════════════════════════════════════════════════
    def _draw_settings(self, c):
        t = self.theme

        # Card fills content area edge-to-edge vertically
        card_y = 20
        card_h = 440
        card_img = _pil_rounded_rect(620, card_h, 22, t["card_bg"])
        tk_card = self._get_tk_image("settings_card", card_img)
        c.create_image(26, card_y, image=tk_card, anchor="nw")

        items = [
            ("Day / Night Mode", 0, "toggle", "night_mode",
             self.settings.get("night_mode", False)),
            ("Alarm Sound", 1, "toggle", "alarm_sound",
             self.settings.get("alarm_sound", True)),
            ("Check for Updates", 2, "button", "update", None),
            ("Constant QR Scan", 3, "toggle", "constant_scan",
             self.settings.get("constant_scan", False)),
        ]

        row_h = card_h // 4
        for label, idx, kind, key, val in items:
            y = card_y + idx * row_h
            px = 52

            if idx < 3:
                c.create_line(px, y + row_h, 622, y + row_h,
                              fill=t["divider"])

            # Bigger icon (48px)
            icon_img = _pil_settings_icon(48, SETTINGS_ICON_COLORS[idx])
            tk_icon = self._get_tk_image(f"set_icon_{idx}", icon_img)
            c.create_image(px, y + (row_h - 48) // 2, image=tk_icon, anchor="nw")

            c.create_text(px + 64, y + row_h // 2 - 4, text=label,
                          font=self.font_settings, fill=t["fg"], anchor="w")

            if kind == "toggle":
                tw, th = 64, 34
                tx = 568
                ty = y + (row_h - th) // 2
                tog_img = _pil_toggle(tw, th, val, t)
                tk_tog = self._get_tk_image(f"toggle_{key}", tog_img)
                c.create_image(tx, ty, image=tk_tog, anchor="nw")

                self._click_zones.append(
                    (tx, ty, tx + tw, ty + th,
                     lambda k=key: self._toggle_setting(k)))

            elif kind == "button":
                bw, bh = 120, 44
                bx = 510
                by = y + (row_h - bh) // 2
                btn_img = _pil_rounded_rect(bw, bh, 14, ACCENT_BLUE)
                tk_btn = self._get_tk_image("update_btn", btn_img)
                c.create_image(bx, by, image=tk_btn, anchor="nw")
                c.create_text(bx + bw // 2, by + bh // 2, text="UPDATE",
                              font=self.font_update_btn, fill="#FFFFFF",
                              anchor="center")

                status = getattr(self, '_update_status_text', '')
                if status:
                    c.create_text(px + 64, y + row_h // 2 + 16, text=status,
                                  font=self.font_small, fill=t["muted"],
                                  anchor="w")

                self._click_zones.append(
                    (bx, by, bx + bw, by + bh,
                     self._on_update_pressed))

        # Secret camera debug — tap bottom-right corner of card 5 times
        self._click_zones.append(
            (500, 400, 646, 460, self._secret_cam_tap))

    def _secret_cam_tap(self):
        now = time.time()
        if now - self._settings_tap_time > 3.0:
            self._settings_tap_count = 0
        self._settings_tap_time = now
        self._settings_tap_count += 1
        if self._settings_tap_count >= 5:
            self._settings_tap_count = 0
            self._camera_view = True
            self._prev_mode = self.mode
            self.mode = "camview"
            self._draw_frame()

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
    #  CAMERA DEBUG VIEW (secret)
    # ══════════════════════════════════════════════════════════════════════
    def _draw_camview(self, c):
        t = self.theme

        c.create_text(32, 16, text="CAMERA VIEW", font=self.font_label,
                      fill=t["muted"], anchor="nw")

        # Close button
        close_img = _pil_rounded_rect(80, 32, 10, t["elevated_bg"])
        tk_close = self._get_tk_image("cam_close", close_img)
        c.create_image(560, 12, image=tk_close, anchor="nw")
        c.create_text(600, 28, text="CLOSE", font=self.font_small_bold,
                      fill=t["fg"], anchor="center")
        self._click_zones.append(
            (560, 12, 640, 44, self._close_camview))

        frame = self._camera_frame
        qr_results = self._camera_qr_results

        if frame is None:
            c.create_text(CONTENT_W // 2, 240,
                          text="No camera feed" if not CAMERA_AVAILABLE else "Waiting for frame...",
                          font=self.font_body, fill=t["muted"], anchor="center")
            return

        # Scale camera frame to fit content area
        view_w = CONTENT_W - 40
        view_h = SCREEN_H - 100
        fw, fh = frame.size
        scale = min(view_w / fw, view_h / fh)
        disp_w = int(fw * scale)
        disp_h = int(fh * scale)

        # Draw QR bounding boxes onto the frame
        annotated = frame.copy()
        draw = ImageDraw.Draw(annotated)
        for r in qr_results:
            if r.rect:
                rx, ry, rw, rh = r.rect.left, r.rect.top, r.rect.width, r.rect.height
                text = r.data.decode("utf-8", errors="ignore").strip()

                # Determine color based on slot
                parsed = self._parse_qr_payload(text)
                if parsed:
                    slot = parsed[0]
                    box_color = _hex_to_rgb(SLOT_COLORS.get(slot, "#C084FC"))
                else:
                    box_color = (192, 132, 252)

                draw.rectangle([rx, ry, rx + rw, ry + rh],
                               outline=box_color + (255,), width=4)

                # Label
                label = parsed[0].upper() if parsed else "UNKNOWN"
                draw.rectangle([rx, ry - 28, rx + len(label) * 14 + 10, ry],
                               fill=box_color + (200,))
                draw.text((rx + 5, ry - 26), label, fill=(255, 255, 255, 255))

        resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
        resized = annotated.resize((disp_w, disp_h), resample)
        tk_frame = self._get_tk_image("cam_frame", resized)
        vx = 20 + (view_w - disp_w) // 2
        vy = 50 + (view_h - disp_h) // 2
        c.create_image(vx, vy, image=tk_frame, anchor="nw")

        # Status bar at bottom
        num_qr = len(qr_results)
        status_text = f"{num_qr} QR code{'s' if num_qr != 1 else ''} detected"
        status_color = "#30D158" if num_qr > 0 else "#FF6B6B"
        c.create_text(32, SCREEN_H - 16, text=status_text,
                      font=self.font_small_bold, fill=status_color, anchor="sw")

        # List detected codes
        if qr_results:
            info_parts = []
            for r in qr_results:
                text = r.data.decode("utf-8", errors="ignore").strip()
                parsed = self._parse_qr_payload(text)
                if parsed:
                    slot, med = parsed
                    known = "KNOWN" if slot in KNOWN_SLOTS else "NEW"
                    info_parts.append(f"{slot}: {med} [{known}]")
                else:
                    info_parts.append(f"?: {text[:30]}")
            c.create_text(200, SCREEN_H - 16, text="  |  ".join(info_parts),
                          font=self.font_small, fill=t["muted"], anchor="sw")

    def _close_camview(self):
        self._camera_view = False
        self.mode = self._prev_mode
        self._draw_frame()

    # ══════════════════════════════════════════════════════════════════════
    #  USER / ADHERENCE SCREEN
    # ══════════════════════════════════════════════════════════════════════
    def _classify_dose(self, md, taken_dt):
        """Classify a dispense against the nearest scheduled dose time.
        Returns (status, sched_iso).  Statuses:
          on_time    — within 15 min before to 60 min after schedule
          late       — more than 60 min after schedule
          early      — before the window but same part of day (okay)
          very_early — a whole part of day ahead of schedule
        """
        self._ensure_dose_times(md)
        best, best_diff = None, None
        for ts in md["dose_times"]:
            h, m = _parse_time12(ts)
            sched = taken_dt.replace(hour=h, minute=m,
                                     second=0, microsecond=0)
            diff = (taken_dt - sched).total_seconds() / 60
            if best is None or abs(diff) < abs(best_diff):
                best, best_diff = sched, diff
        if best is None:
            return "on_time", ""
        if -15 <= best_diff <= 60:
            status = "on_time"
        elif best_diff > 60:
            status = "late"
        elif _day_part(taken_dt.hour) == _day_part(best.hour):
            status = "early"
        else:
            status = "very_early"
        return status, best.isoformat()

    def _adherence_stats(self):
        """Counts + battery score.  Weights: on-time 1.0, early 0.8,
        very-early 0.5, late 0.5, missed 0."""
        events = self.adherence.get("events", [])
        counts = {"on_time": 0, "late": 0, "early": 0, "very_early": 0}
        weight = {"on_time": 1.0, "early": 0.8, "very_early": 0.5,
                  "late": 0.5}
        earned = 0.0
        for ev in events:
            status = ev.get("status")
            if status not in weight:
                # Legacy event without a status — classify it now
                try:
                    taken = datetime.fromisoformat(ev.get("time", ""))
                    md = self.med_data.get(ev.get("key"))
                    status = self._classify_dose(md, taken)[0] if md \
                        else "on_time"
                except Exception:
                    status = "on_time"
            counts[status] += 1
            earned += weight[status]

        # Missed = scheduled doses (since tracking began, last 30 days)
        # that passed more than 12h ago with no dispense within the window
        now = datetime.now()
        missed = 0
        for key, md in self.med_data.items():
            if not md.get("loaded"):
                continue
            self._ensure_dose_times(md)
            try:
                since = datetime.fromisoformat(md["tracking_since"])
            except Exception:
                since = now
            start_day = max(since.date(),
                            (now - timedelta(days=30)).date())
            day = start_day
            while day <= now.date():
                dt_day = datetime(day.year, day.month, day.day)
                if dt_day.strftime("%a") in md.get("schedule_days", ALL_DAYS):
                    for ts in md["dose_times"]:
                        h, m = _parse_time12(ts)
                        sched = dt_day.replace(hour=h, minute=m)
                        if sched < since or (now - sched).total_seconds() < 12 * 3600:
                            continue
                        hit = False
                        for ev in events:
                            if ev.get("key") != key:
                                continue
                            try:
                                tdt = datetime.fromisoformat(ev["time"])
                            except Exception:
                                continue
                            if abs((tdt - sched).total_seconds()) <= 12 * 3600:
                                hit = True
                                break
                        if not hit:
                            missed += 1
                day = day + timedelta(days=1)

        denom = len(events) + missed
        score = 100 if denom == 0 else int(round(100 * earned / denom))
        return {
            "total": len(events),
            "on_time": counts["on_time"],
            "late": counts["late"],
            "early": counts["early"] + counts["very_early"],
            "missed": missed,
            "score": score,
        }

    def _draw_user(self, c):
        t = self.theme

        card_img = _pil_rounded_rect(620, 440, 22, t["card_bg"])
        tk_card = self._get_tk_image("user_card", card_img)
        c.create_image(26, 20, image=tk_card, anchor="nw")

        c.create_text(56, 52, text="YOUR ADHERENCE",
                      font=self.font_label, fill=t["muted"], anchor="nw")

        stats = self._adherence_stats()

        # Five count columns
        cols = [
            ("TOTAL", stats["total"], t["fg"]),
            ("ON TIME", stats["on_time"], "#30D158"),
            ("LATE", stats["late"], "#FFD60A"),
            ("EARLY", stats["early"], "#FF9F43"),
            ("MISSED", stats["missed"], "#FF6B6B"),
        ]
        tile_w, tile_h, gap = 104, 168, 10
        tx, ty = 56, 84
        for i, (label, val, color) in enumerate(cols):
            x = tx + i * (tile_w + gap)
            tile_img = _pil_rounded_rect(tile_w, tile_h, 16, t["elevated_bg"])
            tk_tile = self._get_tk_image(f"user_tile_{i}", tile_img)
            c.create_image(x, ty, image=tk_tile, anchor="nw")
            c.create_text(x + tile_w // 2, ty + 72, text=str(val),
                          font=self.font_hold_big, fill=color,
                          anchor="center")
            c.create_text(x + tile_w // 2, ty + 132, text=label,
                          font=self.font_label, fill=t["muted"],
                          anchor="center")

        # Adherence score — big percentage + slim progress bar
        score = stats["score"]
        c.create_text(56, 288, text="ADHERENCE SCORE",
                      font=self.font_label, fill=t["muted"], anchor="nw")
        score_color = ("#30D158" if score >= 80
                       else "#FFD60A" if score >= 50 else "#FF6B6B")
        c.create_text(56, 366, text=f"{score}%",
                      font=self.font_pct, fill=score_color, anchor="w")

        bar_x, bar_w, bar_h = 240, 376, 14
        bar_y = 366 - bar_h // 2
        track_img = _pil_rounded_rect(bar_w, bar_h, bar_h // 2,
                                      t["elevated_bg"])
        tk_track = self._get_tk_image("user_score_track", track_img)
        c.create_image(bar_x, bar_y, image=tk_track, anchor="nw")
        fill_w = int(bar_w * score / 100)
        if fill_w >= bar_h:
            fill_img = _pil_rounded_rect(fill_w, bar_h, bar_h // 2,
                                         score_color)
            tk_fill = self._get_tk_image("user_score_fill", fill_img)
            c.create_image(bar_x, bar_y, image=tk_fill, anchor="nw")

    # ══════════════════════════════════════════════════════════════════════
    #  ADD MEDICATION SCREEN (triggered by new/demo QR)
    # ══════════════════════════════════════════════════════════════════════
    def _draw_addmed(self, c):
        t = self.theme
        draft = self._draft

        slot_color = SLOT_COLORS.get(self._draft_slot, "#C084FC")

        card_img = _pil_rounded_rect(620, 440, 22, t["card_bg"])
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
        c.create_text(pad + 14, 92,
                      text=self._fit_text(name_text, self.font_title, 470),
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

        # SCHEDULE tile — words + clock button that opens the big editor
        sch_img = _pil_rounded_rect(270, 80, 16, t["elevated_bg"])
        tk_sch = self._get_tk_image("addmed_sched", sch_img)
        c.create_image(pad + 290, qy, image=tk_sch, anchor="nw")

        c.create_text(pad + 425, qy + 12, text="SCHEDULE",
                      font=self.font_tiny, fill=t["muted"], anchor="center")
        dts = self._draft_dose_times()
        times_txt = " & ".join(dts) if len(dts) <= 2 else \
            ", ".join(dts[:-1]) + " & " + dts[-1]
        c.create_text(pad + 415, qy + 48, text=times_txt,
                      font=self.font_medium, fill=t["fg"], anchor="center",
                      width=190)

        clk_bg = _pil_rounded_rect(40, 40, 12, t["card_bg"])
        tk_clkb = self._get_tk_image("addmed_clk_bg", clk_bg)
        c.create_image(pad + 500, qy + 28, image=tk_clkb, anchor="nw")
        clk_icon = _pil_clock_icon(22, t["fg"])
        tk_clki = self._get_tk_image("addmed_clk", clk_icon)
        c.create_image(pad + 509, qy + 37, image=tk_clki, anchor="nw")
        self._click_zones.append(
            (pad + 290, qy, pad + 560, qy + 80,
             lambda: self._open_time_edit("draft")))


        # ADD MEDICATION button
        btn_y = 258
        btn_img = _pil_rounded_rect(560, 52, 16, ACCENT_BLUE)
        tk_btn = self._get_tk_image("addmed_submit", btn_img)
        c.create_image(pad, btn_y, image=tk_btn, anchor="nw")
        c.create_text(pad + 280, btn_y + 26, text="ADD MEDICATION",
                      font=self.font_btn_lg, fill="#FFFFFF", anchor="center")
        self._click_zones.append(
            (pad, btn_y, pad + 560, btn_y + 52, self._submit_add_med))

        # Cancel — kept well below the ADD button so it can't be mis-tapped
        cancel_y = btn_y + 92
        c.create_text(pad + 280, cancel_y, text="Cancel",
                      font=self.font_body, fill=t["muted"], anchor="center")
        self._click_zones.append(
            (pad + 200, cancel_y - 18, pad + 360, cancel_y + 18,
             self._cancel_add_med))

    # ── On-screen keyboard ─────────────────────────────────────────────────
    def _kbd_img(self, key, pil_img):
        tk_img = ImageTk.PhotoImage(pil_img)
        self._kbd_imgs[key] = tk_img
        return tk_img

    def _show_keyboard(self):
        if getattr(self, '_kbd_overlay', None):
            return
        self._kbd_text = self._draft["name"]
        self._kbd_shift = True
        t = self.theme
        kbd_h = 280
        # The overlay canvas is created ONCE and redrawn in place — the old
        # destroy/recreate cycle flashed black between frames on the Pi
        self._kbd_imgs = {}
        self._kbd_overlay = tk.Canvas(self.root, width=CONTENT_W,
                                      height=kbd_h,
                                      highlightthickness=0,
                                      bg=t["bg"])
        self._kbd_overlay.place(x=0, y=SCREEN_H - kbd_h,
                                width=CONTENT_W, height=kbd_h)
        self._raise_widget(self._kbd_overlay)
        self._kbd_overlay.bind("<Button-1>", self._on_kbd_click)
        self._draw_keyboard()

    def _hide_keyboard(self, save=False):
        if getattr(self, '_kbd_overlay', None):
            if save:
                self._draft["name"] = self._kbd_text
            self._kbd_overlay.destroy()
            self._kbd_overlay = None
            self._kbd_imgs = {}

    def _draw_keyboard(self):
        oc = self._kbd_overlay
        if not oc:
            return
        t = self.theme
        oc.delete("all")

        # Text display
        disp_img = _pil_rounded_rect(CONTENT_W - 32, 44, 12, t["elevated_bg"])
        tk_disp = self._kbd_img("kbd_disp", disp_img)
        oc.create_image(16, 8, image=tk_disp, anchor="nw")
        display_text = self._kbd_text or "Type medication name..."
        display_color = t["fg"] if self._kbd_text else t["muted"]
        display_text = self._fit_text(display_text, self.font_title,
                                      CONTENT_W - 160)
        oc.create_text(30, 30, text=display_text,
                       font=self.font_title, fill=display_color, anchor="w")

        # Done button
        done_img = _pil_rounded_rect(80, 36, 10, ACCENT_BLUE)
        tk_done = self._kbd_img("kbd_done", done_img)
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
                tk_key = self._kbd_img("kbd_space", key_img)
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
                tk_key = self._kbd_img(f"kbd_{row_idx}_{ki}", key_img)
                oc.create_image(kx, row_y, image=tk_key, anchor="nw")

                display = key_label
                if not is_special:
                    display = key_label if self._kbd_shift else key_label.lower()
                font = self.font_kbd_special if is_special else self.font_kbd
                oc.create_text(kx + kw // 2, row_y + key_h // 2,
                               text=display, font=font,
                               fill=t["fg"], anchor="center")

                kx += kw + key_gap

    def _on_kbd_click(self, event):
        x, y = event.x, event.y
        t = self.theme

        # Check Done button
        if CONTENT_W - 96 <= x <= CONTENT_W - 16 and 12 <= y <= 48:
            self._hide_keyboard(save=True)
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

    def _draft_dose_times(self):
        if not self._draft.get("dose_times"):
            self._draft["dose_times"] = [
                TIME_PRESETS[i] if 0 <= i < len(TIME_PRESETS) else "8:00 AM"
                for i in self._draft.get("doses", [2])] or ["8:00 AM"]
        return self._draft["dose_times"]

    def _cancel_add_med(self):
        # 30s cooldown so the still-visible QR doesn't reopen the popup
        self._addmed_cancel_time = time.time()
        self._nav("home")

    def _submit_add_med(self):
        self._hide_keyboard(save=True)
        name = self._draft["name"].strip() or "New Medication"
        slot = getattr(self, '_draft_slot', 'demo')
        md = self.med_data[slot]
        md["name"] = name
        md["count"] = self._draft["qty"]
        md["loaded"] = True
        md["doses"] = self._draft["doses"][:]
        md["times_per_day"] = self._draft["times_per_day"]
        md["dose_times"] = list(self._draft_dose_times())
        md["schedule_time"] = md["dose_times"][0]
        md["tracking_since"] = datetime.now().isoformat()
        day_map = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu",
                   4: "Fri", 5: "Sat", 6: "Sun"}
        md["schedule_days"] = [day_map[i] for i in range(7)
                               if self._draft["days"][i]]
        if slot == "demo":
            self._demo_registered = True
        self._save_med()
        self._draft = {"name": "", "times_per_day": 1, "doses": [2],
                       "dose_times": ["8:00 AM"],
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
        cx = CONTENT_W // 2

        elapsed = time.time() - self.hold_start if self.hold_start > 0 else 0
        progress = min(elapsed / HOLD_TIME, 1.0) if self.hold_start > 0 else 0

        ring_size = 200
        ring_img = _pil_stroke_ring(ring_size, progress)
        tk_ring = self._get_tk_image("hold_ring", ring_img)
        ring_x = cx - ring_size // 2
        ring_y = 90
        self._hold_ring_id = c.create_image(ring_x, ring_y, image=tk_ring,
                                            anchor="nw")

        secs = max(1, math.ceil(HOLD_TIME - elapsed)) if self.hold_start > 0 \
            else int(HOLD_TIME)
        self._hold_secs_id = c.create_text(
            cx, ring_y + ring_size // 2, text=f"{secs}s",
            font=self.font_hold_big, fill=DOSE_BLUE_LT, anchor="center")

        c.create_text(cx, 332, text="Hold to Confirm You Want to Dispense",
                      font=self.font_name, fill=t["fg"], anchor="center")
        c.create_text(cx, 362,
                      text=self._fit_text(f"1 pill · {md['name']}",
                                          self.font_body, 560),
                      font=self.font_body, fill=t["muted"], anchor="center")

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
        self.spin_start = 0
        if self.hold_after_id:
            self.root.after_cancel(self.hold_after_id)
            self.hold_after_id = None
        if self.spin_after_id:
            self.root.after_cancel(self.spin_after_id)
            self.spin_after_id = None
        self.mode = self._prev_mode
        self._draw_frame()

    def _end_dispense(self):
        self.dispense_state = 0
        self.dispense_pill = None
        self.hold_start = 0
        self.spin_start = 0
        if self.hold_after_id:
            self.root.after_cancel(self.hold_after_id)
            self.hold_after_id = None
        if self.spin_after_id:
            self.root.after_cancel(self.spin_after_id)
            self.spin_after_id = None
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

        if elapsed >= HOLD_TIME:
            self.hold_start = 0
            self._start_spin()
            return

        # Partial update: only swap the ring image + countdown text
        # (full-screen redraws every frame cause visible stutter)
        progress = min(elapsed / HOLD_TIME, 1.0)
        ring_img = _pil_stroke_ring(200, progress)
        tk_ring = self._get_tk_image("hold_ring", ring_img)
        self.canvas.itemconfig(self._hold_ring_id, image=tk_ring)
        secs = max(1, math.ceil(HOLD_TIME - elapsed))
        self.canvas.itemconfig(self._hold_secs_id, text=f"{secs}s")

        self.hold_after_id = self.root.after(25, self._hold_update)

    # ── SPIN TO DISPENSE stage — user manually spins the spindle ──────────
    def _start_spin(self):
        self.mode = "spin"
        self.spin_start = time.time()
        self._draw_frame()
        self._spin_update()

    def _spin_update(self):
        if self.mode != "spin":
            return
        elapsed = time.time() - self.spin_start
        if elapsed >= SPIN_TIME:
            self.spin_after_id = None
            self.mode = "confirmdisp"
            self._draw_frame()
            return
        # Partial update: rotate only the dial image for a smooth animation
        angle = (elapsed * 200) % 360
        spin_img = _pil_spinner(200, angle)
        tk_spin = self._get_tk_image("spin_ring", spin_img)
        self.canvas.itemconfig(self._spin_ring_id, image=tk_spin)
        self.spin_after_id = self.root.after(25, self._spin_update)

    def _draw_spin(self, c):
        t = self.theme
        md = self.med_data[self.dispense_pill]
        cx = CONTENT_W // 2

        elapsed = time.time() - self.spin_start
        angle = (elapsed * 200) % 360  # smooth continuous rotation

        ring_size = 200
        spin_img = _pil_spinner(ring_size, angle)
        tk_spin = self._get_tk_image("spin_ring", spin_img)
        ring_x = cx - ring_size // 2
        ring_y = 90
        self._spin_ring_id = c.create_image(ring_x, ring_y, image=tk_spin,
                                            anchor="nw")

        c.create_text(cx, 332, text="Spin the Spindle",
                      font=self.font_name, fill=t["fg"], anchor="center")
        c.create_text(cx, 362,
                      text=self._fit_text(f"1 pill · {md['name']}",
                                          self.font_body, 560),
                      font=self.font_body, fill=t["muted"], anchor="center")

        cancel_w, cancel_h = 160, 52
        cancel_img = _pil_rounded_rect(cancel_w, cancel_h, 16, t["elevated_bg"])
        tk_cancel = self._get_tk_image("spin_cancel", cancel_img)
        cancel_x = cx - cancel_w // 2
        cancel_y = 388
        c.create_image(cancel_x, cancel_y, image=tk_cancel, anchor="nw")
        c.create_text(cx, cancel_y + cancel_h // 2, text="Cancel",
                      font=self.font_body_bold, fill=t["fg"], anchor="center")
        self._click_zones.append(
            (cancel_x, cancel_y, cancel_x + cancel_w, cancel_y + cancel_h,
             self._cancel_hold))

    # ── Final confirmation after the spindle has been spun ────────────────
    def _draw_confirm_dispense(self, c):
        t = self.theme
        md = self.med_data[self.dispense_pill]
        cx = CONTENT_W // 2

        c.create_text(cx, 118, text="Dispense 1 pill?",
                      font=self.font_name_lg, fill=t["fg"], anchor="center")
        c.create_text(cx, 156,
                      text=self._fit_text(md["name"], self.font_name, 560),
                      font=self.font_name, fill=DOSE_BLUE, anchor="center")

        # Pill-shaped blue button, dark navy text — same as dose.html
        btn_w, btn_h = 300, 84
        btn_img = _pil_rounded_rect(btn_w, btn_h, btn_h // 2, DOSE_BLUE)
        tk_btn = self._get_tk_image("confirm_btn", btn_img)
        btn_x = cx - btn_w // 2
        btn_y = 214
        c.create_image(btn_x, btn_y, image=tk_btn, anchor="nw")
        c.create_text(cx, btn_y + btn_h // 2, text="Confirm",
                      font=self.font_name_lg, fill="#06101E", anchor="center")
        self._click_zones.append(
            (btn_x, btn_y, btn_x + btn_w, btn_y + btn_h,
             self._confirm_dispense))

        cancel_w, cancel_h = 160, 52
        cancel_img = _pil_rounded_rect(cancel_w, cancel_h, 16, t["elevated_bg"])
        tk_cancel = self._get_tk_image("confirm_cancel", cancel_img)
        cancel_x = cx - cancel_w // 2
        cancel_y = 388
        c.create_image(cancel_x, cancel_y, image=tk_cancel, anchor="nw")
        c.create_text(cx, cancel_y + cancel_h // 2, text="Cancel",
                      font=self.font_body_bold, fill=t["fg"], anchor="center")
        self._click_zones.append(
            (cancel_x, cancel_y, cancel_x + cancel_w, cancel_y + cancel_h,
             self._cancel_hold))

    def _confirm_dispense(self):
        key = self.dispense_pill
        md = self.med_data[key]
        if md["count"] > 0:
            md["count"] -= 1
        self._save_med()

        # Log adherence event, classified against the nearest scheduled dose
        now = datetime.now()
        status, sched_iso = self._classify_dose(md, now)
        self.adherence.setdefault("events", []).append({
            "key": key, "name": md["name"],
            "time": now.isoformat(),
            "sched": sched_iso, "status": status,
        })
        _save_adherence_log(self.adherence)

        self.dispense_state = 4
        self._play_sound()
        self.mode = "dispensed"
        self._draw_frame()
        self.root.after(int(DISPENSED_TIME * 1000), self._end_dispense)

    def _draw_dispensed(self, c):
        t = self.theme
        md = self.med_data[self.dispense_pill]
        cx = CONTENT_W // 2

        check_size = 130
        check_img = _pil_soft_check(check_size)
        tk_check = self._get_tk_image("dispensed_check", check_img)
        c.create_image(cx - check_size // 2, 96, image=tk_check, anchor="nw")

        c.create_text(cx, 282,
                      text=self._fit_text(f"Dispensed · {md['name']}",
                                          self.font_name, 560),
                      font=self.font_name, fill=t["fg"], anchor="center")
        try:
            logged = datetime.now().strftime("%-I:%M %p")
        except ValueError:
            logged = datetime.now().strftime("%I:%M %p").lstrip("0")
        c.create_text(cx, 314, text=f"Logged {logged}",
                      font=self.font_body, fill=t["muted"], anchor="center")

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

        # close X, iOS-style, top-right of the card
        x_img = _pil_status_icon(30, "missed")
        tk_x = self._get_tk_image("qty_close", x_img)
        c.create_image(56 + 560 - 44, 84, image=tk_x, anchor="nw")
        self._click_zones.append(
            (56 + 560 - 52, 76, 56 + 560 - 6, 122, self._qty_cancel))

    def _qty_adjust(self, delta):
        self._qty_value = max(1, self._qty_value + delta)
        self._draw_frame()

    def _qty_cancel(self):
        self._qty_cancel_time = time.time()
        self.mode = self._prev_mode
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
        self._check_due_doses()
        if self.mode in ("home", "storage"):
            self._draw_frame()
        self.root.after(1000, self._tick_clock)

    # ── Dose-time notification ─────────────────────────────────────────────
    def _dose_status(self, key, time_str):
        """Status of one scheduled dose today: 'taken' if dispensed in
        its window (15 min early to 60 min late), 'missed' once the
        window has passed without a proper dispense, None if upcoming."""
        now = datetime.now()
        h, m = _parse_time12(time_str)
        sched = now.replace(hour=h, minute=m, second=0, microsecond=0)
        for ev in self.adherence.get("events", []):
            if ev.get("key") != key:
                continue
            try:
                tdt = datetime.fromisoformat(ev["time"])
            except Exception:
                continue
            if tdt.date() != now.date():
                continue
            delta = (tdt - sched).total_seconds() / 60
            if -15 <= delta <= 60:
                return "taken"
        if (now - sched).total_seconds() / 60 > 60:
            return "missed"
        return None

    def _dose_due_map(self):
        """Doses due right now: scheduled time has arrived (up to 60 min
        ago) today and no dispense has been logged for it yet."""
        now = datetime.now()
        events = self.adherence.get("events", [])
        due = {}
        for key, md in self.med_data.items():
            if not md.get("loaded") or md.get("count", 0) <= 0:
                continue
            if now.strftime("%a") not in md.get("schedule_days", ALL_DAYS):
                continue
            self._ensure_dose_times(md)
            for ts in md["dose_times"]:
                h, m = _parse_time12(ts)
                sched = now.replace(hour=h, minute=m,
                                    second=0, microsecond=0)
                mins = (now - sched).total_seconds() / 60
                if not (0 <= mins <= 60):
                    continue
                taken = False
                for ev in events:
                    if ev.get("key") != key:
                        continue
                    try:
                        tdt = datetime.fromisoformat(ev["time"])
                    except Exception:
                        continue
                    if -15 * 60 <= (tdt - sched).total_seconds() <= 3600:
                        taken = True
                        break
                if not taken:
                    due[key] = ts
        return due

    def _check_due_doses(self):
        due = self._dose_due_map()
        newly_due = set(due) - self._due_prev
        cleared = self._due_prev - set(due)
        if newly_due:
            self._play_sound()  # chime the moment a dose becomes due
            self._banner_dismissed = False
        self._due_keys = due
        self._due_prev = set(due)

        # Full-screen takeover: holds until the user dispenses or
        # dismisses (banner keeps reminding after a dismiss)
        if (newly_due and self.dispense_state == 0
                and self.mode in ("home", "storage", "settings", "user")):
            self._alert_key = sorted(newly_due)[0]
            self._alert_return = self.mode
            self.mode = "dosealert"
            self._draw_frame()
            return

        # if the due dose was taken while the alert is up, close it
        if self.mode == "dosealert" and self._alert_key not in due:
            self.mode = self._alert_return
            self._draw_frame()
            return

        # settings/user don't redraw every second — refresh them when the
        # banner appears or clears
        if (newly_due or cleared) and self.mode in ("settings", "user"):
            self._draw_frame()

    def _draw_dose_alert(self, c):
        t = self.theme
        key = self._alert_key
        md = self.med_data.get(key, {})
        name = md.get("name", "Medication")
        cx = CONTENT_W // 2

        card_img = _pil_rounded_rect(620, 440, 22, t["card_bg"],
                                     outline=DOSE_BLUE, outline_w=4)
        tk_card = self._get_tk_image("alert_card", card_img)
        c.create_image(26, 20, image=tk_card, anchor="nw")

        icon = _pil_clock_icon(72, DOSE_BLUE)
        tk_icon = self._get_tk_image("alert_icon", icon)
        c.create_image(cx - 36, 56, image=tk_icon, anchor="nw")

        c.create_text(cx, 168, text="TIME TO TAKE YOUR MEDICATION",
                      font=self.font_label, fill=t["muted"],
                      anchor="center")
        c.create_text(cx, 216,
                      text=self._fit_text(name, self.font_pct, 560),
                      font=self.font_pct, fill=DOSE_BLUE, anchor="center")
        sched = self._due_keys.get(key, "")
        extra = len(self._due_keys) - 1
        sub = f"Scheduled for {sched}" if sched else ""
        if extra > 0:
            sub += f"  ·  +{extra} more due"
        if sub:
            c.create_text(cx, 262, text=sub, font=self.font_body,
                          fill=t["muted"], anchor="center")

        btn_w, btn_h = 320, 72
        btn_img = _pil_rounded_rect(btn_w, btn_h, btn_h // 2, DOSE_BLUE)
        tk_btn = self._get_tk_image("alert_btn", btn_img)
        btn_x, btn_y = cx - btn_w // 2, 300
        c.create_image(btn_x, btn_y, image=tk_btn, anchor="nw")
        c.create_text(cx, btn_y + btn_h // 2, text="DISPENSE NOW",
                      font=self.font_btn_lg, fill="#06101E",
                      anchor="center")
        self._click_zones.append(
            (btn_x, btn_y, btn_x + btn_w, btn_y + btn_h,
             self._alert_dispense))

        c.create_text(cx, 412, text="Not now",
                      font=self.font_body, fill=t["muted"],
                      anchor="center")
        self._click_zones.append(
            (cx - 80, 394, cx + 80, 430, self._alert_dismiss))

    def _alert_dispense(self):
        self.mode = self._alert_return
        self._start_dispense(self._alert_key)

    def _alert_dismiss(self):
        self.mode = self._alert_return
        self._draw_frame()

    def _draw_due_banner(self, c):
        t = self.theme
        names = [self.med_data[k]["name"] for k in self._due_keys
                 if k in self.med_data]
        if not names:
            return
        if len(names) == 1:
            msg = f"Time to take {names[0]}"
        else:
            msg = f"Time to take {len(names)} medications"
        bw, bh = 620, 56
        bx, by = 26, 20
        bar_img = _pil_rounded_rect(bw, bh, 16, DOSE_BLUE)
        tk_bar = self._get_tk_image("due_banner", bar_img)
        c.create_image(bx, by, image=tk_bar, anchor="nw")
        icon = _pil_clock_icon(24, "#06101E")
        tk_icon = self._get_tk_image("due_banner_icon", icon)
        c.create_image(bx + 18, by + bh // 2 - 12, image=tk_icon,
                       anchor="nw")
        c.create_text(bx + 56, by + bh // 2,
                      text=self._fit_text(msg, self.font_body_bold, 500),
                      font=self.font_body_bold, fill="#06101E", anchor="w")
        c.create_text(bx + bw - 26, by + bh // 2, text="✕",
                      font=self.font_body_bold, fill="#06101E",
                      anchor="center")
        self._click_zones.append((bx, by, bx + bw, by + bh,
                                  self._dismiss_banner))

    def _dismiss_banner(self):
        self._banner_dismissed = True
        self._draw_frame()

    def _fit_text(self, text, font, max_w):
        """Truncate text with an ellipsis so it never clips its container."""
        try:
            if font.measure(text) <= max_w:
                return text
            while text and font.measure(text + "…") > max_w:
                text = text[:-1]
            return text + "…"
        except Exception:
            return text

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
        # Bottle animations only play over home/storage — never on top of
        # the editor, keyboard, dispense flow, or alerts
        if self.mode not in ("home", "storage"):
            self._anim_queue.clear()
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
        if silent:
            # Never auto-overwrite on launch: GitHub's raw CDN can serve a
            # stale file for ~5 min, which would "update" BACKWARDS and
            # revert newer local code. Just announce it — applying stays
            # one tap away on the UPDATE button.
            self.root.after(0, self._update_result,
                            "Update available — press UPDATE")
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
                main={"size": (1920, 1080), "format": "RGB888"})
            self.camera.configure(config)
            self.camera.start()
            try:
                # True auto-exposure. Setting ExposureTime/AnalogueGain
                # silently disables AE in picamera2 — the old fixed 30ms
                # exposure blew glossy stickers into pure white glare.
                self.camera.set_controls({
                    "AfMode": 2,
                    "AeEnable": True,
                    "AwbEnable": True,
                    # Bias AE slightly dark: with the internal light the
                    # failure mode is clipped highlights, and QR codes
                    # decode far better underexposed than blown out
                    "ExposureValue": -0.5,
                })
            except Exception:
                try:
                    self.camera.set_controls({"AfMode": 2})
                except Exception:
                    pass
            self.camera_running = True
            threading.Thread(target=self._camera_loop, daemon=True).start()
        except Exception:
            self.camera = None
            self.camera_running = False

    def _decode_passes(self, pil_img):
        """Run progressively harder decode passes tuned for glossy /
        glary stickers, merging unique codes across all of them."""
        from PIL import ImageEnhance, ImageFilter, ImageOps
        found = {}

        def absorb(results):
            for r in results:
                text = r.data.decode("utf-8", errors="ignore").strip()
                if text and text not in found and self._is_our_qr(text):
                    found[text] = r

        try:
            absorb(_scan_qr(pil_img))
        except Exception:
            pass
        gray = pil_img.convert("L")

        # Adaptive (local) threshold — the key pass for specular glare:
        # each pixel is compared to its local neighborhood, so QR modules
        # survive even inside a washed-out highlight
        if len(found) < 4:
            try:
                import numpy as np
                g = np.asarray(gray, dtype=np.int16)
                bg = np.asarray(gray.filter(ImageFilter.GaussianBlur(15)),
                                dtype=np.int16)
                binary = ((g > bg - 6) * 255).astype("uint8")
                absorb(_scan_qr(Image.fromarray(binary)))
            except Exception:
                pass

        # Autocontrast with clipping — re-stretches frames the glare
        # has washed out
        if len(found) < 4:
            try:
                absorb(_scan_qr(ImageOps.autocontrast(gray, cutoff=3)))
            except Exception:
                pass

        if len(found) < 4:
            try:
                absorb(_scan_qr(ImageEnhance.Contrast(gray).enhance(2.0)))
            except Exception:
                pass

        if len(found) < 4:
            try:
                absorb(_scan_qr(pil_img.filter(ImageFilter.SHARPEN)))
            except Exception:
                pass

        return list(found.values())

    def _camera_loop(self):
        # One-time calibration: the unit is internally lit, so the right
        # exposure never changes. Let auto-exposure converge briefly,
        # then LOCK it — constant AE hunting (and the old bracketing
        # that toggled AE) made exposure swing, decodes flicker, and
        # bottles look like they were being removed and replaced.
        locked = False
        settle_until = time.time() + 3.5
        while self.camera_running:
            try:
                if not locked and time.time() >= settle_until:
                    try:
                        meta = self.camera.capture_metadata()
                        exp = int(meta.get("ExposureTime", 8000))
                        gain = float(meta.get("AnalogueGain", 2.0))
                        self.camera.set_controls({
                            "AeEnable": False,
                            # 20% darker than AE's pick: protects the
                            # glossy stickers from clipped highlights
                            "ExposureTime": max(500, int(exp * 0.8)),
                            "AnalogueGain": gain,
                        })
                    except Exception:
                        pass
                    locked = True

                frame = self.camera.capture_array()
                pil_img = Image.fromarray(frame[:, :, ::-1])

                results = self._decode_passes(pil_img)

                # Store frame + results for camera debug view
                self._camera_frame = pil_img
                self._camera_qr_results = list(results) if results else []

                if results:
                    seen_data = set()
                    qr_with_pos = []
                    for r in results:
                        text = r.data.decode("utf-8", errors="ignore").strip()
                        if text in seen_data:
                            continue
                        seen_data.add(text)
                        x_pos = r.rect.left if r.rect else 0
                        qr_with_pos.append((x_pos, text))
                    qr_with_pos.sort(key=lambda p: p[0], reverse=True)
                    self.root.after(0, self._handle_qr_results, qr_with_pos)

                if self._camera_view and self.mode == "camview":
                    self.root.after(0, self._draw_frame)

                time.sleep(0.2)
            except Exception:
                time.sleep(1)

    def _parse_qr_payload(self, raw_text):
        """Parse a QR code and return (slot, med_name) or None."""
        text = raw_text.strip()
        try:
            payload = json.loads(text)
            slot = payload.get("slot", "")
            med = payload.get("med", "")
            if slot and med:
                return (slot, med)
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
        return None

    def _is_our_qr(self, raw_text):
        """True only for QR codes we generated: DOSE JSON with one of
        our slots (blue/red/green/yellow/demo)."""
        parsed = self._parse_qr_payload(raw_text)
        return bool(parsed) and parsed[0] in (list(KNOWN_SLOTS) + ["demo"])

    def _handle_qr_results(self, qr_with_pos):
        now = time.time()
        triggered_addmed = False

        for x_pos, raw_text in qr_with_pos:
            parsed = self._parse_qr_payload(raw_text)
            if not parsed:
                continue  # not one of our codes — ignore entirely
            slot, med_name = parsed
            if slot not in KNOWN_SLOTS and slot != "demo":
                continue  # unknown slot — not a code we created

            # Known slot (blue/red/green/yellow) — instant recognition
            if slot in KNOWN_SLOTS:
                self.qr_last_seen[slot] = now
                md = self.med_data[slot]
                # The QR payload is the source of truth for the name —
                # overwrites any stale saved name (e.g. old "Vitamin D"
                # data lingering in med_data.json)
                correct_name = med_name or KNOWN_SLOTS[slot]
                if md.get("name") != correct_name:
                    md["name"] = correct_name
                    self._save_med()
                if not md.get("loaded"):
                    md["loaded"] = True
                    md["count"] = md.get("count", 0) or DEFAULT_QTY
                    self._save_med()
                elif (md.get("count", 0) <= 0 and self.dispense_state == 0
                        and self.mode in ("home", "storage")
                        and time.time() - self._qty_cancel_time > 30):
                    if not triggered_addmed:
                        self._show_qty_confirm(slot, md["name"])
                        return
                continue

            # Demo / unknown slot — new medication flow
            self.qr_last_seen["demo"] = now
            if not self._demo_registered:
                if (not triggered_addmed and self.dispense_state == 0
                        and self.mode in ("home", "storage")
                        and time.time() - self._addmed_cancel_time > 30):
                    self._draft_slot = "demo"
                    self._draft["name"] = med_name if med_name != "New Medication" else ""
                    self._prev_mode = self.mode
                    self.mode = "addmed"
                    self._draw_frame()
                    triggered_addmed = True
            else:
                md = self.med_data["demo"]
                if not md.get("loaded"):
                    md["name"] = med_name
                    md["loaded"] = True
                    md["count"] = DEFAULT_QTY

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
