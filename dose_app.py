#!/usr/bin/env python3
"""
DOSE Home Station — Prototype Application
v6.0 — PIL-rendered UI with User adherence screen, demo QR flow

Modes: Home, Storage, Settings, User (adherence tracking)
New QR ("demo" slot) triggers Add Med popup; old 4 QRs are instant.
"""

import functools
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
# A bottle counts as REMOVED only after this many completed scan passes
# in a row that did not see its code — never on elapsed time alone.
# At the camera's ~0.2 s cadence that is about 1.6 s of real scanning.
#
# Elapsed time is the wrong measure because it cannot tell "the bottle
# is gone" apart from "the camera thread didn't get to run". When the
# Pi is busy — speech recognition during a conversation is the obvious
# case — decode passes get starved, the old 2.5 s window lapsed, and
# the station announced a removal and then a replacement, over and
# over, while nothing had been touched.
QR_MISS_LIMIT = 8
# How long the voice panel stays on screen at minimum, so a fast reply
# is still readable instead of a flicker.
VOICE_OVERLAY_MIN_S = 1.8
# If the camera hasn't completed a pass in this long it is stalled, not
# empty: presence freezes rather than expiring.
QR_SCAN_STALL = 1.5
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
REPO = "relude117-star/doseconceptprototype"
BRANCH = "claude/quirky-brown-vkHwi"
RAW_URL = "https://raw.githubusercontent.com/%s/%s" % (REPO, BRANCH)

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


# ── DRAW CACHE ───────────────────────────────────────────────────────
# Every one of these builds an image from scratch: draw at 2x, then
# LANCZOS down. That is tens of milliseconds a frame, and the screen
# was paying it on EVERY redraw — a full repaint measured 80-116 ms on
# a dev machine, which is 650-930 ms on a Pi. That is the lag when a
# screen changes, when a button is pressed, and when the voice panel
# appears.
#
# They are pure functions of their arguments, so the result is cached.
# Nothing mutates a returned image (checked), so sharing one is safe.
# maxsize is generous but bounded: a station draws from a small, fixed
# set of shapes, so in practice this fills once and never evicts.
def _qw(value, step=4, lo=4):
    """Round a bar width to a step.

    Anything that draws a PROGRESS BAR calls the rounded-rectangle
    renderer with a width that changes continuously — the live mic
    meter redraws three of them every 120 ms. Cached by exact width
    that fills the cache with hundreds of near-identical images and
    evicts everything useful, which is worse than not caching at all.
    Rounding to 4 px is invisible on a 560 px bar and turns an
    unbounded set into about 140 entries that are reused forever."""
    return max(lo, int(value) // step * step)


def _drawcache(maxsize=256):
    def deco(fn):
        return functools.lru_cache(maxsize=maxsize)(fn)
    return deco


@_drawcache()
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


@_drawcache()
def _pil_circle(size, fill, scale=2):
    ss = size * scale
    img = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    fc = _hex_to_rgba(fill) if isinstance(fill, str) else fill
    d.ellipse([0, 0, ss - 1, ss - 1], fill=fc)
    resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
    return img.resize((size, size), resample)


@_drawcache()
def _pil_toggle_bg(w, h, is_on, off_colour, scale=2):
    """Cached inner form. The public wrapper takes the theme dict,
    which is unhashable — only the one colour it needs is passed in
    here, so the drawing can be cached like everything else."""
    sw, sh = w * scale, h * scale
    sr = (h // 2) * scale
    img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = _hex_to_rgba(ACCENT_BLUE) if is_on else _hex_to_rgba(off_colour)
    d.rounded_rectangle([0, 0, sw - 1, sh - 1], radius=sr, fill=bg)
    knob_r = int((h - 4) * scale / 2)
    cx = sw - knob_r - 2 * scale if is_on else knob_r + 2 * scale
    cy = sh // 2
    d.ellipse([cx - knob_r, cy - knob_r, cx + knob_r, cy + knob_r],
              fill=(255, 255, 255, 255))
    resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
    return img.resize((w, h), resample)


def _pil_toggle(w, h, is_on, theme, scale=2):
    return _pil_toggle_bg(w, h, bool(is_on), theme["elevated_bg"], scale)


@_drawcache()
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


@_drawcache()
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



@_drawcache()
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


@_drawcache()
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


def _pil_voice_wave(w, h, phase, amp, scale=2):
    """Siri-like waveform: three overlapping sine ribbons in dose blue."""
    sw, sh = w * scale, h * scale
    img = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    mid = sh / 2
    layers = [
        (_hex_to_rgba(DOSE_BLUE)[:3] + (230,), 1.0, 1.0, 0.0),
        (_hex_to_rgba(DOSE_BLUE_LT)[:3] + (170,), 0.72, 1.6, 1.9),
        ((255, 255, 255, 90), 0.45, 2.3, 4.1),
    ]
    for color, a_mul, f_mul, p_off in layers:
        pts = []
        for x in range(0, sw + 1, 3 * scale):
            u = x / sw
            envelope = math.sin(math.pi * u) ** 1.5
            y = mid + (mid - 3 * scale) * amp * a_mul * envelope * \
                math.sin(2 * math.pi * (u * 2.1 * f_mul) + phase + p_off)
            pts.append((x, y))
        d.line(pts, fill=color, width=2 * scale, joint="curve")
    resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
    return img.resize((w, h), resample)


@_drawcache()
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


@_drawcache()
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


@_drawcache()
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


@_drawcache()
def _pil_logo(path, size):
    """The logo at a given size, decoded ONCE.

    A 234 KB PNG was being opened, decoded and LANCZOS-resized on
    every single repaint — four decodes a frame across the rail and
    the screens. Decoding a file the user cannot change while the app
    runs, sixty times a minute, was most of what made the interface
    feel heavy."""
    resample = getattr(Image, 'LANCZOS', getattr(Image, 'ANTIALIAS', None))
    return Image.open(path).convert("RGBA").resize((size, size), resample)


@_drawcache()
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
        self._fit_cache = {}

        # ── State ──────────────────────────────────────────────────────────
        self.med_data = {}
        self.settings = {"night_mode": False, "alarm_sound": True,
                         "constant_scan": False, "voice_enabled": True,
                         "mic_device": "auto"}
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
        # Build ID: short hash of the running app file — shown in
        # Settings so the installed version is always verifiable
        try:
            with open(os.path.abspath(__file__), "rb") as _f:
                self._build_id = hashlib.md5(
                    _f.read()).hexdigest()[:7]
        except Exception:
            self._build_id = "unknown"
        self._banner_dismissed = False
        self._alert_key = None
        self._alert_return = "home"
        self._addmed_cancel_time = 0.0
        self._qty_cancel_time = 0.0
        self.qr_x_pos = {}   # last seen camera x-position per slot
        self.voice = None
        self._voice_state = "idle"
        self._voice_user_text = ""
        self._voice_reply = ""
        self._voice_anim_running = False
        self._voice_items = {}
        self._voice_sig = None
        self._voice_shown = {}
        self._voice_last_change = 0.0
        self._voice_imgs = {}
        self._voice_ov_t0 = 0.0
        # Screen changes are INSTANT. The fade code stays available —
        # flip this to True to bring the soft transition back.
        self._fx_enabled = False
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
        # consecutive completed scan passes that did NOT see each code
        self._qr_miss = {k: QR_MISS_LIMIT for k in SLOT_KEYS}
        self._qr_miss["demo"] = QR_MISS_LIMIT
        self._qr_held = {k: False for k in SLOT_KEYS}
        self._qr_held["demo"] = False
        self._qr_last_scan = 0.0
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

        # ── Voice assistant ("Hey Dose") ───────────────────────────────
        if self.settings.get("voice_enabled", True):
            self._start_voice()
            # Make sure she is the voice — on EVERY launch, not only
            # when something is missing. A station carrying an older
            # voice looks perfectly healthy to the probe, which is
            # exactly how one kept the slow voice through an update.
            # Build the panel image before it is ever needed, so the
            # very first time the overlay appears it is already there.
            self.root.after(400, self._voice_prerender_panel)
            self.root.after(900, self.migrate_voice)
            # install any NEW voice packages an update introduced,
            # and restore any companion module an old updater missed
            self.root.after(1200, self.heal_missing_modules)
            self.root.after(1500, self._ensure_python_deps)
        if self.has_touch:
            self._poll_touch()

        self.root.after(2000, lambda: self._do_update_check(silent=True))
        self.root.focus_force()

    # ── Safe widget raising ────────────────────────────────────────────────
    @staticmethod
    def _raise_widget(widget):
        widget.tk.call('raise', widget._w)

    def _get_tk_image(self, key, pil_img):
        """Tk handle for a PIL image, cached across frames.

        This used to build a NEW PhotoImage on every call and the
        dictionary was wiped at the top of every repaint, so it was a
        keep-alive list, not a cache. Combined with re-rendering each
        PIL image from scratch, a full repaint cost 80-116 ms on a dev
        machine — call it 650-930 ms on a Pi. That is what made a
        screen change, a button press and the voice panel feel slow.

        Because the generators above are memoized, identical arguments
        return the SAME object, so object identity is an exact test
        for "this is the picture I already converted"."""
        cached = self._img_cache.get(key)
        if cached is not None and cached[0] is pil_img:
            return cached[1]
        tk_img = ImageTk.PhotoImage(pil_img)
        if len(self._img_cache) > 400:
            self._img_cache.clear()
        self._img_cache[key] = (pil_img, tk_img)
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
        self._fit_cache = {}
        self._voice_panel_cache = {}
        self._voice_fit_cache = {}
        self._voice_sig = None
        self.root.configure(bg=self.theme["bg"])
        self.canvas.configure(bg=self.theme["bg"])
        self._draw_frame()

    # ── QR Presence ────────────────────────────────────────────────────────
    def _is_qr_present(self, key):
        """Is this bottle in the unit right now?

        Answered from COMPLETED SCAN PASSES, not from elapsed time. A
        slot is absent only once the camera has actually looked for it
        and not found it QR_MISS_LIMIT times running. If the camera is
        stalled or stopped, the last known answer stands — a busy CPU
        must never be reported as someone removing their medication."""
        if not getattr(self, "camera_running", False):
            return self._qr_held.get(key, False)
        since_scan = time.time() - getattr(self, "_qr_last_scan", 0.0)
        if since_scan > QR_SCAN_STALL:
            # the camera isn't keeping up — hold, don't guess
            return self._qr_held.get(key, False)
        present = (self._qr_miss.get(key, QR_MISS_LIMIT) < QR_MISS_LIMIT
                   and (time.time() - self.qr_last_seen.get(key, 0))
                   < QR_PRESENCE_TIMEOUT)
        self._qr_held[key] = present
        return present

    # ══════════════════════════════════════════════════════════════════════
    #  MASTER DRAW
    # ══════════════════════════════════════════════════════════════════════
    def _draw_frame(self):
        c = self.canvas
        c.delete("all")
        self._click_zones.clear()
        # NOT clearing _img_cache: that is the whole point of it. The
        # canvas items are gone, but the images they referenced are
        # about to be used again by this very repaint.

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
        elif self.mode == "micreport":
            self._draw_mic_report(c)
        elif self.mode == "micmeter":
            self._draw_mic_meter(c)
        elif self.mode == "calibrate":
            self._draw_calibrate(c)
        elif self.mode == "audit":
            self._draw_audit(c)
        elif self.mode == "sysinfo":
            self._draw_sysinfo(c)
        elif self.mode == "btaudio":
            self._draw_bt_audio(c)
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
        if active in ("micreport", "btaudio", "micmeter", "sysinfo",
                      "calibrate", "audit"):
            active = "settings"
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
            if mode_key == "home":
                # remember the logo rect for hold-to-talk detection
                self._home_btn_rect = (bx, y, bx + 72, y + 90)

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
                            img = _pil_logo(logo_path, 48)
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
        # Save schedule edits on nav even if some async redraw/alert has
        # changed the mode out from under the editor — the edit-in-
        # progress flag, not the current mode, decides.
        if self.mode == "timeedit" or getattr(self, "_te_active", False):
            self._te_commit()
        if mode_key == self.mode:
            self._draw_frame()
            return
        self._prev_mode = self.mode
        if not self._fx_enabled:
            self.mode = mode_key
            self._draw_frame()
            return
        self._fx_to(mode_key)

    def _fx_to(self, mode_key):
        """Soft fade-through-background between screens: a true-alpha
        veil in the theme background (no stipple dithering), eased in
        and out over ~190 ms. First frame is instant so taps feel
        immediate."""
        c = self.canvas
        veils = self._fx_veils()

        def veil(i):
            c.delete("fx_veil")
            c.create_image(0, 0, image=veils[i], anchor="nw",
                           tags="fx_veil")

        # near-instant: one soft frame in, switch, one soft frame out
        seq = [2, None, 0]

        def step(i):
            if i >= len(seq):
                c.delete("fx_veil")
                return
            if seq[i] is None:
                self.mode = mode_key
                self._draw_frame()
                veil(1)
            else:
                veil(seq[i])
            self.root.after(18, lambda: step(i + 1))

        step(0)

    def _fx_veils(self):
        """Cached translucent full-content veils, rebuilt on theme
        change: increasing alpha steps of the background color."""
        if getattr(self, "_fx_cache_bg", None) != self.theme["bg"]:
            self._fx_cache_bg = self.theme["bg"]
            r, g, b = _hex_to_rgb(self.theme["bg"])
            self._fx_imgs = [
                ImageTk.PhotoImage(Image.new(
                    "RGBA", (CONTENT_W, SCREEN_H), (r, g, b, a)))
                for a in (80, 160, 225, 250)]
        return self._fx_imgs

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
        vs = self._voice_status_text()
        if vs and "off" not in vs:
            hw.append(vs)
        if hw:
            c.create_text(32, SCREEN_H - 20,
                          text=self._fit_text("  ·  ".join(hw),
                                              self.font_small, 600),
                          font=self.font_small, fill="#444444",
                          anchor="sw")

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
        # Mirror the physical arrangement in the unit: the rightmost QR
        # in the camera frame is listed at the top, the leftmost at the
        # bottom — reordering live as bottles are swapped around
        visible.sort(key=lambda k: self.qr_x_pos.get(k, 0), reverse=True)

        for key in visible:
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
            try:
                rows = max(1, math.ceil(self.font_body.measure(txt) / 354))
            except Exception:
                rows = 1
            # never run into the DISPENSE button: truncate to what fits
            if ly + rows * line_h > 384:
                fit_rows = max(0, (384 - ly) // line_h)
                if fit_rows == 0:
                    break
                txt = self._fit_text(txt, self.font_body,
                                     354 * fit_rows - 24)
                rows = fit_rows
            c.create_text(px, ly, text=txt,
                          font=self.font_body, fill=t["fg"], anchor="nw",
                          width=354)
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
        self._te_active = True   # an edit is in progress until committed
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
        if not getattr(self, "_te_active", False):
            return   # nothing being edited — never write stale times
        self._te_active = False
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
            ("Voice Assistant", 4, "toggle", "voice_enabled",
             self.settings.get("voice_enabled", True)),
        ]

        row_h = card_h // 5
        for label, idx, kind, key, val in items:
            y = card_y + idx * row_h
            px = 52

            if idx < 4:
                c.create_line(px, y + row_h, 622, y + row_h,
                              fill=t["divider"])

            # Bigger icon (48px)
            icon_img = _pil_settings_icon(
                48, SETTINGS_ICON_COLORS[idx % len(SETTINGS_ICON_COLORS)])
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
                if key == "voice_enabled":
                    vs = self._voice_status_text() \
                        or self._voice_mic_subtext()
                    if vs:
                        c.create_text(px + 64, y + row_h // 2 + 16,
                                      text=self._fit_text(
                                          vs, self.font_small, 430),
                                      font=self.font_small,
                                      fill=t["muted"], anchor="w")
                    self._click_zones.append(
                        (px, y, px + 440, y + row_h,
                         self._voice_row_tap))

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
                ver = "Build " + getattr(self, "_build_id", "?")
                if status:
                    ver += "  ·  " + status
                c.create_text(px + 64, y + row_h // 2 + 16,
                              text=self._fit_text(
                                  ver + "   ·  tap for details",
                                  self.font_small, 430),
                              font=self.font_small, fill=t["muted"],
                              anchor="w")
                # tap the row (left of the button) -> what's installed
                self._click_zones.append(
                    (px, y, bx - 8, y + row_h, self._open_sysinfo))

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
        elif key == "voice_enabled":
            on = not self.settings.get("voice_enabled", True)
            self.settings["voice_enabled"] = on
            self._save_config()
            if on and self.voice is None:
                self._start_voice()
            if self.voice:
                self.voice.set_muted(not on)
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
        c.create_text(pad + 415, qy + 48,
                      text=self._fit_text(times_txt,
                                          self.font_medium, 185),
                      font=self.font_medium, fill=t["fg"],
                      anchor="center")

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
        if getattr(self, "_dispensed_after_id", None):
            try:
                self.root.after_cancel(self._dispensed_after_id)
            except Exception:
                pass
            self._dispensed_after_id = None
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
        """Auto-close the 'dispensed' screen. A QUEUED close must never
        yank the user off a screen they moved to in the meantime — if
        we're no longer on the dispensed screen, just clean up the
        timers and leave the current screen alone."""
        self._dispensed_after_id = None
        restore = (self.mode == "dispensed")
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
        if not restore:
            return
        self.mode = self._prev_mode
        self._draw_frame()

    def _cancel_home_longpress(self):
        """Drop any pending hold-to-talk timer/state (a new, unrelated
        interaction supersedes a half-finished press)."""
        if getattr(self, "_home_longpress_id", None):
            try:
                self.root.after_cancel(self._home_longpress_id)
            except Exception:
                pass
            self._home_longpress_id = None
        self._home_press_active = False

    def _on_canvas_press(self, event):
        # a fresh press cancels any stale hold-to-talk state
        self._cancel_home_longpress()
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
        # HOLD THE DOSE LOGO to talk: press-and-hold the Home logo for
        # ~0.7 s starts the voice assistant listening (no wake word).
        # A quick tap still navigates Home (handled on release).
        rect = getattr(self, "_home_btn_rect", None)
        if rect and rect[0] <= event.x <= rect[2] \
                and rect[1] <= event.y <= rect[3]:
            self._home_longpressed = False
            self._home_press_active = True
            self._home_longpress_id = self.root.after(
                700, self._home_longpress_fire)
            return
        self._on_canvas_click(event)

    def _home_longpress_fire(self):
        self._home_longpress_id = None
        if not getattr(self, "_home_press_active", False):
            return
        self._home_longpressed = True
        self._voice_push_to_talk()

    def _voice_push_to_talk(self):
        # Show the panel on the TAP. Everything below has to reach the
        # audio thread, which picks the request up on its next block —
        # up to a tenth of a second during which the screen said
        # nothing at all and the tap felt ignored. The engine will set
        # the real state a moment later; this is just immediate
        # acknowledgement that the press landed.
        try:
            self._voice_overlay_update("listening", "", "")
        except Exception:
            pass
        return self._voice_push_to_talk_inner()

    def _voice_push_to_talk_inner(self):
        """Enter talking mode.

        Not push-to-talk in the radio sense, despite the name: this
        opens a CONVERSATION. Tap the Dose logo once (or hold it from
        any screen) and it listens, answers, and keeps listening for
        whatever you say next. It stops when you tap outside the
        panel, tap the logo again, say you're done, or go quiet."""
        if self.voice and getattr(self.voice, "available", False):
            if self.voice.request_listen():
                return
        # No live engine (first boot, or libraries still installing):
        # bring one up right now rather than silently doing nothing.
        try:
            if self.voice is None:
                self._start_voice()
            if self.voice and getattr(self.voice, "available", False):
                if self.voice.request_listen():
                    return
        except Exception:
            pass
        # Still not ready. Take the panel back down first — we put it
        # up optimistically when the press landed, and leaving a
        # "listening" panel floating over the audio settings screen
        # would be a lie about what is happening.
        self._voice_dismiss()
        self._voice_row_tap()

    def _on_canvas_release(self, event):
        # Dose-logo hold-to-talk: on release, decide tap vs. hold
        if getattr(self, "_home_press_active", False):
            self._home_press_active = False
            if getattr(self, "_home_longpress_id", None):
                self.root.after_cancel(self._home_longpress_id)
                self._home_longpress_id = None
            if not getattr(self, "_home_longpressed", False):
                # A QUICK TAP. You should not have to hold the logo
                # down like a radio button to have a conversation —
                # one tap puts it in talking mode and it stays there
                # until you tap away or say you're done.
                if self._voice_state != "idle":
                    self._voice_dismiss()      # tap again to stop
                elif self.mode == "home":
                    # already Home, so navigating Home does nothing:
                    # the tap means "listen to me"
                    self._voice_push_to_talk()
                else:
                    self._nav("home")
            return
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
        self._dispensed_after_id = self.root.after(
            int(DISPENSED_TIME * 1000), self._end_dispense)

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
            # Reaped on the next alarm: a fire-and-forget Popen that is
            # never waited on leaves a zombie behind every time, and
            # this fires on every dose.
            old = getattr(self, "_chime_proc", None)
            if old is not None and old.poll() is not None:
                self._chime_proc = None
            self._chime_proc = subprocess.Popen(
                ["aplay", "-q",
                 "/usr/share/sounds/alsa/Front_Center.wav"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
        c.create_text(80, 118,
                      text=self._fit_text(self._qty_name,
                                          self.font_name_lg, 500),
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
        # A tap OUTSIDE the voice panel means "we're done here". It is
        # the obvious gesture and it needs no words — the alternative
        # was waiting out the follow-up timeout. A tap inside the panel
        # is ignored so you can't dismiss it by aiming badly.
        if self._voice_state != "idle":
            box = self._voice_panel_box()
            if box and not (box[0] <= x <= box[2] and box[1] <= y <= box[3]):
                self._voice_dismiss()
                return
        for (x1, y1, x2, y2, callback) in reversed(self._click_zones):
            if x1 <= x <= x2 and y1 <= y <= y2:
                callback()
                return

    def _voice_panel_box(self):
        """Where the voice panel is on screen right now, or None."""
        try:
            ids = self.canvas.find_withtag("voice_ov")
            if not ids:
                return None
            boxes = [b for b in (self.canvas.bbox(i) for i in ids) if b]
            if not boxes:
                return None
            return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                    max(b[2] for b in boxes), max(b[3] for b in boxes))
        except Exception:
            return None

    def _voice_dismiss(self):
        """End the conversation now: stop listening and take the panel
        away. Safe to call whether or not the engine is running."""
        try:
            if self.voice is not None:
                self.voice.close_conversation()
        except Exception:
            pass
        self._voice_state = "idle"
        self._voice_hide_at = 0.0          # no hold — they asked it to go
        self._voice_finish_overlay()

    # ══════════════════════════════════════════════════════════════════════
    #  CLOCK TICK
    # ══════════════════════════════════════════════════════════════════════
    def _screen_signature(self):
        """Everything the idle screens actually display.

        The clock shows hours and minutes — no seconds — so repainting
        every second was sixty times more often than anything on
        screen could change. A full repaint is the most expensive
        thing this app does, so that alone was most of its idle load."""
        try:
            now = datetime.now()
            return (
                self.mode,
                now.hour, now.minute,
                tuple(self._is_qr_present(k) for k in SLOT_KEYS),
                tuple(self._get_count(k) for k in SLOT_KEYS),
                tuple(self._is_loaded(k) for k in SLOT_KEYS),
                tuple(sorted(self._due_keys or ())),
                self.dispense_state,
                bool(self._banner_dismissed),
                self.theme_name if hasattr(self, "theme_name") else "",
            )
        except Exception:
            return None

    def _tick_clock(self):
        self._check_presence_changes()
        self._check_due_doses()
        if self.mode in ("home", "storage"):
            sig = self._screen_signature()
            # Repaint when something changed — and unconditionally
            # every 30 s regardless, as a backstop. A stale screen is
            # worse than the CPU it saves, so anything this signature
            # might not cover still gets picked up within half a
            # minute.
            now = time.time()
            stale = now - getattr(self, "_last_full_draw", 0) > 30
            if sig is None or sig != getattr(self, "_screen_sig", None) \
                    or stale:
                self._screen_sig = sig
                self._last_full_draw = now
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
            if self._fx_enabled:
                self._fx_to("dosealert")
            else:
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
        """Truncate text with an ellipsis so it never clips its container.

        Cached. Each call asks Tk to measure glyphs, and the truncation
        loop asks once PER CHARACTER REMOVED — measured at 20 font
        measurements per repaint on the home screen, about 13 ms of
        every frame. The answer only depends on the string, the font
        and the width, none of which change between repaints, so it is
        computed once."""
        cache = self._fit_cache
        key = (text, id(font), max_w)
        hit = cache.get(key)
        if hit is not None:
            return hit
        out = text
        try:
            if font.measure(text) > max_w:
                # binary search instead of shaving one character at a
                # time: ~7 measurements for a long label instead of
                # dozens
                lo, hi = 0, len(text)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if font.measure(text[:mid] + "…") <= max_w:
                        lo = mid
                    else:
                        hi = mid - 1
                out = text[:lo] + "…"
        except Exception:
            out = text
        if len(cache) > 600:
            cache.clear()
        cache[key] = out
        return out

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

    # Every python module the app imports besides itself. The updater
    # downloads each one, and a change in any of them triggers an
    # update — so adding a new module can never silently miss devices.
    COMPANION_MODULES = ("dose_voice.py", "dose_nlu.py")

    @staticmethod
    def _fetch_repo_file(fname, timeout=20, api_only=False):
        """Fetch a repo file, freshest source first.

        1. GitHub API (application/vnd.github.raw) — authoritative,
           never CDN-stale, so the UPDATE button always sees the
           newest push immediately.
        2. raw.githubusercontent with a cache-busting query string
           (skipped when api_only — auto-applied updates must never
           risk a stale-CDN downgrade).
        """
        from urllib.request import Request
        api = ("https://api.github.com/repos/relude117-star/"
               "doseconceptprototype/contents/%s"
               "?ref=claude/quirky-brown-vkHwi" % fname)
        try:
            req = Request(api, headers={
                "Accept": "application/vnd.github.raw",
                "User-Agent": "dose-home-station"})
            return urlopen(req, timeout=timeout).read()
        except Exception:
            if api_only:
                raise
        url = RAW_URL + "/" + fname + "?nocache=%d" % int(time.time())
        return urlopen(url, timeout=timeout).read()

    def _do_update_check(self, silent=False):
        try:
            remote_data = self._fetch_repo_file("dose_app.py")
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

        # a change in ANY companion module also counts as an update
        # (a missing module locally counts too — that is how a brand new
        # file like dose_nlu.py reaches a device that has never had it)
        if remote_hash == local_hash:
            here = os.path.dirname(os.path.abspath(__file__))
            for mod in self.COMPANION_MODULES:
                try:
                    mremote = hashlib.md5(
                        self._fetch_repo_file(mod)).hexdigest()
                    mpath = os.path.join(here, mod)
                    if not os.path.exists(mpath):
                        local_hash = "module-missing"
                        break
                    with open(mpath, "rb") as f:
                        mlocal = hashlib.md5(f.read()).hexdigest()
                    if mremote != mlocal:
                        local_hash = "module-outdated"
                        break
                except Exception:
                    pass

        if remote_hash == local_hash:
            if not silent:
                self.root.after(0, self._update_result,
                                "Up to date (build %s)"
                                % remote_hash[:7])
            return
        if silent:
            # Never auto-overwrite on launch: GitHub's raw CDN can serve a
            # stale file for ~5 min, which would "update" BACKWARDS and
            # revert newer local code. Just announce it — applying stays
            # one tap away on the UPDATE button.
            self.root.after(0, self._update_result,
                            "Update available: %s → %s"
                            % (local_hash[:7], remote_hash[:7]))
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
            """ATOMIC update: fetch and VALIDATE everything first, and
            only write once every required file is known good. A failed
            or truncated download can therefore never leave a broken
            app (new dose_app.py against a stale/missing module)."""
            try:
                local_path = os.path.abspath(__file__)
                here = os.path.dirname(local_path)

                # 1) collect — required python first, then extras
                payload = {"dose_app.py": remote_data}
                for mod in self.COMPANION_MODULES:
                    payload[mod] = self._fetch_repo_file(mod)

                # 2) validate: real python, not an error page or a
                #    truncated body. Anything bad aborts the update.
                for name, data in payload.items():
                    if not data or len(data) < 500:
                        raise ValueError("%s download too small" % name)
                    try:
                        compile(data.decode("utf-8"), name, "exec")
                    except Exception as e:
                        raise ValueError("%s is not valid python (%s)"
                                         % (name, e))

                # 3) optional extras — never block the update
                extras = {}
                for fname in ("DOSE.sh", "dose_logo.png", "demo_qr.png"):
                    try:
                        d = self._fetch_repo_file(fname)
                        if d:
                            extras[fname] = d
                    except Exception:
                        pass

                # 4) commit — write everything, app LAST so a crash
                #    mid-write never leaves a new app beside old modules
                os.makedirs(APP_DIR, exist_ok=True)
                for name, data in payload.items():
                    if name == "dose_app.py":
                        continue
                    for d in (here, APP_DIR):
                        tmp = os.path.join(d, name + ".tmp")
                        with open(tmp, "wb") as f:
                            f.write(data)
                        os.replace(tmp, os.path.join(d, name))
                for name, data in extras.items():
                    fpath = os.path.join(APP_DIR, name)
                    tmp = fpath + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(data)
                    os.replace(tmp, fpath)
                    if name.endswith(".sh"):
                        os.chmod(fpath, 0o755)
                for d in (here, APP_DIR):
                    tmp = os.path.join(d, "dose_app.py.tmp")
                    with open(tmp, "wb") as f:
                        f.write(remote_data)
                    os.replace(tmp, os.path.join(d, "dose_app.py"))

                self.root.after(0, self._finish_update)
            except Exception as e:
                self.root.after(0, self._update_result,
                                "Update failed (nothing changed): %s" % e)

        threading.Thread(target=do_download, daemon=True).start()

    def _finish_update(self):
        """Files are in. Now finish the ACTUAL job.

        The button used to replace .py files and restart, and that was
        it. Anything a new version needed — a python package, a speech
        model, the voice detector — was left to a shell script the
        user had to find and run, and the screen said so. That is the
        one thing they cannot do from the station in front of them, so
        the button now finishes the whole job itself and only restarts
        once there is nothing left to fetch."""
        self._update_status_text = "Updated — finishing setup…"
        if self.mode in ("settings", "sysinfo"):
            self._draw_frame()
        self.root.after(200, self._update_finish_setup)

    def _update_finish_setup(self):
        """Install and download whatever the new version needs."""
        def work():
            steps = []
            try:
                missing = dict(self.missing_voice_deps())
            except Exception:
                missing = {}
            if missing:
                steps.append("libraries")
                try:
                    self._ensure_python_deps()
                except Exception:
                    pass
            # models: voice, speech, and the voice detector
            try:
                vdir = os.path.expanduser("~/dose-home-station/voice")
                import glob as _g
                if not _g.glob(os.path.join(vdir, "*.onnx")) or \
                        not _g.glob(os.path.join(vdir, "vosk-model*")):
                    steps.append("models")
                    self._voice_download_models(force=True)
            except Exception:
                pass
            try:
                if self.voice is not None:
                    self.voice.fetch_vad_model()
                    self.voice.ensure_upgraded_models()
            except Exception:
                pass
            self._update_status_text = (
                "Updated — %s installing in the background. Restarting…"
                % ", ".join(steps) if steps
                else "Updated! Restarting…")

        threading.Thread(target=work, daemon=True,
                         name="update-finish").start()
        # Restart regardless: the downloads continue after the restart
        # and report themselves on the audit page. A station that will
        # not come back up is worse than one still fetching a model.
        self.root.after(2500, self._restart_app)

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
        # 20 ms, not 50. A capacitive pad polled every 50 ms can be up
        # to 50 ms late on top of the redraw, which is what "it takes a
        # while to recognise my hand" feels like. The read is a couple
        # of I2C registers and costs almost nothing.
        self.root.after(20, self._poll_touch)

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

    # How often the EXPENSIVE decode passes may run, at most.
    HARD_PASS_PERIOD = 1.0

    def _expected_codes(self):
        """How many of our codes we currently believe are in view."""
        try:
            return sum(1 for v in self._qr_held.values() if v)
        except Exception:
            return 0

    @staticmethod
    def _frame_to_gray(frame):
        """Grayscale image for decoding, as cheaply as possible.

        A QR code carries no colour. Converting every frame to RGB and
        letting PIL convert that to grayscale does two full-image
        passes for nothing."""
        try:
            import numpy as np
            if frame.ndim == 3:
                # ITU-R 601 luma, in integer arithmetic
                b = frame[:, :, 0].astype(np.uint16)
                g = frame[:, :, 1].astype(np.uint16)
                r = frame[:, :, 2].astype(np.uint16)
                y = ((r * 77 + g * 150 + b * 29) >> 8).astype(np.uint8)
            else:
                y = frame
            return Image.fromarray(y, mode="L")
        except Exception:
            return Image.fromarray(frame[:, :, ::-1]).convert("L")

    def _decode_passes(self, pil_img):
        """Decode the frame, escalating only when it is actually needed.

        There are five passes here: a plain scan, then an adaptive
        threshold, autocontrast, a contrast stretch and a sharpen, each
        followed by its own zbar scan. They exist because glossy
        stickers under a bright internal light are genuinely hard to
        read, and they work.

        They were ALL running five times a second, whenever fewer than
        four codes were visible — which is nearly always, since most
        people do not have four bottles in view. Measured on a 640x480
        frame that is 14.5 ms of image processing per frame before any
        scanning, about 58% of a Pi core, and roughly three times that
        at 720p. It is the single biggest thing running on this device.

        Now: the cheap pass runs every frame. The expensive ones run
        only when the cheap pass came back with less than we already
        believe is in there — and then at most once a second, not five
        times. In the steady state, where the plain scan finds what is
        there, they never run at all."""
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

        # Good enough? Then stop — this is the common case.
        if found and len(found) >= self._expected_codes():
            return list(found.values())

        now = time.time()
        if now - getattr(self, "_last_hard_pass", 0) < self.HARD_PASS_PERIOD:
            return list(found.values())
        self._last_hard_pass = now

        gray = pil_img if pil_img.mode == "L" else pil_img.convert("L")

        def enough():
            """Stop escalating once we have what we expect. The passes
            get more expensive as they go — the sharpen at the end is
            the costliest single operation here — so there is no point
            running the hard ones after the answer is already in."""
            want = max(1, self._expected_codes())
            return len(found) >= want

        # Adaptive (local) threshold — the key pass for specular glare:
        # each pixel is compared to its local neighborhood, so QR modules
        # survive even inside a washed-out highlight
        if not enough():
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
        if not enough():
            try:
                absorb(_scan_qr(ImageOps.autocontrast(gray, cutoff=3)))
            except Exception:
                pass

        if not enough():
            try:
                absorb(_scan_qr(ImageEnhance.Contrast(gray).enhance(2.0)))
            except Exception:
                pass

        if not enough():
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

                # Scan in GRAYSCALE. A QR code has no colour, and
                # building a full RGB image for every frame — the
                # channel flip alone is 2.25 ms here, about 18 ms on a
                # Pi, some 9% of a core at five frames a second — buys
                # nothing the decoder uses. Luma straight out of the
                # array is a fraction of that, and the colour image is
                # only built when the debug view is actually open.
                pil_img = self._frame_to_gray(frame)

                results = self._decode_passes(pil_img)

                # Colour frame for the camera debug view — built ONLY
                # when that screen is up.
                if self._camera_view and self.mode == "camview":
                    try:
                        self._camera_frame = Image.fromarray(
                            frame[:, :, ::-1])
                    except Exception:
                        self._camera_frame = pil_img
                else:
                    self._camera_frame = pil_img
                self._camera_qr_results = list(results) if results else []

                # Report EVERY completed pass, including empty ones.
                # Presence is counted in passes, so the UI has to know
                # the difference between "looked and saw nothing" and
                # "never got to look".
                seen_data = set()
                qr_with_pos = []
                for r in (results or []):
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
        """Parse a QR code and return (slot, med_name) or None.
        Input boundary: QR content is untrusted data — the name is
        sanitized (printable characters only) and length-capped so a
        crafted code cannot inject junk into the UI, storage, or the
        voice assistant's speech."""
        text = raw_text.strip()
        if len(text) > 500:
            return None
        try:
            payload = json.loads(text)
            slot = payload.get("slot", "")
            med = payload.get("med", "")
            if not (isinstance(slot, str) and isinstance(med, str)):
                return None
            slot = slot.strip()[:20]
            med = "".join(ch for ch in med if ch.isprintable())
            med = " ".join(med.split())[:40]
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

        # One completed scan pass. Count a miss for every slot this
        # pass did not see, and reset the counter for those it did.
        # This runs BEFORE anything below can return early, so the
        # bookkeeping can never be skipped by a flow that opens a
        # screen.
        self._qr_last_scan = now
        seen_slots = set()
        for _x, _raw in qr_with_pos:
            _p = self._parse_qr_payload(_raw)
            if _p:
                seen_slots.add(_p[0])
        for _k in list(self._qr_miss):
            if _k in seen_slots:
                self._qr_miss[_k] = 0
            elif self._qr_miss[_k] < QR_MISS_LIMIT:
                self._qr_miss[_k] += 1

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
                self.qr_x_pos[slot] = x_pos
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
            self.qr_x_pos["demo"] = x_pos
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

    # ══════════════════════════════════════════════════════════════════════
    #  VOICE ASSISTANT — "Hey Dose" (see dose_voice.py)
    # ══════════════════════════════════════════════════════════════════════
    _WP_LUA = ("bluez_monitor.properties = {\n"
               '  ["bluez5.enable-sbc-xq"] = true,\n'
               '  ["bluez5.enable-msbc"] = true,\n'
               '  ["bluez5.enable-hw-volume"] = true,\n'
               '  ["bluez5.headset-roles"] = '
               '"[ hsp_hs hsp_ag hfp_hf hfp_ag ]",\n'
               '  ["bluez5.hfphsp-backend"] = "native",\n'
               '  ["bluez5.roles"] = "[ a2dp_sink a2dp_source '
               'hsp_hs hsp_ag hfp_hf hfp_ag ]",\n'
               "}\n")
    _WP_CONF = ("monitor.bluez.properties = {\n"
                "  bluez5.enable-sbc-xq = true\n"
                "  bluez5.enable-msbc = true\n"
                "  bluez5.enable-hw-volume = true\n"
                "  bluez5.headset-roles = "
                "[ hsp_hs hsp_ag hfp_hf hfp_ag ]\n"
                '  bluez5.hfphsp-backend = "native"\n'
                "  bluez5.roles = [ a2dp_sink a2dp_source "
                "hsp_hs hsp_ag hfp_hf hfp_ag ]\n"
                "}\n")

    def _ensure_bt_mic_config(self):
        """Idempotent: if the Bluetooth-microphone (HFP) audio config
        is already in place, do nothing; if missing, write it and
        restart the user audio services. No sudo needed — user-level
        files and services. Pi only."""
        on_pi = (os.path.exists("/boot/config.txt")
                 or os.path.exists("/boot/firmware/config.txt"))
        if not on_pi or os.environ.get("DOSE_DISABLE_SELF_INSTALL"):
            return
        home = os.path.expanduser("~")
        lua = os.path.join(home, ".config/wireplumber/"
                                 "bluetooth.lua.d/50-dose-bluez.lua")
        conf = os.path.join(home, ".config/wireplumber/"
                                  "wireplumber.conf.d/"
                                  "50-dose-bluez.conf")
        if os.path.exists(lua) and os.path.exists(conf):
            return   # already configured — just use it
        try:
            os.makedirs(os.path.dirname(lua), exist_ok=True)
            os.makedirs(os.path.dirname(conf), exist_ok=True)
            with open(lua, "w") as f:
                f.write(self._WP_LUA)
            with open(conf, "w") as f:
                f.write(self._WP_CONF)
            for svc_cmd in (
                    ["systemctl", "--user", "restart", "wireplumber",
                     "pipewire", "pipewire-pulse"],
                    ["wpctl", "settings", "--save",
                     "bluetooth.autoswitch-to-headset-profile",
                     "true"]):
                try:
                    subprocess.run(svc_cmd, capture_output=True,
                                   timeout=30)
                except Exception:
                    pass
        except Exception:
            pass

    def _ensure_audio_packages(self):
        """Install the Bluetooth audio tool packages, VISIBLY: the
        outcome (or the exact reason it can't) is stored and shown
        on the Voice & Bluetooth screen and in the mic report.
        Retries whenever the audio screen is opened."""
        import shutil as _sh
        # arecord (alsa-utils) is the PRIMARY capture path now, so it
        # is the one that must be present; pactl/parec are secondary.
        if _sh.which("arecord") and _sh.which("pactl"):
            self._audio_pkg_status = ""
            return
        on_pi = (os.path.exists("/boot/config.txt")
                 or os.path.exists("/boot/firmware/config.txt"))
        if not on_pi or os.environ.get("DOSE_DISABLE_SELF_INSTALL"):
            return
        if getattr(self, "_audio_pkg_busy", False):
            return
        if time.time() - getattr(self, "_audio_pkg_last", 0) < 60:
            return
        self._audio_pkg_busy = True
        self._audio_pkg_last = time.time()
        self._audio_pkg_status = "installing audio tools…"

        def worker():
            import shutil as _sh
            status = ""
            try:
                # Known USB-mic fix: make sure the user can access
                # audio hardware (a PCM2902 records silence if the
                # account isn't in the 'audio' group). Best-effort.
                try:
                    subprocess.run(
                        ["sudo", "-n", "usermod", "-a", "-G", "audio",
                         os.environ.get("USER", "")],
                        capture_output=True, timeout=15)
                except Exception:
                    pass
                # refresh the package index first — a stale index is
                # the usual reason a package name fails to resolve
                subprocess.run(["sudo", "-n", "apt-get", "update"],
                               capture_output=True, text=True,
                               timeout=300)
                # install ONE BY ONE: apt is all-or-nothing per
                # command, so a single unresolvable package must
                # never sink the critical ones
                pkgs = ["alsa-utils", "pulseaudio-utils",
                        "pipewire-pulse", "pipewire", "pipewire-alsa",
                        "wireplumber", "libspa-0.2-bluez5"]
                failed = []
                sudo_blocked = False
                for pkg in pkgs:
                    try:
                        r = subprocess.run(
                            ["sudo", "-n", "apt-get", "install",
                             "-y", pkg],
                            capture_output=True, text=True,
                            timeout=300)
                        err = (r.stderr or "").lower()
                        if r.returncode != 0:
                            if ("password is required" in err
                                    or "a terminal is required"
                                    in err):
                                sudo_blocked = True
                                break
                            if "lock" in err:
                                self._audio_pkg_last = 0
                            failed.append(pkg)
                    except Exception:
                        failed.append(pkg)
                if sudo_blocked:
                    status = ("can't install audio tools without a "
                              "password — everything else still works")
                elif _sh.which("arecord"):
                    status = ("audio tools installed ✓"
                              + (" (optional missing: %s)"
                                 % ", ".join(failed) if failed
                                 else ""))
                    subprocess.run(
                        ["systemctl", "--user", "restart",
                         "wireplumber", "pipewire",
                         "pipewire-pulse"],
                        capture_output=True, timeout=30)
                else:
                    status = ("install incomplete — failed: "
                              + ", ".join(failed[:3]))
            except Exception as e:
                status = "install failed: %s" % e

            def done():
                self._audio_pkg_busy = False
                self._audio_pkg_status = status
                if self.voice:
                    self.voice.request_reopen()
                self._draw_frame()
            try:
                self.root.after(0, done)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    # Python packages the upgraded voice needs. The UPDATE button only
    # replaces .py files and restarts python directly (not DOSE.sh), so
    # without this a new dependency would NEVER install on a device
    # whose voice already works — the upgrade would silently do nothing.
    VOICE_DEPS = (
        ("rapidfuzz", "rapidfuzz"),          # phonetic drug matching
        ("jellyfish", "jellyfish"),          # metaphone
        ("audioop", "audioop-lts"),          # py3.13 removed audioop
        ("moonshine_voice", "moonshine-voice"),   # fast recogniser
        # The stronger model, used only when the fast one comes back
        # with something that does not parse. Giving up quickly is not
        # an accuracy strategy.
        ("faster_whisper", "faster-whisper"),
        # NOTE: silero-vad is deliberately NOT a pip dependency. The
        # package requires torch and torchaudio — hundreds of
        # megabytes, and installing them on a 4 GB Pi drove the load
        # average to 5.0 and the install failed anyway. We need one
        # 2.3 MB ONNX file and onnxruntime, which is already here for
        # the speech models. It is fetched directly; see
        # VAD_MODEL_URL in dose_voice.py.
    )

    def _swap_voice_engine(self):
        """Restart the voice engine to pick up newly installed
        libraries WITHOUT ever leaving self.voice as None — hold-to-talk
        and the mic picker both need a live engine, so the old one is
        kept until a working replacement exists."""
        old = self.voice
        try:
            import importlib
            if "dose_voice" in sys.modules:
                importlib.reload(sys.modules["dose_voice"])
            from dose_voice import DoseVoice
            new = DoseVoice(self)
            if not getattr(new, "available", False):
                return False          # keep the old engine running
            try:
                if old:
                    old.stop()
            except Exception:
                pass
            new.start()
            self.voice = new
            return True
        except Exception:
            return False

    def _open_sysinfo(self):
        """What is actually installed and running on THIS device."""
        self._prev_mode = self.mode
        self.mode = "sysinfo"
        self._draw_frame()
        self._sysinfo_tick()
        # refresh the "what's on GitHub" column in the background
        if not getattr(self, "_sysinfo_busy", False):
            self._sysinfo_busy = True

            def work():
                remote = {}
                for f in ("dose_app.py",) + self.COMPANION_MODULES:
                    try:
                        remote[f] = hashlib.md5(
                            self._fetch_repo_file(f)).hexdigest()[:7]
                    except Exception:
                        remote[f] = "?"

                def done():
                    self._sysinfo_remote = remote
                    self._sysinfo_busy = False
                    if self.mode == "sysinfo":
                        self._draw_frame()
                try:
                    self.root.after(0, done)
                except Exception:
                    pass
            threading.Thread(target=work, daemon=True).start()

    def _sysinfo_tick(self):
        """Keep the page live while it's open, so installs and model
        downloads visibly progress instead of looking frozen."""
        if self.mode != "sysinfo":
            return
        self._draw_frame()
        try:
            self.root.after(2000, self._sysinfo_tick)
        except Exception:
            pass

    def _close_sysinfo(self):
        self.mode = "settings"
        self._draw_frame()

    def _draw_sysinfo(self, c):
        t = self.theme
        card = _pil_rounded_rect(620, 440, 22, t["card_bg"])
        c.create_image(26, 20, image=self._get_tk_image("sys_card", card),
                       anchor="nw")
        c.create_text(56, 38, text="INSTALLED ON THIS DEVICE",
                      font=self.font_label, fill=t["muted"], anchor="nw")
        remote = getattr(self, "_sysinfo_remote", {})
        # Two columns so nothing ever runs off the bottom of the card:
        # software on the left, the hardware this is running on (and
        # anything about it that is costing response time) on the right.
        COL_L, COL_R, COL_W = 56, 330, 250
        y = 62
        col = COL_L

        def line(txt, colour=None, dy=15, font=None):
            nonlocal y
            c.create_text(col, y,
                          text=self._fit_text(txt, font or self.font_tiny,
                                              COL_W if col == COL_R
                                              else 272),
                          font=font or self.font_tiny,
                          fill=colour or t["fg"], anchor="nw")
            y += dy

        # ── program files: device build vs what GitHub is serving ──
        line("PROGRAM FILES  (device  /  GitHub)", t["muted"])
        here = os.path.dirname(os.path.abspath(__file__))
        files = [("dose_app.py", getattr(self, "_build_id", "?"))]
        for m, ok_, bid in self.module_report():
            files.append((m, bid if ok_ else "MISSING"))
        for name, local in files:
            rem = remote.get(name, "…")
            same = (local == rem)
            mark = "same" if same else ("MISSING" if local == "MISSING"
                                        else "differs")
            colour = ("#2ECC71" if same else
                      "#FF6B6B" if local == "MISSING" else "#F1C40F")
            line("   %-15s %-9s %-9s %s" % (name, local, rem, mark),
                 colour)

        # ── voice models actually in use ──
        y += 6
        line("MODELS IN USE", t["muted"])
        gone = getattr(self, "_voice_migrated", None)
        if gone:
            line("   retired: %s" % ", ".join(
                g.replace(".onnx", "") for g in gone)[:34], "#2ECC71")
        try:
            rows = self.voice.model_status() if self.voice else []
        except Exception:
            rows = []
        if not rows:
            line("   (voice engine not started)", t["muted"])
        for label, ok_, detail in rows:
            colour = ("#2ECC71" if ok_ else
                      "#F1C40F" if ok_ is None else "#FF6B6B")
            line("   %-26s %s" % (label, detail or
                                  ("ready" if ok_ else "not present")),
                 colour)

        # ── python libraries ──
        y += 6
        line("UPGRADE LIBRARIES", t["muted"])
        try:
            missing = dict(self.missing_voice_deps())
        except Exception:
            missing = {}
        for mod, pkg in self.VOICE_DEPS:
            present = mod not in missing
            line("   %-22s %s" % (pkg, "installed" if present
                                  else "MISSING — installing"),
                 "#2ECC71" if present else "#F1C40F")
        ds = getattr(self, "_deps_status", "")
        if ds:
            y += 4
            line(ds, DOSE_BLUE_LT)

        # ── the hardware, and whether it is holding us back ──
        def _endpoint_s():
            try:
                import dose_voice as _dv
                return float(_dv.ENDPOINT_SILENCE)
            except Exception:
                return 0.55

        col, y = COL_R, 62
        line("RASPBERRY PI — SPEED", t["muted"])
        try:
            hw = self.voice.hardware_report() if self.voice else []
        except Exception:
            hw = []
        if not hw:
            line("   (voice engine not started)", t["muted"])
        for label, detail, good in hw:
            line("   %-13s %s" % (label, detail),
                 "#2ECC71" if good else "#F1C40F")

        y += 6
        line("RESPONSE BUDGET", t["muted"])
        for label, detail in (
                ("end of speech", "%.2f s" % _endpoint_s()),
                ("recognise", "~0.25 s"),
                ("answer", "instant (on-device)"),
                ("first words", "~0.20 s"),
                ("TOTAL", "under 1 s")):
            line("   %-13s %s" % (label, detail),
                 DOSE_BLUE_LT if label == "TOTAL" else t["fg"])
        col = COL_L

        for label, x0, cb in (("CHECK NOW", 56, self._on_update_pressed),
                              ("AUDIT", 236, self._open_audit),
                              ("CLOSE", 466, self._close_sysinfo)):
            b = _pil_rounded_rect(150, 44, 14,
                                  DOSE_BLUE if label == "CLOSE"
                                  else t["elevated_bg"])
            c.create_image(x0, 400,
                           image=self._get_tk_image("sys_%s" % label, b),
                           anchor="nw")
            c.create_text(x0 + 75, 422, text=label,
                          font=self.font_small_bold,
                          fill="#06101E" if label == "CLOSE" else t["fg"],
                          anchor="center")
            self._click_zones.append((x0, 400, x0 + 150, 444, cb))

    def module_report(self):
        """Ground truth about the companion modules ON THIS DEVICE:
        [(name, present, build_id)]. Shown on screen so you never have
        to guess whether an update actually delivered a new file."""
        here = os.path.dirname(os.path.abspath(__file__))
        out = []
        for mod in self.COMPANION_MODULES:
            path = os.path.join(here, mod)
            if os.path.exists(path):
                try:
                    with open(path, "rb") as f:
                        bid = hashlib.md5(f.read()).hexdigest()[:7]
                except Exception:
                    bid = "?"
                out.append((mod, True, bid))
            else:
                out.append((mod, False, "MISSING"))
        return out

    def heal_missing_modules(self):
        """If a companion module isn't on the device (an older updater
        never shipped it), fetch it now, then restart the voice engine
        so it is actually used."""
        if getattr(self, "_heal_busy", False):
            return
        gone = [m for m, ok_, _ in self.module_report() if not ok_]
        if not gone:
            return
        self._heal_busy = True

        def work():
            here = os.path.dirname(os.path.abspath(__file__))
            got = []
            for mod in gone:
                try:
                    data = self._fetch_repo_file(mod)
                    compile(data.decode("utf-8"), mod, "exec")
                    tmp = os.path.join(here, mod + ".tmp")
                    with open(tmp, "wb") as f:
                        f.write(data)
                    os.replace(tmp, os.path.join(here, mod))
                    try:
                        os.makedirs(APP_DIR, exist_ok=True)
                        with open(os.path.join(APP_DIR, mod), "wb") as f:
                            f.write(data)
                    except Exception:
                        pass
                    got.append(mod)
                except Exception:
                    pass

            def finish():
                self._heal_busy = False
                if got:
                    self._deps_status = ("restored %s — restarting voice"
                                         % ", ".join(got))
                    try:
                        self._swap_voice_engine()
                    except Exception:
                        pass
                self._draw_frame()
            try:
                self.root.after(0, finish)
            except Exception:
                pass
        threading.Thread(target=work, daemon=True,
                         name="module-heal").start()

    def missing_voice_deps(self):
        out = []
        for mod, pkg in self.VOICE_DEPS:
            try:
                __import__(mod)
            except Exception:
                out.append((mod, pkg))
        return out

    def _ensure_python_deps(self):
        """Install any missing voice packages in the background, then
        restart the voice engine so they take effect. Runs on every
        launch; a no-op (and silent) once everything is present."""
        if getattr(self, "_deps_busy", False):
            return
        missing = self.missing_voice_deps()
        if not missing:
            self._deps_status = ""
            return
        if os.environ.get("DOSE_DISABLE_SELF_INSTALL"):
            return
        self._deps_busy = True
        self._deps_status = ("installing voice upgrades: %s"
                             % ", ".join(p for _, p in missing))
        self._draw_frame()

        def worker():
            done, failed = [], []
            total = len(missing)
            for i, (mod, pkg) in enumerate(missing, 1):
                # live progress so a long install never looks hung
                def show(msg=None, _i=i, _p=pkg):
                    self._deps_status = (msg or
                                         "installing %s  (%d of %d)…"
                                         % (_p, _i, total))
                    if self.mode in ("sysinfo", "btaudio", "settings"):
                        self._draw_frame()
                try:
                    self.root.after(0, show)
                except Exception:
                    pass
                okpkg = False
                t0 = time.time()
                for args in (["--break-system-packages", pkg], [pkg]):
                    try:
                        r = subprocess.run(
                            [sys.executable, "-m", "pip", "install",
                             "--no-input"] + args,
                            capture_output=True, text=True, timeout=1800)
                        if r.returncode == 0:
                            okpkg = True
                            break
                    except Exception:
                        pass
                secs = int(time.time() - t0)
                (done if okpkg else failed).append(pkg)
                try:
                    self.root.after(
                        0, show,
                        "%s %s in %ds  (%d of %d)" %
                        (pkg, "installed" if okpkg else "FAILED",
                         secs, i, total))
                except Exception:
                    pass
            try:
                __import__("importlib").invalidate_caches()
            except Exception:
                pass

            def finish():
                self._deps_busy = False
                if failed:
                    self._deps_status = ("could not install: %s"
                                         % ", ".join(failed[:3]))
                else:
                    self._deps_status = ("voice upgrades installed — "
                                         "fetching models…")
                # restart the engine so the new libraries are used
                try:
                    self._swap_voice_engine()
                except Exception:
                    pass
                self._draw_frame()
            try:
                self.root.after(0, finish)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True,
                         name="voice-deps").start()

    def _start_voice(self):
        self._ensure_audio_packages()
        # Bluetooth audio is retired: USB mic + USB speaker only.
        # (_ensure_bt_mic_config kept in the code but no longer run —
        # its user-service restarts could disrupt live USB audio.)
        self.settings["mic_device"] = "auto"
        try:
            from dose_voice import DoseVoice
        except Exception:
            # Self-heal: older updaters didn't know about
            # dose_voice.py — fetch it next to the app and retry once
            try:
                vdata = self._fetch_repo_file("dose_voice.py")
                vpath = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "dose_voice.py")
                with open(vpath, "wb") as f:
                    f.write(vdata)
                from dose_voice import DoseVoice
            except Exception:
                self.voice = None
                return
        try:
            self.voice = DoseVoice(self)
            if self.voice.available:
                self.voice.start()
            elif "microphone" in self.voice.reason:
                # no mic yet — watch for one being connected (USB or
                # Bluetooth) and start automatically when it appears
                if not getattr(self, "_voice_mic_watch", False):
                    self._voice_mic_watch = True

                    def re_probe():
                        self._voice_mic_watch = False
                        if (self.voice and not self.voice.available
                                and "microphone" in self.voice.reason
                                and self.settings.get("voice_enabled",
                                                      True)):
                            self._start_voice()
                            self._draw_frame()
                    self.root.after(15000, re_probe)
            elif self.voice.reason in ("audio library not installed",
                                       "vosk not installed",
                                       "piper not installed"):
                self._voice_self_install()
            elif self.voice.reason in ("speech model missing",
                                       "voice model missing"):
                self._voice_download_models()
        except Exception:
            self.voice = None

    # HER VOICE. One file. The station does not carry alternatives,
    # because a station that answers in a different voice than the one
    # it was built with is a bug, not a fallback.
    VOICE_NAME = "en_US-hfc_female-medium"
    VOICE_SUB = "en/en_US/hfc_female/medium"
    # more than one mirror ref, so a single bad path can't block her
    VOICE_REFS = ("v1.0.0", "main")

    def _retire_other_voices(self, vdir):
        """Delete every voice that is not hers, plus any speech clip
        rendered in one. Returns the names removed.

        This has to be its own step because of how the last update
        failed: the download and the cleanup both lived inside the
        'voice model missing' branch, so a station that ALREADY had the
        old Amy voice was considered fine and never ran either one. It
        kept the slow voice forever. This runs regardless of whether
        anything is missing."""
        import glob as _glob
        removed = []
        keep = self.VOICE_NAME
        for f in _glob.glob(os.path.join(vdir, "*.onnx")) + \
                _glob.glob(os.path.join(vdir, "*.onnx.json")):
            if keep in os.path.basename(f):
                continue
            try:
                os.unlink(f)
                removed.append(os.path.basename(f))
            except Exception:
                pass
        if removed:
            # clips rendered in the old voice would still play
            for f in _glob.glob(os.path.join(vdir, "cache", "*.wav")):
                try:
                    os.unlink(f)
                except Exception:
                    pass
        return removed

    def migrate_voice(self):
        """Run on EVERY launch: make sure the station is using her
        voice and nothing else. Cheap when there is nothing to do —
        a directory listing — and it is the only thing that can rescue
        a device already carrying an older voice.

        Any other voice is deleted ON SIGHT, before anything is
        downloaded and whether or not hers is present yet. This is
        safe because the engine will not speak in a voice it does not
        recognise in the first place: the probe reports "voice model
        missing" and the assistant never starts. So an old voice on
        disk is not a fallback that keeps the station talking — it is
        dead weight that can only cause confusion. The station is
        silent until HER voice arrives, which is the intended
        behaviour: better to say nothing than to say it in the wrong
        voice."""
        import glob as _glob
        vdir = os.path.expanduser("~/dose-home-station/voice")
        if not os.path.isdir(vdir):
            return
        try:
            mine = os.path.join(vdir, self.VOICE_NAME + ".onnx")
            gone = self._retire_other_voices(vdir)
            if gone:
                self._voice_migrated = gone
            if os.path.exists(mine):
                if gone:
                    self._swap_voice_engine()
                return
            # hers is not here yet — fetch her
            self._voice_download_models(force=True)
        except Exception:
            pass

    def _voice_download_models(self, force=False):
        """Missing speech/voice models: fetch them ourselves in the
        background (Vosk small ~40 MB, the fast hfc_female voice
        ~60 MB), then start the assistant. Pi only, once per boot."""
        if getattr(self, "_voice_downloading", False):
            return
        if force:
            self._voice_dl_tries = 0
        if not hasattr(self, "_voice_dl_tries"):
            self._voice_dl_tries = 0
        on_pi = (os.path.exists("/boot/config.txt")
                 or os.path.exists("/boot/firmware/config.txt"))
        if not on_pi or os.environ.get("DOSE_DISABLE_SELF_INSTALL"):
            return
        self._voice_downloading = True

        def fetch(url, path, min_bytes, magic=None):
            """Download and VALIDATE. A CDN that answers a bad path
            with an HTML error page must not leave a file on disk that
            looks like a model — that is how a station ends up mute
            with no explanation."""
            try:
                data = urlopen(url, timeout=600).read()
                if len(data) < min_bytes:
                    return False
                if magic and not data.startswith(magic):
                    return False
                if data.lstrip()[:15].lower().startswith(b"<!doctype html") \
                        or data.lstrip()[:6].lower() == b"<html>":
                    return False
                with open(path, "wb") as f:
                    f.write(data)
                return True
            except Exception:
                return False

        def worker():
            import glob as _glob
            import tempfile as _tf
            vdir = os.path.expanduser("~/dose-home-station/voice")
            try:
                os.makedirs(vdir, exist_ok=True)
            except Exception:
                pass

            if not _glob.glob(os.path.join(vdir, "vosk-model*")):
                fd, zpath = _tf.mkstemp(suffix=".zip")
                os.close(fd)
                if fetch("https://alphacephei.com/vosk/models/"
                         "vosk-model-small-en-us-0.15.zip",
                         zpath, 10_000_000):
                    try:
                        import zipfile
                        with zipfile.ZipFile(zpath) as z:
                            z.extractall(vdir)
                    except Exception:
                        pass
                try:
                    os.unlink(zpath)
                except Exception:
                    pass

            # HER VOICE — exactly one file, fetched by name. If some
            # other .onnx is sitting in the folder it does NOT count:
            # the station must sound like itself. ONNX files start with
            # the protobuf tag 0x08, which is how we tell a real model
            # from a CDN error page.
            onx = os.path.join(vdir, self.VOICE_NAME + ".onnx")
            if not os.path.exists(onx):
                for ref in self.VOICE_REFS:
                    base = ("https://huggingface.co/rhasspy/"
                            "piper-voices/resolve/%s/%s/"
                            % (ref, self.VOICE_SUB))
                    if (fetch(base + self.VOICE_NAME + ".onnx", onx,
                              10_000_000, magic=b"\x08")
                            and fetch(base + self.VOICE_NAME
                                      + ".onnx.json",
                                      onx + ".json", 500, magic=b"{")):
                        break
                    for _p in (onx, onx + ".json"):
                        try:
                            os.unlink(_p)
                        except Exception:
                            pass

            # Now that she is on disk, remove every other voice — and
            # every clip pre-rendered in one. Ordering matters: we
            # never take away the only voice the station has.
            if os.path.exists(onx):
                self._retire_other_voices(vdir)

            def finish():
                self._voice_downloading = False
                self._start_voice()
                self._draw_frame()
                # network hiccup? retry automatically, up to 5 times
                still_missing = (
                    self.voice is not None
                    and not self.voice.available
                    and "model missing" in self.voice.reason)
                if still_missing and self._voice_dl_tries < 5:
                    self._voice_dl_tries += 1
                    self.root.after(120000,
                                    self._voice_download_models)
            try:
                self.root.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _voice_self_install(self):
        """The audio libraries are missing even though the voice files
        are present (a past DOSE.sh run marked itself done while pip
        silently failed). Install them ourselves, once per boot, in
        the background — Pi only."""
        if getattr(self, "_voice_installing", False):
            return
        on_pi = (os.path.exists("/boot/config.txt")
                 or os.path.exists("/boot/firmware/config.txt"))
        if not on_pi or os.environ.get("DOSE_DISABLE_SELF_INSTALL"):
            return
        self._voice_installing = True

        def worker():
            for pkg in ("libportaudio2", "python3-srt",
                        "alsa-utils"):
                try:
                    subprocess.run(["sudo", "-n", "apt-get",
                                    "install", "-y", pkg],
                                   capture_output=True, timeout=300)
                except Exception:
                    pass
            for extra_env in (None, {"SETUPTOOLS_USE_DISTUTILS":
                                     "stdlib"}):
                env = dict(os.environ)
                if extra_env:
                    env.update(extra_env)
                try:
                    r = subprocess.run(
                        [sys.executable, "-m", "pip", "install",
                         "--break-system-packages", "sounddevice",
                         "vosk", "piper-tts", "qrcode"],
                        capture_output=True, timeout=900, env=env)
                    if r.returncode != 0:
                        subprocess.run(
                            [sys.executable, "-m", "pip", "install",
                             "sounddevice", "vosk", "piper-tts", "qrcode", "audioop-lts",
                             "rapidfuzz", "jellyfish", "moonshine-voice",
                             "faster-whisper", "useful-moonshine-onnx"],
                            capture_output=True, timeout=900, env=env)
                except Exception:
                    pass
                try:
                    __import__("importlib").invalidate_caches()
                    import sounddevice, vosk, piper  # noqa
                    break
                except Exception:
                    continue

            def finish():
                self._voice_installing = False
                self._start_voice()
                self._draw_frame()
            try:
                self.root.after(0, finish)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    # The app installs and downloads all of these ITSELF — the UPDATE
    # button now finishes the whole job, so telling the user to go and
    # run a shell script was both wrong and the only thing they could
    # not do from the screen in front of them.
    _VOICE_HINTS = {
        "audio library not installed": "installing…",
        "vosk not installed": "installing…",
        "piper not installed": "installing…",
        "speech model missing": "downloading…",
        "voice model missing": "downloading…",
        "no microphone detected": "plug the USB microphone in",
        "microphone failed to open": "reseat the USB microphone",
    }

    def _voice_status_text(self):
        if getattr(self, "_voice_installing", False):
            return "Voice: installing audio components…"
        if getattr(self, "_voice_downloading", False):
            return "Voice: downloading speech models… (a few minutes)"
        if self.voice is None:
            return "Voice: starting…"
        if not self.voice.available:
            hint = self._VOICE_HINTS.get(self.voice.reason)
            if hint:
                return f"Voice: {self.voice.reason} — {hint}"
            return "Voice: " + self.voice.reason
        if not self.settings.get("voice_enabled", True):
            return "Voice: off"
        return ""

    def _voice_mic_subtext(self):
        if getattr(self, "_mic_testing", False):
            return "Testing mic — say something…"
        # The live meter's peak is the only real proof the mic hears;
        # a fresh non-zero peak overrides an older 'silent' verdict.
        live = bool(getattr(self, "_meter_peak", 0) > 40)
        r = getattr(self, "_mic_test_result", None)
        if r is not None and not (live and r <= 5):
            if r > 60:
                verdict = "mic is LIVE (tap to retest)"
            elif r > 5:
                verdict = "quiet — speak louder, retest"
            else:
                verdict = (getattr(self, "_mic_diag", None)
                           or "NO SIGNAL — mic not working")
            return f"Mic {r}: {verdict}"
        if self.voice and self.voice.available:
            return f"Mic: {self.voice.mic_name} · tap row to test"
        return ""

    # ══════════════════════════════════════════════════════════════════
    #  VOICE & BLUETOOTH SCREEN — pair headsets from the touchscreen
    # ══════════════════════════════════════════════════════════════════
    def _bt_state(self):
        if not hasattr(self, "_bt"):
            self._bt = {"status": "", "devices": [], "busy": False}
        return self._bt

    @staticmethod
    def _btctl(*args, timeout=12):
        return subprocess.run(["bluetoothctl"] + list(args),
                              capture_output=True, text=True,
                              timeout=timeout)

    def _bt_refresh(self, scan=False):
        bt = self._bt_state()
        if bt["busy"]:
            return
        bt["busy"] = True
        bt["status"] = ("Scanning… put your AirPods in pairing mode "
                        "(open lid, hold the case button)"
                        if scan else "Reading Bluetooth devices…")
        self._draw_frame()

        def worker():
            devices = []
            try:
                if scan:
                    subprocess.run(["bluetoothctl", "--timeout", "10",
                                    "scan", "on"],
                                   capture_output=True, timeout=20)
                paired = set()
                for listing in (["devices", "Paired"],
                                ["paired-devices"]):
                    try:
                        out = self._btctl(*listing).stdout
                        for ln in out.splitlines():
                            p = ln.split(None, 2)
                            if len(p) >= 2 and p[0] == "Device":
                                paired.add(p[1])
                    except Exception:
                        continue
                out = self._btctl("devices").stdout
                seen = set()
                for ln in out.splitlines():
                    p = ln.split(None, 2)
                    if len(p) < 3 or p[0] != "Device":
                        continue
                    mac, name = p[1], p[2]
                    if mac in seen or name.replace("-", ":") == mac:
                        continue
                    seen.add(mac)
                    connected = False
                    try:
                        info = self._btctl("info", mac).stdout
                        connected = "Connected: yes" in info
                    except Exception:
                        pass
                    devices.append({"mac": mac, "name": name,
                                    "paired": mac in paired,
                                    "connected": connected})
                devices.sort(key=lambda d: (not d["connected"],
                                            not d["paired"]))
                status = ("" if devices else
                          "No devices found — tap SCAN with your "
                          "headset in pairing mode")
            except FileNotFoundError:
                status = "Bluetooth tools not available on this system"
            except Exception:
                status = "Bluetooth scan failed — try again"

            def done():
                bt["busy"] = False
                bt["devices"] = devices
                bt["status"] = status
                if self.mode == "btaudio":
                    self._draw_frame()
            try:
                self.root.after(0, done)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _bt_action(self, mac, action):
        bt = self._bt_state()
        if bt["busy"]:
            return
        bt["busy"] = True
        bt["status"] = {"pair": "Pairing… keep the AirPods case "
                                "button held",
                        "connect": "Connecting…",
                        "remove": "Forgetting device…"}[action]
        self._draw_frame()

        def worker():
            ok = False
            try:
                if action == "pair":
                    r1 = self._btctl("pair", mac, timeout=35)
                    self._btctl("trust", mac)
                    r2 = self._btctl("connect", mac, timeout=25)
                    ok = ("successful" in (r1.stdout + r2.stdout).lower()
                          or r2.returncode == 0)
                elif action == "connect":
                    r = self._btctl("connect", mac, timeout=25)
                    ok = r.returncode == 0 or "successful" in                         r.stdout.lower()
                else:
                    r = self._btctl("remove", mac, timeout=20)
                    ok = r.returncode == 0
            except Exception:
                ok = False

            def done():
                bt["busy"] = False
                bt["status"] = ("Done. Run the mic test." if ok else
                                "That didn't work — make sure the "
                                "device is in pairing mode and try "
                                "again")
                self._bt_refresh(scan=False)
            try:
                self.root.after(0, done)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _draw_bt_audio(self, c):
        t = self.theme
        bt = self._bt_state()
        card_img = _pil_rounded_rect(620, 440, 22, t["card_bg"])
        tk_card = self._get_tk_image("bta_card", card_img)
        c.create_image(26, 20, image=tk_card, anchor="nw")
        c.create_text(56, 44, text="VOICE & AUDIO",
                      font=self.font_label, fill=t["muted"],
                      anchor="nw")

        mic_line = self._voice_status_text() or             self._voice_mic_subtext() or "Voice ready."
        c.create_text(56, 66,
                      text=self._fit_text(mic_line,
                                          self.font_small, 556),
                      font=self.font_small, fill=t["fg"], anchor="nw")
        status_line = bt["status"] or getattr(
            self, "_audio_pkg_status", "")
        if status_line:
            # width= wraps instead of truncating — a verdict must
            # never be cut off mid-sentence
            c.create_text(56, 84, text=status_line,
                          font=self.font_small, fill=DOSE_BLUE_LT,
                          anchor="nw", width=556)

        # ── zero-setup: what's plugged in is what's used ──────────
        y = 122
        v = self.voice
        mic_name = (getattr(v, "mic_name", None) or
                    "searching for USB microphone…") if v else \
            "voice engine starting…"
        out_cache = getattr(v, "_out_cache", None) if v else None
        spk_name = (out_cache and out_cache[1]) or \
            "system default (press TEST SPKR)"
        for p in ("alsa_output.", "alsa_input."):
            if str(spk_name).startswith(p):
                spk_name = str(spk_name)[len(p):]
        # MICROPHONE row (tap → live meter + mic picker)
        row_img = _pil_rounded_rect(560, 52, 12, t["elevated_bg"])
        tk_row = self._get_tk_image("bta_dev_mic", row_img)
        c.create_image(56, y, image=tk_row, anchor="nw")
        c.create_text(72, y + 14, text="MICROPHONE  (tap to pick + test)",
                      font=self.font_label, fill=t["muted"], anchor="w")
        c.create_text(72, y + 36,
                      text=self._fit_text(str(mic_name),
                                          self.font_small_bold, 520),
                      font=self.font_small_bold, fill=t["fg"], anchor="w")
        self._click_zones.append((56, y, 616, y + 52,
                                  self._open_mic_meter))
        y += 62

        # SPEAKER — tappable list of every output; tap selects + tests
        # ── VOICE MODELS: proof on the device that the free upgraded
        #    models actually downloaded (or are still coming).
        try:
            rows = v.model_status() if v else []
        except Exception:
            rows = []
        try:
            mods = self.module_report()
            mtxt = "  ·  ".join(
                "%s %s" % (m.replace(".py", ""),
                           b if ok_ else "MISSING")
                for m, ok_, b in mods)
            allmods = all(ok_ for _, ok_, _ in mods)
            c.create_text(56, y,
                          text=self._fit_text("FILES: " + mtxt,
                                              self.font_tiny, 556),
                          font=self.font_tiny,
                          fill="#2ECC71" if allmods else "#FF6B6B",
                          anchor="nw")
            y += 16
        except Exception:
            pass
        miss = []
        try:
            miss = self.missing_voice_deps()
        except Exception:
            miss = []
        dstat = getattr(self, "_deps_status", "")
        if miss or dstat:
            msg = dstat or ("upgrade libraries missing: %s"
                            % ", ".join(p for _, p in miss))
            c.create_text(56, y, text=self._fit_text(msg,
                                                     self.font_tiny, 556),
                          font=self.font_tiny,
                          fill="#F1C40F" if miss else "#2ECC71",
                          anchor="nw")
            y += 16
        if rows:
            parts = []
            for label, ok_, detail in rows[:4]:
                mark = "OK" if ok_ else ("…" if ok_ is None else "X")
                short = label.split(" (")[0]
                parts.append("%s %s" % (short, mark))
            allok = all(r[1] for r in rows)
            c.create_text(56, y,
                          text=self._fit_text(
                              "MODELS: " + "  ·  ".join(parts),
                              self.font_tiny, 556),
                          font=self.font_tiny,
                          fill="#2ECC71" if allok else DOSE_BLUE_LT,
                          anchor="nw")
            y += 18

        c.create_text(56, y, text="SPEAKER  (tap one to use + hear it)",
                      font=self.font_label, fill=t["muted"], anchor="nw")
        y += 22
        outs = []
        try:
            outs = v.list_output_devices() if v else []
        except Exception:
            outs = []
        cur_sink = getattr(v, "_forced_sink", None) if v else None
        if not outs:
            c.create_text(64, y, text="(no output devices found)",
                          font=self.font_tiny, fill="#FF6B6B", anchor="nw")
            y += 20
        for sink, short in outs[:3]:
            sel = (sink == cur_sink)
            r_img = _pil_rounded_rect(560, 30, 9,
                                      DOSE_BLUE if sel else t["btn_bg"])
            tkr = self._get_tk_image(f"spk_{abs(hash(sink)) % 99999}",
                                     r_img)
            c.create_image(56, y, image=tkr, anchor="nw")
            c.create_text(72, y + 15,
                          text=self._fit_text(short, self.font_small_bold,
                                              520),
                          font=self.font_small_bold,
                          fill="#06101E" if sel else t["fg"], anchor="w")
            self._click_zones.append(
                (56, y, 616, y + 30,
                 lambda sk=sink: self._pick_speaker(sk)))
            y += 34

        # bottom actions
        r = getattr(self, "_mic_test_result", None)
        show_report = (r is not None and r <= 5
                       and getattr(self, "_mic_report_lines", None))
        buttons = [("MIC LEVEL", self._open_mic_meter, False),
                   # read a few lines and it tunes itself to this room
                   ("TUNE ROOM", self._open_calibrate, False),
                   ("AUDIT", self._open_audit, False),
                   ("TEST SPKR", self._speaker_test, False),
                   ("RESCAN", self._audio_rescan, False)]
        if show_report:
            buttons.append(("DETAILS", self._open_mic_report, False))
        buttons.append(("CLOSE", self._bt_close, True))
        # fit the row to the card, whatever it holds (56..646)
        bw = max(72, min(108, (590 - 8 * (len(buttons) - 1))
                         // len(buttons)))
        bx = 56
        for label, cb, primary in buttons:
            b_img = _pil_rounded_rect(bw, 44, 14, DOSE_BLUE if primary
                                      else t["elevated_bg"])
            tk_b = self._get_tk_image(f"bta_btn_{label}", b_img)
            c.create_image(bx, 400, image=tk_b, anchor="nw")
            c.create_text(bx + bw // 2, 422, text=label,
                          font=self.font_small_bold,
                          fill="#06101E" if primary else t["fg"],
                          anchor="center")
            self._click_zones.append((bx, 400, bx + bw, 444, cb))
            bx += bw + 8

    def _speaker_test(self):
        """Play a spoken test line and report which output path
        carried it — answers 'can I hear anything?' directly."""
        bt = self._bt_state()
        if bt["busy"]:
            return
        bt["busy"] = True
        bt["status"] = "Playing speaker test — listen…"
        self._draw_frame()

        def worker():
            method = False
            try:
                if self.voice:
                    method = self.voice.speaker_test()
                else:
                    r = subprocess.run(
                        ["aplay", "-q",
                         "/usr/share/sounds/alsa/Front_Center.wav"],
                        capture_output=True, timeout=30)
                    method = ("system default (aplay)"
                              if r.returncode == 0 else False)
            except Exception:
                method = False

            def done():
                bt["busy"] = False
                bt["status"] = (
                    f"Speaker played via {method} — did you hear "
                    "the voice? If not, the wrong output is selected."
                    if method else
                    "Speaker playback FAILED — no working output "
                    "device found.")
                if self.mode == "btaudio":
                    self._draw_frame()
            try:
                self.root.after(0, done)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _set_mic_device(self, value):
        self.settings["mic_device"] = value
        self._save_config()
        self._mic_test_result = None
        if self.voice:
            self.voice.request_reopen()
        self._draw_frame()

    # ── LIVE MIC LEVEL METER ────────────────────────────────────────────
    def _open_mic_meter(self):
        """Open a live, moving level meter so you can SEE, in real
        time, exactly how much the microphone is picking up as you
        speak. raw = the mic's physical signal (0 = it hears nothing);
        boosted = what the recognizer receives after auto-gain."""
        self._prev_mode = self.mode
        self.mode = "micmeter"
        self._meter_raw = 0
        self._meter_boost = 0
        self._meter_peak = 0
        self._meter_run = True

        # Worker thread ONLY samples and updates shared values (never
        # touches tk). A main-thread `after` loop does all drawing —
        # the thread-safe pattern.
        def worker():
            while (getattr(self, "_meter_run", False)
                   and self.mode == "micmeter"):
                raw, boost = (self.voice.mic_meter_sample(0.2)
                              if self.voice else (0, 0))
                self._meter_raw = raw
                self._meter_boost = boost
                self._meter_peak = max(getattr(self, "_meter_peak", 0),
                                       raw)
        threading.Thread(target=worker, daemon=True).start()
        self._meter_tick()

    def _meter_tick(self):
        if not getattr(self, "_meter_run", False) \
                or self.mode != "micmeter":
            return
        self._draw_frame()
        try:
            self.root.after(120, self._meter_tick)
        except Exception:
            pass

    def _close_mic_meter(self):
        self._meter_run = False
        self.mode = "btaudio"
        self._draw_frame()

    # ── AUDIT PAGE ───────────────────────────────────────────────────
    # One screen, everything on it, nothing cut off — made to be
    # photographed and sent to me. Every number here is measured on
    # THIS device: the real cost of the last turn stage by stage, what
    # the microphone is actually getting, which models are loaded, and
    # what the Pi is doing. Guessing at a slow assistant from a
    # description is how we went round in circles; this replaces the
    # description with data.
    def _open_audit(self):
        # a stick may have been plugged in since last time
        try:
            if not self._audit_token():
                pass
        except Exception:
            pass
        # and is this device even running current code?
        try:
            self._audit_build_check()
        except Exception:
            pass
        # Don't record "audit" as the screen to go back to — opening it
        # twice would trap CLOSE on this page.
        if self.mode != "audit":
            self._prev_mode = self.mode
        self.mode = "audit"
        self._audit_tick()
        self._draw_frame()

    def _close_audit(self):
        self.mode = getattr(self, "_prev_mode", "settings") or "settings"
        self._draw_frame()

    def _audit_tick(self):
        if self.mode != "audit":
            return
        self._draw_frame()
        self.root.after(1000, self._audit_tick)

    def _audit_build_check(self):
        """Is this device running what is on GitHub?

        The whole loop was going round twice because the audit was
        being read from a device that had never taken the update — the
        numbers described code I had already fixed. This says so, on
        the page, at the top."""
        if getattr(self, "_build_check_running", False):
            return
        self._build_check_running = True

        def work():
            try:
                data = self._fetch_repo_file("dose_app.py", timeout=15)
                import hashlib
                self._remote_build = hashlib.md5(data).hexdigest()[:7]
            except Exception:
                self._remote_build = None
            finally:
                self._build_check_running = False

        threading.Thread(target=work, daemon=True,
                         name="audit-build").start()

    def _audit_sections(self):
        """(heading, [(label, value, ok)]) for everything worth seeing."""
        v = self.voice
        out = []

        # 0. IS THIS DEVICE UP TO DATE? Everything below describes the
        #    code that is actually running, which is worthless if that
        #    is not the code being fixed.
        local = getattr(self, "_build_id", "?")
        remote = getattr(self, "_remote_build", None)
        if remote is None:
            out.append(("VERSION", [("build", local, True),
                                    ("github", "checking…", True)]))
        elif remote == local:
            out.append(("VERSION", [("build", local, True),
                                    ("github", "up to date", True)]))
        else:
            out.append(("*** OUT OF DATE ***", [
                ("this device", local, False),
                ("github has", remote, False),
                ("ACTION", "press UPDATE in Settings", False),
                ("", "numbers below are from OLD code", False),
            ]))

        # 1. the last turn, measured
        try:
            rows = v.turn_report() if v else []
        except Exception as e:
            rows = [("turn report failed", str(e)[:30], False, "")]
        out.append(("LAST TURN (measured on this device)",
                    [(a, b, c) for a, b, c, _d in rows]))

        # 1b. how it has been doing OVERALL, not just last turn
        try:
            sm = v.turn_log_summary() if v else {"turns": 0}
        except Exception:
            sm = {"turns": 0}
        if sm.get("turns"):
            n = sm["turns"]
            miss = sm.get("not_understood", 0)
            out.append(("LAST %d TURNS" % n, [
                ("understood", "%d of %d" % (n - miss, n),
                 miss * 3 <= n),
                ("NOT understood", "%d" % miss, miss == 0),
                ("heard nothing", "%d" % sm.get("heard_nothing", 0),
                 sm.get("heard_nothing", 0) == 0),
                ("distorted audio", "%d" % sm.get("distorted", 0),
                 sm.get("distorted", 0) == 0),
                ("engines disagreed", "%d"
                 % sm.get("engines_disagreed", 0), True),
                ("slower than 1.5s", "%d" % sm.get("slow", 0),
                 sm.get("slow", 0) == 0),
                ("typical", "%.2f s" % sm.get("p50", 0),
                 sm.get("p50", 0) < 1.5),
                ("worst", "%.2f s" % sm.get("worst", 0),
                 sm.get("worst", 0) < 3.0),
            ]))
        else:
            out.append(("LAST TURNS",
                        [("no turns logged yet", "say something", True)]))

        # 1c. WHAT WAS ACTUALLY SAID. Counts tell me how often it is
        #     failing; only the words tell me why. "1 of 8 understood"
        #     could be a microphone problem, a recogniser problem or a
        #     vocabulary problem, and these three lines separate them
        #     in a photograph.
        try:
            recent = (v.turn_log(4) if v else [])[-4:]
        except Exception:
            recent = []
        if recent:
            rows = []
            for r in reversed(recent):
                heard = (r.get("heard") or "").strip()
                if not heard:
                    rows.append(("(heard nothing)",
                                 "%ss" % r.get("secs", "?"), False))
                    continue
                rows.append(('"%s"' % heard[:22],
                             r.get("intent", "?")[:12],
                             bool(r.get("understood"))))
            out.append(("WHAT YOU SAID (newest first)", rows))

        # 2. what the microphone is getting
        mic = []
        try:
            mic.append(("room", v._room_note(), v._nfloor < 400))
            mic.append(("voice level", "%.0f" % v._speech_level,
                        800 < v._speech_level < 6000))
            mic.append(("boost", "%.2fx" % v._agc_ceiling(),
                        v._agc_ceiling() <= 2.0))
            mic.append(("detector", v._vad_note(),
                        "listening" in v._vad_note()))
            cal = getattr(v, "_cal_profile", None)
            mic.append(("calibrated", "yes — gate %.0f" % cal["gate"]
                        if cal else "NO — run TUNE ROOM", bool(cal)))
            mic.append(("device", (v.mic_name or "?")[:26]
                        if hasattr(v, "mic_name") else "-", True))
        except Exception as e:
            mic.append(("unavailable", str(e)[:28], False))
        out.append(("MICROPHONE", mic))

        # 3. models
        mods = []
        try:
            for label, ok_, detail in (v.model_status() if v else []):
                mods.append((label, (detail or "")[:26], bool(ok_)))
        except Exception:
            pass
        try:
            import dose_voice as _dv
            mods.append(("wake word",
                         "on (costs CPU)" if _dv.WAKE_WORD else "off",
                         True))
            mods.append(("endpoint", "%.2fs / %.1fs"
                         % (_dv.ENDPOINT_STABLE, _dv.ENDPOINT_DANGLING),
                         True))
        except Exception:
            pass
        out.append(("MODELS", mods))

        # 4. the machine
        hw = []
        try:
            for label, detail, good in (v.hardware_report() if v else []):
                hw.append((label, str(detail)[:26], bool(good)))
        except Exception as e:
            hw.append(("unavailable", str(e)[:26], False))
        out.append(("RASPBERRY PI", hw))

        # 5. what is installed
        files = [("dose_app.py", getattr(self, "_build_id", "?"), True)]
        try:
            for m, ok_, bid in self.module_report():
                files.append((m, bid if ok_ else "MISSING", bool(ok_)))
        except Exception:
            pass
        try:
            missing = dict(self.missing_voice_deps())
        except Exception:
            missing = {}
        for mod_name, pkg in self.VOICE_DEPS:
            files.append((pkg, "ok" if mod_name not in missing
                          else "MISSING", mod_name not in missing))
        out.append(("INSTALLED", files))
        return out

    # Where a token lives, if the user chooses to add one. NEVER in
    # the repository — this file is public, and a committed token is a
    # token that has to be revoked.
    AUDIT_TOKEN_PATHS = (
        "~/dose-home-station/github_token",
        "~/.dose_github_token",
    )

    def audit_report_text(self):
        """The audit as plain text — what gets saved and posted."""
        from datetime import datetime as _dt
        lines = ["DOSE Home Station — audit",
                 _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "build %s" % getattr(self, "_build_id", "?"), ""]
        for heading, rows in self._audit_sections():
            lines.append("## %s" % heading)
            for label, value, good in rows:
                lines.append("  %-22s %-28s %s"
                             % (label, value, "" if good else "<-- CHECK"))
            lines.append("")

        # EVERY TURN. This is the part worth reading: what was said,
        # what it heard, whether it worked it out, and how long each
        # one took. A run of bad turns can be read here instead of
        # described.
        lines.append("## EVERY TURN (newest last)")
        lines.append("  Each turn shows what EACH recogniser produced,")
        lines.append("  what the language layer made of it, and what")
        lines.append("  the microphone was actually getting.")
        lines.append("")
        try:
            rows = self.voice.turn_log(40) if self.voice else []
        except Exception as e:
            rows = []
            lines.append("  (log unavailable: %s)" % str(e)[:40])
        if not rows:
            lines.append("  (nothing logged yet)")
        for r in rows:
            mark = "OK " if r.get("understood") else "BAD"
            lines.append("  [%s] %s  total %ss (end %s fast %s slow %s)"
                         % (mark, r.get("at", "?"), r.get("total", "?"),
                            r.get("endpoint", "?"), r.get("fast", "?"),
                            r.get("slow", "?")))
            lines.append("       audio : %.1fs  room %s  voice %s  "
                         "peak %s  clip %s%%  snr %s"
                         % (r.get("secs", 0) or 0, r.get("room", "?"),
                            r.get("voice", "?"), r.get("peak", "?"),
                            r.get("clip_pct", "?"), r.get("snr", "?")))
            lines.append("       live  : %r" % (r.get("vosk") or "")[:56])
            lines.append("       fast  : %r"
                         % (r.get("fast_text") or "")[:56])
            if r.get("slow_text"):
                lines.append("       slow  : %r"
                             % (r.get("slow_text") or "")[:56])
            lines.append("       USED  : %r  (%s)"
                         % ((r.get("heard") or "")[:56],
                            r.get("engine", "?")))
            lines.append("       intent: %s" % r.get("intent", "?"))
            lines.append("       said  : %s" % (r.get("reply") or "")[:66])
            lines.append("")
        lines.append("")
        return "\n".join(lines)

    # Where a USB stick gets mounted on Raspberry Pi OS.
    USB_ROOTS = ("/media", "/mnt", "/run/media")
    TOKEN_FILENAMES = ("github_token", "dose_github_token",
                       "github_token.txt", "dose_token.txt")

    def import_usb_token(self):
        """Pick up a token from a USB stick, automatically.

        Typing a 93-character token on a touchscreen keyboard is
        miserable, and there is no reason to: drop a file called
        github_token on any USB stick, plug it in, and the station
        copies it to its own storage with the permissions locked down.
        Returns the token if one was imported."""
        import glob as _g
        pats = []
        for root in self.USB_ROOTS:
            for name in self.TOKEN_FILENAMES:
                pats.append(os.path.join(root, "*", name))
                pats.append(os.path.join(root, "*", "*", name))
        for pat in pats:
            for path in _g.glob(pat):
                try:
                    tok = open(path).read().strip()
                except Exception:
                    continue
                if not self._looks_like_token(tok):
                    continue
                try:
                    dest = os.path.join(APP_DIR, "github_token")
                    os.makedirs(APP_DIR, exist_ok=True)
                    with open(dest, "w") as f:
                        f.write(tok)
                    os.chmod(dest, 0o600)
                    self._audit_status = (
                        "token imported from USB — you can remove the "
                        "stick")
                    return tok
                except Exception:
                    continue
        return None

    @staticmethod
    def _looks_like_token(tok):
        """A GitHub token, roughly. Catches an empty or obviously
        wrong file before it is stored and used."""
        tok = (tok or "").strip()
        if len(tok) < 20 or len(tok) > 255 or " " in tok:
            return False
        return tok.startswith(("ghp_", "github_pat_", "gho_", "ghs_"))

    def _audit_token(self):
        for cand in self.AUDIT_TOKEN_PATHS:
            try:
                path = os.path.expanduser(cand)
                if os.path.isfile(path):
                    tok = open(path).read().strip()
                    if tok:
                        return tok
            except Exception:
                continue
        # nothing stored — is there a stick plugged in?
        return self.import_usb_token()

    def _post_audit(self):
        """Save the audit, and post it to GitHub if a token is present.

        A token is never shipped in this repository — the repository is
        public, and a committed credential is a revoked credential. The
        report is ALWAYS written to disk either way, so it can be
        recovered even with no network and no token."""
        self._audit_status = "saving…"
        self._draw_frame()

        def work():
            text = ""
            try:
                text = self.audit_report_text()
                path = os.path.join(
                    os.path.expanduser("~/dose-home-station"),
                    "audit-latest.txt")
                with open(path, "w") as f:
                    f.write(text)
                saved = "saved to %s" % path
            except Exception as e:
                saved = "could not save: %s" % str(e)[:30]

            tok = self._audit_token()
            if not tok:
                self._audit_done(saved)
                return
            try:
                import urllib.request
                body = json.dumps({
                    "title": "Audit — build %s"
                             % getattr(self, "_build_id", "?"),
                    "body": "```\n" + text[:60000] + "\n```",
                }).encode()
                req = urllib.request.Request(
                    "https://api.github.com/repos/%s/issues" % REPO,
                    data=body, method="POST",
                    headers={"Authorization": "Bearer " + tok,
                             "Accept": "application/vnd.github+json",
                             "User-Agent": "dose-station",
                             "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    num = json.loads(r.read().decode()).get("number")
                self._audit_done("posted to GitHub as issue #%s" % num)
            except Exception as e:
                self._audit_done("%s · post failed: %s"
                                 % (saved, str(e)[:40]))

        threading.Thread(target=work, daemon=True,
                         name="audit-post").start()

    def _audit_done(self, msg):
        """Record the outcome. Called ON THE WORKER THREAD, so it does
        NOT touch Tk — setting an attribute is safe, calling into the
        widget tree from another thread is not, and Tkinter raises
        "main thread is not in main loop" when it is. The audit page
        already refreshes once a second and will show this on its next
        pass."""
        self._audit_status = msg

    def _draw_audit(self, c):
        t = self.theme
        c.create_image(26, 12, image=self._get_tk_image(
            "audit_card", _pil_rounded_rect(620, 456, 20, t["card_bg"])),
            anchor="nw")
        c.create_text(42, 22, text="AUDIT — photograph this",
                      font=self.font_label, fill=DOSE_BLUE_LT,
                      anchor="nw")

        sections = self._audit_sections()
        # Two columns, and the font shrinks to fit rather than letting
        # anything run off the card. The whole point is that nothing
        # is cut off.
        col_x = (42, 340)
        col_w = 280
        y = [40, 40]
        col = 0
        for heading, rows in sections:
            # Stop well clear of the footer. The device photo showed
            # the "saved to …" line running straight through the
            # INSTALLED column because the columns were allowed to
            # grow into the same space the footer uses.
            need = 14 + 12 * len(rows)
            if y[col] + need > 376 and col == 0:
                col = 1
            if y[col] + need > 376 and col == 1:
                break
            c.create_text(col_x[col], y[col], text=heading,
                          font=self.font_tiny, fill=t["muted"],
                          anchor="nw")
            y[col] += 14
            for label, value, good in rows:
                line = "%s  %s" % (label, value)
                c.create_text(
                    col_x[col] + 4, y[col],
                    text=self._fit_text(line, self.font_tiny, col_w),
                    font=self.font_tiny,
                    fill=("#2ECC71" if good else "#F1C40F"),
                    anchor="nw")
                y[col] += 12
            y[col] += 6

        # Say plainly whether posting is possible, and if not, the
        # exact steps — on the screen in front of you, not in a
        # README you would have to go and find.
        have_tok = False
        try:
            have_tok = bool(self._audit_token())
        except Exception:
            pass
        if have_tok:
            note = "GitHub: ready — SEND TO GITHUB posts this as an issue"
            note_col = "#2ECC71"
        else:
            note = ("No token: put one in 'github_token' on a USB "
                    "stick (github.com/settings/personal-access-tokens, "
                    "Issues:write)")
            note_col = "#F1C40F"
        # Footer: the token note, then the last action. Both single
        # lines, both fitted to the card, neither allowed to wrap into
        # the columns above.
        c.create_text(42, 382,
                      text=self._fit_text(note, self.font_tiny, 590),
                      font=self.font_tiny, fill=note_col, anchor="nw")

        status = getattr(self, "_audit_status", "")
        if status:
            c.create_text(42, 396, text=self._fit_text(
                status, self.font_tiny, 590), font=self.font_tiny,
                fill=DOSE_BLUE_LT, anchor="nw")

        for label, x0, cb in (("REFRESH", 42, self._draw_frame),
                              ("SEND TO GITHUB", 222, self._post_audit),
                              ("CLOSE", 496, self._close_audit)):
            c.create_image(x0, 418, image=self._get_tk_image(
                "audit_%s" % label, _pil_rounded_rect(
                    150, 40, 12, DOSE_BLUE if label == "CLOSE"
                    else t["elevated_bg"])), anchor="nw")
            c.create_text(x0 + 75, 438, text=label,
                          font=self.font_small_bold,
                          fill="#06101E" if label == "CLOSE" else t["fg"],
                          anchor="center")
            self._click_zones.append((x0, 418, x0 + 150, 458, cb))

    # ── ROOM CALIBRATION SCREEN ──────────────────────────────────────
    def _open_calibrate(self):
        if self.mode != "calibrate":
            self._prev_mode = self.mode
        self.mode = "calibrate"
        self._cal_line = 0
        try:
            if self.voice:
                self.voice.start_calibration()
        except Exception:
            pass
        self._calibrate_tick()
        self._draw_frame()

    def _close_calibrate(self):
        try:
            if self.voice:
                self.voice.cancel_calibration()
        except Exception:
            pass
        self.mode = getattr(self, "_prev_mode", "settings") or "settings"
        self._draw_frame()

    def _calibrate_tick(self):
        if self.mode != "calibrate":
            return
        self._draw_frame()
        self.root.after(120, self._calibrate_tick)

    def _draw_calibrate(self, c):
        """Teach the station this room and this voice.

        Every threshold before this was a number I picked: how far
        above the room speech has to sit, what counts as a healthy
        level, how much to amplify. None of them know how far away you
        sit, how loud you talk, or what your kitchen sounds like. Here
        it measures all three from you, once, and uses that instead."""
        t = self.theme
        c.create_image(26, 20, image=self._get_tk_image(
            "cal_card", _pil_rounded_rect(620, 440, 22, t["card_bg"])),
            anchor="nw")
        c.create_text(56, 36, text="TUNE TO THIS ROOM",
                      font=self.font_label, fill=t["muted"], anchor="nw")

        phase, level, note = ("idle", 0.0, "")
        try:
            if self.voice:
                phase, level, note = self.voice.calibration_state()
        except Exception:
            pass

        lines = []
        try:
            lines = list(self.voice.CALIBRATION_LINES) if self.voice else []
        except Exception:
            pass

        if phase == "room":
            head, colour = "Listening to the room…", t["fg"]
            sub = "Stay quiet for a moment."
        elif phase == "voice":
            head, colour = "Now read this out loud:", DOSE_BLUE_LT
            sub = lines[0] if lines else "Hello."
        elif phase == "done":
            head, colour = "Done — tuned to this room.", "#2ECC71"
            sub = note
        elif phase == "failed":
            head, colour = "I could not hear you.", "#F1C40F"
            sub = note
        else:
            head, colour = "Read a few lines and I'll tune to you.", \
                t["fg"]
            sub = ("I'll measure how loud the room is and how loud your "
                   "voice is from where you sit, then set my levels "
                   "from that instead of guessing.")

        c.create_text(323, 78, text=head, font=self.font_title,
                      fill=colour, anchor="n", width=560)
        c.create_text(323, 118, text=sub, font=self.font_small,
                      fill=t["muted"] if phase != "voice" else t["fg"],
                      anchor="n", width=540)

        # live level bar — seeing it move is what tells you it is
        # actually hearing you
        bx, bw, bh = 56, 560, 30
        by = 208
        c.create_image(bx, by, image=self._get_tk_image(
            "cal_bar_bg", _pil_rounded_rect(bw, bh, 10,
                                            t["elevated_bg"])),
            anchor="nw")
        frac = max(0.0, min(1.0, float(level) / 6000.0))
        if frac > 0.01:
            c.create_image(bx, by, image=self._get_tk_image(
                "cal_bar_%02d" % int(frac * 40),
                _pil_rounded_rect(_qw(bw * frac, 8, 8), bh, 10,
                                  DOSE_BLUE)), anchor="nw")
        c.create_text(323, by + bh + 12, text="%d" % int(level),
                      font=self.font_tiny, fill=t["muted"], anchor="n")

        prof = getattr(self.voice, "_cal_profile", None) if self.voice \
            else None
        if prof:
            c.create_text(
                323, 286,
                text="saved: voice %.0f · room %.0f · gate %.0f · "
                     "boost %.1fx" % (prof.get("voice", 0),
                                      prof.get("floor", 0),
                                      prof.get("gate", 0),
                                      prof.get("gain", 1.0)),
                font=self.font_tiny, fill="#2ECC71", anchor="n",
                width=560)

        for label, x0, cb in (
                ("START" if phase in ("idle", "done", "failed")
                 else "LISTENING…", 56, self._restart_calibration),
                ("CLOSE", 466, self._close_calibrate)):
            c.create_image(x0, 400, image=self._get_tk_image(
                "cal_%s" % label, _pil_rounded_rect(
                    150, 44, 14, DOSE_BLUE if label == "CLOSE"
                    else t["elevated_bg"])), anchor="nw")
            c.create_text(x0 + 75, 422, text=label,
                          font=self.font_small_bold,
                          fill="#06101E" if label == "CLOSE" else t["fg"],
                          anchor="center")
            self._click_zones.append((x0, 400, x0 + 150, 444, cb))

    def _restart_calibration(self):
        try:
            if self.voice:
                self.voice.start_calibration()
        except Exception:
            pass
        self._draw_frame()

    def _draw_mic_meter(self, c):
        t = self.theme
        card_img = _pil_rounded_rect(620, 440, 22, t["card_bg"])
        tk_card = self._get_tk_image("meter_card", card_img)
        c.create_image(26, 20, image=tk_card, anchor="nw")
        c.create_text(56, 36, text="LIVE MIC LEVEL — tap a mic, then speak",
                      font=self.font_label, fill=t["muted"], anchor="nw")

        v = self.voice
        raw = getattr(self, "_meter_raw", 0)
        boost = getattr(self, "_meter_boost", 0)
        peak = getattr(self, "_meter_peak", 0)

        # compact live bars
        bar_x, bar_w, bar_h = 56, 560, 34
        full = 4000.0

        def bar(y, val, color, label):
            track = _pil_rounded_rect(bar_w, bar_h, 10, t["elevated_bg"])
            tk_tr = self._get_tk_image(f"meter_tr_{label}", track)
            c.create_image(bar_x, y, image=tk_tr, anchor="nw")
            fw = _qw(bar_w * max(0.0, min(val / full, 1.0)))
            if fw > 6:
                fill = _pil_rounded_rect(fw, bar_h, 10, color)
                tk_f = self._get_tk_image(f"meter_fill_{label}", fill)
                c.create_image(bar_x, y, image=tk_f, anchor="nw")
            c.create_text(bar_x + 10, y + bar_h // 2,
                          text="%s: %d" % (label, int(val)),
                          font=self.font_small_bold, fill=t["fg"],
                          anchor="w")

        raw_color = ("#2ECC71" if raw > 200 else
                     "#F1C40F" if raw > 40 else "#7F8C8D")
        bar(58, raw, raw_color, "MIC (raw)")
        bar(98, boost, DOSE_BLUE, "TO RECOGNIZER")
        verdict = ("WORKING — it hears you!" if peak > 120 else
                   "faint — speak louder" if peak > 30 else
                   "silent — try the next mic")
        c.create_text(56, 142,
                      text="Loudest so far: %d  (%s)" % (int(peak), verdict),
                      font=self.font_small_bold,
                      fill=(DOSE_BLUE_LT if peak > 30 else "#FF6B6B"),
                      anchor="nw")

        # ── EVERY capture device, tappable (no truncation) ──
        c.create_text(56, 166, text="Microphones — tap each until one moves:",
                      font=self.font_tiny, fill=t["muted"], anchor="nw")
        devs = []
        try:
            devs = v.list_capture_devices() if v else []
        except Exception:
            devs = []
        active = getattr(v, "mic_card", None) if v else None
        y = 184
        if not devs:
            c.create_text(64, y, text="(no capture devices found)",
                          font=self.font_tiny, fill="#FF6B6B", anchor="nw")
            y += 22
        for card, dev, short, is_mic in devs[:7]:
            sel = (active == card)
            row = _pil_rounded_rect(560, 26, 8,
                                    DOSE_BLUE if sel else t["elevated_bg"])
            tkr = self._get_tk_image(f"micpick_{card}_{dev}", row)
            c.create_image(56, y, image=tkr, anchor="nw")
            tag = " ●MIC" if is_mic else ""
            c.create_text(70, y + 13,
                          text=self._fit_text(
                              "card %d,%d  %s%s" % (card, dev, short, tag),
                              self.font_tiny, 530),
                          font=self.font_tiny,
                          fill="#06101E" if sel else t["fg"], anchor="w")
            self._click_zones.append(
                (56, y, 616, y + 26,
                 lambda cd=card, dv=dev: self._meter_pick(cd, dv)))
            y += 29
        ft = getattr(self, "_full_test_status", None)
        if ft:
            c.create_text(56, min(y + 2, 392),
                          text=self._fit_text(ft, self.font_tiny, 556),
                          font=self.font_tiny, fill=DOSE_BLUE_LT,
                          anchor="nw", width=556)

        # buttons: FULL TEST (records every device), RESET, DONE
        for label, cb, x0 in (("FULL TEST", self._full_mic_test, 56),
                              ("RESET", self._meter_reset, 300),
                              ("DONE", self._close_mic_meter, 500)):
            b_img = _pil_rounded_rect(120, 46, 14,
                                      DOSE_BLUE if label == "DONE"
                                      else t["elevated_bg"])
            tk_b = self._get_tk_image(f"meter_btn_{label}", b_img)
            c.create_image(x0, 398, image=tk_b, anchor="nw")
            c.create_text(x0 + 60, 421, text=label,
                          font=self.font_small_bold,
                          fill="#06101E" if label == "DONE" else t["fg"],
                          anchor="center")
            self._click_zones.append((x0, 398, x0 + 120, 444, cb))

    def _pick_speaker(self, sink):
        """User tapped a speaker — make it the output and immediately
        play a test so they can hear which one it is."""
        if self.voice:
            self.voice.force_sink(sink)
        self._speaker_test()

    def _meter_pick(self, card, dev):
        """User tapped a microphone in the list — use exactly it and
        watch the live meter to see if it works."""
        self._meter_peak = 0
        if self.voice is None:
            try:
                self._start_voice()
            except Exception:
                pass
        if self.voice:
            self.voice.force_card(card, dev)
            self._full_test_status = (
                "Switched to card %d,%d — speak and watch the bar."
                % (card, dev))
        else:
            self._full_test_status = (
                "Voice engine is still starting — try again in a moment.")
        self._draw_frame()

    def _meter_reset(self):
        self._meter_peak = 0
        self._draw_frame()

    def _meter_rescan(self):
        """Force the engine to re-locate and reopen the mic now, and
        clear the peak — so you can retry after re-seating the mic."""
        self._meter_peak = 0
        if self.voice:
            self.voice.request_reopen()
        self._draw_frame()

    def _full_mic_test(self):
        """Records directly from EVERY capture device, finds the one
        that actually hears, and locks onto it. Runs in a worker so the
        UI stays live; shows a countdown prompt to speak."""
        if not self.voice or getattr(self, "_full_test_busy", False):
            self._full_test_status = ("Voice engine not ready yet — "
                                      "wait for setup to finish.")
            self._draw_frame()
            return
        self._full_test_busy = True
        self._meter_peak = 0
        self._full_test_status = ("Testing every device — SPEAK NOW, "
                                  "keep talking for ~10 seconds…")
        self._draw_frame()

        def worker():
            summary = "test failed"
            report = []
            try:
                summary, report = self.voice.full_mic_test(2.5)
            except Exception as e:
                summary = "test error: %s" % e

            def done():
                self._full_test_busy = False
                self._full_test_status = summary
                # show the FULL report on the readable report screen
                self._mic_report_lines = [ln.rstrip()
                                          for ln in report][:40]
                self._prev_mode = "micmeter"
                self.mode = "micreport"
                self._draw_frame()
            try:
                self.root.after(0, done)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _open_mic_report(self):
        self._prev_mode = self.mode
        self.mode = "micreport"
        self._draw_frame()

    def _bt_close(self):
        self.mode = "settings"
        self._draw_frame()

    def _draw_mic_report(self, c):
        """The Pi's audio-system data, on screen: chosen backend,
        every input device, Bluetooth cards/sources, recorders."""
        t = self.theme
        card_img = _pil_rounded_rect(620, 440, 22, t["card_bg"])
        tk_card = self._get_tk_image("micrep_card", card_img)
        c.create_image(26, 20, image=tk_card, anchor="nw")
        c.create_text(56, 44, text="MICROPHONE REPORT",
                      font=self.font_label, fill=t["muted"],
                      anchor="nw")
        pkg = getattr(self, "_audio_pkg_status", "")
        if pkg:
            c.create_text(616, 44,
                          text=self._fit_text("tools: " + pkg,
                                              self.font_small, 300),
                          font=self.font_small, fill=DOSE_BLUE_LT,
                          anchor="ne")
        lines = getattr(self, "_mic_report_lines", None) or             ["No report yet — run a mic test first."]
        y = 66
        for ln in lines[:40]:
            if not ln.strip():
                y += 4
                continue
            low = ln.lower()
            color = ("#2ECC71" if "verdict" in low or "found the working"
                     in low else "#FF6B6B" if ("silent" in low
                     or "no audio" in low or "failed" in low
                     or "no capture" in low) else
                     DOSE_BLUE_LT if ln.startswith("$") else t["fg"])
            c.create_text(56, y,
                          text=self._fit_text(ln, self.font_tiny, 560),
                          font=self.font_tiny, fill=color, anchor="nw")
            y += 11
            if y > 384:
                break

        for label, x0, cb in (("RETEST", 56, self._mic_retest),
                              ("CLOSE", 466, self._mic_report_close)):
            btn_img = _pil_rounded_rect(150, 44, 14, DOSE_BLUE if
                                        label == "CLOSE" else
                                        t["elevated_bg"])
            tk_b = self._get_tk_image(f"micrep_{label}", btn_img)
            c.create_image(x0, 400, image=tk_b, anchor="nw")
            c.create_text(x0 + 75, 422, text=label,
                          font=self.font_btn,
                          fill="#06101E" if label == "CLOSE"
                          else t["fg"], anchor="center")
            self._click_zones.append((x0, 400, x0 + 150, 444, cb))

    def _mic_retest(self):
        self.mode = "settings"
        self._draw_frame()
        self._voice_mic_test()

    def _mic_report_close(self):
        self.mode = ("btaudio" if self._prev_mode == "btaudio"
                     else "settings")
        self._draw_frame()

    def _voice_row_tap(self):
        self._prev_mode = self.mode
        self.mode = "btaudio"
        self._ensure_audio_packages()
        self._draw_frame()

    def _audio_rescan(self):
        """Redo USB device selection right now — same thing the
        engine does automatically on hot-plug, on demand."""
        bt = self._bt_state()
        bt["status"] = "Re-scanning USB audio devices…"
        # a rescan supersedes any earlier mic-test verdict
        self._mic_test_result = None
        self._mic_diag = None
        self._draw_frame()
        if self.voice:
            self.voice.request_reopen()

        def clear():
            bt["status"] = ("Re-scan done — mic: %s" %
                            (getattr(self.voice, "mic_name", None)
                             or "none found")
                            if self.voice else "Voice engine not "
                            "running yet")
            if self.mode == "btaudio":
                self._draw_frame()
        try:
            self.root.after(6000, clear)
        except Exception:
            pass

    def _voice_mic_test(self):
        """Tap the Voice Assistant row: 2-second live mic check with
        a plain verdict — answers 'is the AirPods mic even working?'"""
        if (not self.voice or not self.voice.available
                or getattr(self, "_mic_testing", False)):
            return
        self._mic_testing = True
        self._mic_test_result = None
        self._draw_frame()

        def worker():
            level = self.voice.mic_level(2.0)
            self._mic_diag = None
            self._mic_report_lines = None
            if level <= 5:
                try:
                    self._mic_diag = self.voice.mic_report()
                    import dose_voice as _dv
                    rp = os.path.join(_dv.VOICE_DIR, "mic_report.txt")
                    with open(rp) as f:
                        self._mic_report_lines = [
                            ln.rstrip() for ln in f.readlines()][:40]
                except Exception:
                    pass

            def done():
                self._mic_testing = False
                self._mic_test_result = level
                self._draw_frame()
            try:
                self.root.after(0, done)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _med_info_for(self, name):
        """Bridge for the voice assistant: guidance lines for a med."""
        return MED_INFO.get((name or "").strip().lower(),
                            MED_INFO_DEFAULT)

    def _voice_overlay_update(self, state, user_text="", reply_text=""):
        """Called (on the Tk thread) by the voice engine."""
        self._voice_state = state
        if user_text:
            self._voice_user_text = user_text
        if reply_text:
            self._voice_reply = reply_text
        if state == "idle":
            # Don't yank it off the screen. Replies are fast enough now
            # that a short exchange was over before the panel had
            # finished sliding up — all you saw was a flash of the blue
            # wave. Hold it for a moment so it can actually be read,
            # then let the tick retire it.
            # Measured from when the CURRENT content appeared, not
            # from when the panel first opened. In a continuing
            # conversation the panel may have been up for a while, and
            # the last answer still deserves its moment on screen.
            self._voice_hide_at = max(
                time.time(),
                getattr(self, "_voice_last_change", 0)
                + VOICE_OVERLAY_MIN_S)
            if not self._voice_anim_running:
                self._voice_finish_overlay()
            return
        self._voice_hide_at = None
        self._voice_last_change = time.time()
        if not self._voice_anim_running:
            self._voice_anim_running = True
            self._voice_ov_t0 = time.time()
            self._voice_next_frame = self._voice_ov_t0
            self._voice_sig = None
            self._voice_anim_tick()

    def _voice_prerender_panel(self):
        """Render the overlay's panel once, up front. It costs ~10 ms
        here and nearer 80 ms on a Pi, and that was being paid the
        first time you spoke — which is exactly when it is most
        noticeable."""
        try:
            t = self.theme
            cache = getattr(self, "_voice_panel_cache", None)
            if cache is None:
                cache = self._voice_panel_cache = {}
            for bar_h in (124, 168):          # normal and in-conversation
                key = (620, bar_h, t["card_bg"])
                if key not in cache:
                    cache[key] = ImageTk.PhotoImage(
                        _pil_rounded_rect(620, bar_h, 20, t["card_bg"],
                                          outline=DOSE_BLUE, outline_w=2))
        except Exception:
            pass

    def _voice_finish_overlay(self):
        self._voice_anim_running = False
        self._voice_hide_at = None
        self._voice_user_text = ""
        self._voice_reply = ""
        try:
            self.canvas.delete("voice_ov")
        except Exception:
            pass
        self._voice_imgs.clear()
        self._voice_items = {}
        self._voice_sig = None
        self._voice_shown = {}

    # Wave geometry: three overlapping sine ribbons, drawn as NATIVE
    # canvas lines. The old overlay re-rendered two supersampled PIL
    # images (the panel and the wave) every single frame and threw the
    # whole overlay away in between — on a Pi that is ~100 ms of work
    # per frame, which is why it crawled. Now the panel image is built
    # once and reused, the ribbons are plain canvas polylines whose
    # coordinates are updated in place, and nothing is destroyed or
    # re-allocated between frames. That runs comfortably at 60 fps.
    WAVE_LAYERS = (
        # (colour, amplitude x, frequency x, phase offset, width)
        (DOSE_BLUE, 1.00, 1.0, 0.0, 3),
        (DOSE_BLUE_LT, 0.72, 1.6, 1.9, 2),
        ("#8fd0ff", 0.45, 2.3, 4.1, 2),
    )
    # 24 points per ribbon, drawn as a plain polyline. It was 44 with
    # smooth=True: Tk tessellates a spline through every point on EVERY
    # redraw, in software, and on a Pi that is most of the frame. At
    # this width 24 straight segments are indistinguishable from a
    # curve, and cost about a third as much.
    WAVE_POINTS = 24
    VOICE_FPS = 60            # ceiling; the real rate adapts (see below)
    VOICE_FPS_MIN = 30        # a steady 30 beats a stuttering 45

    def _voice_wave_coords(self, x, y, w, h, phase, amp,
                           a_mul, f_mul, p_off):
        mid = y + h / 2.0
        span = (h / 2.0) - 2
        step = w / float(self.WAVE_POINTS - 1)
        pts = []
        for i in range(self.WAVE_POINTS):
            u = i / float(self.WAVE_POINTS - 1)
            envelope = math.sin(math.pi * u) ** 1.5
            pts.append(x + i * step)
            pts.append(mid + span * amp * a_mul * envelope *
                       math.sin(2 * math.pi * (u * 2.1 * f_mul)
                                + phase + p_off))
        return pts

    def _voice_anim_tick(self):
        c = self.canvas
        t = self.theme
        now = time.time()

        if self._voice_state == "idle":
            hide_at = getattr(self, "_voice_hide_at", None)
            if hide_at is None or now >= hide_at:
                self._voice_finish_overlay()
                return
            # still inside the minimum visible window — keep drawing

        # A full redraw (_draw_frame) does canvas.delete("all"), which
        # takes the overlay's items with it. Tk then accepts coords()
        # on those dead ids SILENTLY, so the animation would carry on
        # at 60 fps moving things that no longer exist and the panel
        # would simply vanish. Anything that repaints the screen mid-
        # sentence used to do this — a QR presence change was the
        # common one. Notice it and rebuild.
        if not c.find_withtag("voice_ov"):
            self._voice_sig = None

        # During a conversation (add-med, corrections) the panel grows
        # and shows the dictation in big type so the user can verify
        # every word they said
        in_convo = bool(getattr(self.voice, "_flow", None))
        bar_w = 620
        # Taller than it was: the panel now always has room for BOTH
        # what you said and what she answered. It used to show only one
        # or the other, so the corrected transcript — the accurate one,
        # from the real recogniser rather than the live on-screen
        # listener — was never actually shown to you.
        bar_h = 168 if in_convo else 124

        # Rebuild the canvas items only when the LAYOUT changes, never
        # per frame.
        sig = (in_convo, bar_h, t["card_bg"])
        items = getattr(self, "_voice_items", None) or {}
        if getattr(self, "_voice_sig", None) != sig or not items:
            c.delete("voice_ov")
            self._voice_imgs.clear()
            items = {}
            # Rendering this rounded rectangle costs ~10 ms here and
            # closer to 80 ms on a Pi — which was the whole "it takes a
            # moment to pop up". It never changes, so it is built once
            # and kept for the life of the app.
            pkey = (bar_w, bar_h, t["card_bg"])
            panel = getattr(self, "_voice_panel_cache", None)
            if panel is None:
                panel = self._voice_panel_cache = {}
            if pkey not in panel:
                panel[pkey] = ImageTk.PhotoImage(
                    _pil_rounded_rect(bar_w, bar_h, 20, t["card_bg"],
                                      outline=DOSE_BLUE, outline_w=2))
            self._voice_imgs["bar"] = panel[pkey]
            items["bar"] = c.create_image(
                0, 0, image=self._voice_imgs["bar"], anchor="nw",
                tags="voice_ov")
            for i, (color, _a, _f, _p, lw) in enumerate(self.WAVE_LAYERS):
                items["w%d" % i] = c.create_line(
                    0, 0, 1, 1, fill=color, width=lw,
                    capstyle="round", tags="voice_ov")
            items["top"] = c.create_text(
                0, 0, text="", anchor="n", width=568,
                font=self.font_small, fill=t["muted"], tags="voice_ov")
            items["big"] = c.create_text(
                0, 0, text="", anchor="n", width=580,
                font=self.font_name, fill=DOSE_BLUE_LT, tags="voice_ov")
            self._voice_items = items
            self._voice_sig = sig
            self._voice_shown = {}

        # eased slide-up entrance (250 ms, cubic ease-out)
        p = min(1.0, (now - self._voice_ov_t0) / 0.25)
        ease = 1 - (1 - p) ** 3
        rise = int((1 - ease) * (bar_h + 14))
        bx, by = 26, SCREEN_H - bar_h - 14 + rise
        c.coords(items["bar"], bx, by)

        # Siri-style animated wave — coordinates only, no re-rendering
        phase = now * 5.0
        amp = {"listening": 1.0, "thinking": 0.3, "idle": 0.0}.get(
            self._voice_state, 0.45 + 0.45 * abs(math.sin(phase * 1.7)))
        if self._voice_state == "idle":
            # settling out during the hold, so it reads as finished
            # rather than frozen
            left = max(0.0, getattr(self, "_voice_hide_at", now) - now)
            amp = 0.25 * min(1.0, left / 0.8)
        wx, wy, ww, wh = bx + 24, by + bar_h - 38, bar_w - 48, 30
        for i, (_c, a_mul, f_mul, p_off, _lw) in enumerate(
                self.WAVE_LAYERS):
            c.coords(items["w%d" % i],
                     *self._voice_wave_coords(wx, wy, ww, wh, phase, amp,
                                              a_mul, f_mul, p_off))

        # Text: recomputed cheaply, but only PUSHED to the canvas when
        # it actually changes (itemconfigure forces a redraw).
        shown = self._voice_shown
        heard = self._voice_user_text

        def fit(txt, font, budget):
            """_fit_text measures glyphs through Tk, which is slow on a
            Pi. The strings only change when the conversation moves on,
            so the result is remembered."""
            k = (txt, id(font), budget)
            cache = getattr(self, "_voice_fit_cache", None)
            if cache is None:
                cache = self._voice_fit_cache = {}
            if k not in cache:
                if len(cache) > 64:
                    cache.clear()
                cache[k] = self._fit_text(txt, font, budget)
            return cache[k]
        if in_convo:
            # a question is being asked: her question small, your
            # answer large, so you can check every word of it
            top = (fit(self._voice_reply, self.font_small, 1120) if self._voice_reply else "")
            big = heard or ("Listening…"
                            if self._voice_state == "listening" else "…")
            big = fit(big, self.font_name, 1100)
            top_style = (self.font_small, t["muted"])
            big_style = (self.font_name, DOSE_BLUE_LT)
            top_y, big_y = by + 14, by + 62
        else:
            # ALWAYS both lines: what was heard on top, the answer
            # below. While listening the top line is the live
            # transcript; once the turn closes it is replaced by the
            # accurate one from the real recogniser, so you can see
            # exactly what it understood you to say.
            if heard:
                top = '"%s"' % fit(heard, self.font_small, 1080)
                top_style = (self.font_small, DOSE_BLUE_LT)
            elif self._voice_state == "listening":
                top = "Listening…"
                top_style = (self.font_small, t["muted"])
            else:
                top = "…"
                top_style = (self.font_small, t["muted"])
            if self._voice_state == "speaking" and self._voice_reply:
                big = fit(self._voice_reply, self.font_small, 1180)
                big_style = (self.font_small, t["fg"])
            elif self._voice_state == "thinking":
                big = "…"
                big_style = (self.font_small, t["muted"])
            else:
                big = ""
                big_style = (self.font_small, t["fg"])
            top_y, big_y = by + 12, by + 40

        for key, text, style, ty in (("top", top, top_style, top_y),
                                     ("big", big, big_style, big_y)):
            c.coords(items[key], bx + bar_w // 2, ty)
            if shown.get(key) != (text, style):
                c.itemconfigure(items[key], text=text, font=style[0],
                                fill=style[1])
                shown[key] = (text, style)

        # Hold a steady frame rate: schedule by the time the NEXT frame
        # is due, so a slow frame doesn't push every later one back and
        # the animation never queues up behind itself. The clock is
        # clamped on BOTH sides — if we fall behind we resync to now
        # rather than trying to catch up in a burst, and if the clock
        # ever gets ahead of real time we pull it back, so a frame can
        # never be scheduled further out than one interval.
        # Adaptive pacing. Asking a Pi for 60 fps of software-rendered
        # canvas while it is also recognising speech and decoding QR
        # codes does not produce 60 fps — it produces a backlog and a
        # stutter. Measure what a frame actually costs and pick a rate
        # the machine can hold: a steady 30 looks better than a ragged
        # 45, and leaves the CPU for the things that matter.
        cost = time.time() - now
        avg = getattr(self, "_voice_frame_cost", cost) * 0.9 + cost * 0.1
        self._voice_frame_cost = avg
        fps = self.VOICE_FPS
        if avg > 0.7 / self.VOICE_FPS:        # can't hold the ceiling
            fps = self.VOICE_FPS_MIN
        self._voice_fps_now = fps
        interval = 1.0 / fps
        done = time.time()
        nxt = getattr(self, "_voice_next_frame", 0.0) + interval
        if not (done <= nxt <= done + interval):
            nxt = done + interval
        self._voice_next_frame = nxt
        self.root.after(max(1, int(round((nxt - done) * 1000))),
                        self._voice_anim_tick)

    # ── Quit ───────────────────────────────────────────────────────────────
    def _quit(self):
        try:
            if self.voice:
                self.voice.stop()
        except Exception:
            pass
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
