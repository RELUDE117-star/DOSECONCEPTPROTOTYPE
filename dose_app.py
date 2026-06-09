#!/usr/bin/env python3
"""
DOSE Home Station - Medication Dispenser Kiosk Application
Raspberry Pi 4B + Elecrow 5" 800x480 Touchscreen
"""

import tkinter as tk
import tkinter.font as tkfont
import json
import os
import subprocess
import time
import threading
import math
import base64
import io
from datetime import datetime

# ---------------------------------------------------------------------------
# Graceful optional imports
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Embedded dose+ logo (48x48 PNG, base64-encoded)
# ---------------------------------------------------------------------------
DOSE_LOGO_B64 = (
    "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"
    "IVyT4AAAAASUVORK5CYII="
)

# ---------------------------------------------------------------------------
# Default medication data
# ---------------------------------------------------------------------------
DEFAULT_MEDS = {
    "blue": {
        "name": "Blue", "accent": "#5B9BFF", "pills_left": 26,
        "schedule_time": "11:00 AM",
        "schedule_days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "take_with": "A full glass of water. With or without food.",
        "safety": (
            "Do not exceed prescribed dose. Store at room temperature away "
            "from moisture and heat. May cause dizziness — avoid driving "
            "until you know how it affects you."
        ),
    },
    "red": {
        "name": "Red", "accent": "#FF6B6B", "pills_left": 14,
        "schedule_time": "8:00 AM",
        "schedule_days": ["Mon", "Wed", "Fri"],
        "take_with": "Food, to avoid stomach upset. Avoid alcohol.",
        "safety": (
            "Take with food to reduce nausea. Do not crush or chew. Avoid "
            "grapefruit juice. Report unusual bleeding or bruising to your doctor."
        ),
    },
    "green": {
        "name": "Green", "accent": "#5BD08A", "pills_left": 30,
        "schedule_time": "9:00 AM",
        "schedule_days": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "take_with": "An empty stomach, ~1 hour before eating.",
        "safety": (
            "Take on empty stomach for best absorption. Do not take with "
            "dairy products. May increase sun sensitivity — use sunscreen."
        ),
    },
    "yellow": {
        "name": "Yellow", "accent": "#E6C34A", "pills_left": 8,
        "schedule_time": "7:00 AM",
        "schedule_days": ["Mon", "Tue", "Wed", "Thu", "Fri"],
        "take_with": "Morning, with water. Do not crush or chew.",
        "safety": (
            "Swallow whole — do not split, crush, or chew. Take at the "
            "same time each day. Store away from direct sunlight."
        ),
    },
}

ALL_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

CONFIG_PATH = os.path.expanduser("~/.dose_config.json")

# ---------------------------------------------------------------------------
# Theme palettes
# ---------------------------------------------------------------------------
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

# Grid constants
MARGIN_LEFT = 52
MARGIN_RIGHT = 52
RIGHT_EDGE = 800 - MARGIN_RIGHT  # 748


# ===================================================================
# Main Application
# ===================================================================
class DoseApp:
    """Top-level controller for the DOSE Home Station kiosk."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("DOSE Home Station")
        self.root.geometry("800x480")
        self.root.resizable(False, False)
        self.root.configure(bg="#070708")

        # Fullscreen & cursor
        try:
            self.root.attributes("-fullscreen", True)
        except Exception:
            pass
        self.root.config(cursor="none")

        # ---- Fonts -------------------------------------------------
        available = tkfont.families()
        family = "DejaVu Sans"
        for candidate in ("Nunito", "Nunito Sans"):
            if candidate in available:
                family = candidate
                break
        self.font_family = family

        self.font_clock = tkfont.Font(family=family, size=32, weight="normal")
        self.font_xl = tkfont.Font(family=family, size=44, weight="normal")
        self.font_name = tkfont.Font(family=family, size=36, weight="bold")
        self.font_lg = tkfont.Font(family=family, size=20, weight="normal")
        self.font_md = tkfont.Font(family=family, size=16, weight="normal")
        self.font_sm = tkfont.Font(family=family, size=13, weight="normal")
        self.font_label = tkfont.Font(family=family, size=11, weight="bold")
        self.font_xs = tkfont.Font(family=family, size=10, weight="normal")
        self.font_bold_sm = tkfont.Font(family=family, size=13, weight="bold")
        self.font_bold_md = tkfont.Font(family=family, size=16, weight="bold")
        self.font_bold_lg = tkfont.Font(family=family, size=20, weight="bold")

        # ---- State -------------------------------------------------
        self.meds = {}
        self.settings = {"night_mode": True, "alarm_sound": True}
        self.theme = dict(DARK_THEME)
        self.mode = "standby"  # standby | storage | settings
        self.selected_pill = "blue"

        # Dispensing state
        self.dispensing_active = False
        self.dispense_state = None  # READ | HOLD | CONFIRM | DISPENSED
        self.dispense_pill_key = None
        self.hold_start = None
        self.hold_timer_id = None
        self.dispense_return_id = None

        # D-menu
        self.menu_visible = False
        self.menu_timeout_id = None

        # Camera
        self.camera = None
        self.camera_thread = None
        self.camera_running = False

        # Load saved config
        self._load_config()

        # Apply theme from settings
        if self.settings.get("night_mode", True):
            self.theme = dict(DARK_THEME)
        else:
            self.theme = dict(LIGHT_THEME)

        # ---- Build UI layers ----------------------------------------
        self.root.configure(bg=self.theme["bg"])

        # Main container
        self.main_frame = tk.Frame(self.root, bg=self.theme["bg"])
        self.main_frame.place(x=0, y=0, width=800, height=480)

        # Overlay for dispensing flow (on top of everything)
        self.overlay_frame = tk.Frame(self.root, bg=self.theme["bg"])

        # D button (always visible)
        self.d_btn_canvas = tk.Canvas(
            self.root, width=48, height=48,
            bg=self.theme["bg"], highlightthickness=0, bd=0
        )
        self.d_btn_canvas.place(x=740, y=424)
        self._draw_d_button()
        self.d_btn_canvas.bind("<Button-1>", self._on_d_pressed)

        # Popup menu frame
        self.popup_frame = tk.Frame(
            self.root, bg=self.theme["popup_bg"],
            highlightbackground=self.theme["muted"], highlightthickness=1
        )

        # ---- Key bindings -------------------------------------------
        self.root.bind("<Escape>", lambda e: self.root.destroy())

        # ---- Build modes --------------------------------------------
        self._build_standby()
        self._build_storage()
        self._build_settings()
        self._build_overlay()

        # ---- Show standby -------------------------------------------
        self._show_mode("standby")

        # ---- Start clock --------------------------------------------
        self._tick_clock()

        # ---- Camera -------------------------------------------------
        if CAMERA_AVAILABLE:
            self._start_camera()

        # Focus so Esc works
        self.root.focus_force()

    @staticmethod
    def _raise_widget(widget):
        """Raise a widget in the stacking order. Works for Canvas too."""
        widget.tk.call('raise', widget._w)

    # ---------------------------------------------------------------
    # Config persistence
    # ---------------------------------------------------------------
    def _load_config(self):
        """Load medication data and settings from JSON, or use defaults."""
        try:
            with open(CONFIG_PATH, "r") as f:
                data = json.load(f)
            self.meds = data.get("meds", {})
            self.settings = data.get("settings", {"night_mode": True, "alarm_sound": True})
            # Ensure all default keys exist
            for key, default in DEFAULT_MEDS.items():
                if key not in self.meds:
                    self.meds[key] = dict(default)
                else:
                    for dk, dv in default.items():
                        if dk not in self.meds[key]:
                            self.meds[key][dk] = dv
        except Exception:
            self.meds = {k: dict(v) for k, v in DEFAULT_MEDS.items()}
            self.settings = {"night_mode": True, "alarm_sound": True}

    def _save_config(self):
        """Persist current state to JSON."""
        try:
            data = {"meds": self.meds, "settings": self.settings}
            with open(CONFIG_PATH, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    # ---------------------------------------------------------------
    # Theme helpers
    # ---------------------------------------------------------------
    def _apply_theme(self):
        """Recolour every widget after a theme change."""
        t = self.theme
        self.root.configure(bg=t["bg"])
        self.main_frame.configure(bg=t["bg"])
        self.overlay_frame.configure(bg=t["bg"])
        self.d_btn_canvas.configure(bg=t["bg"])
        self._draw_d_button()
        self.popup_frame.configure(bg=t["popup_bg"], highlightbackground=t["muted"])

        # Rebuild everything (simplest way to recolour deeply)
        self._build_standby()
        self._build_storage()
        self._build_settings()
        self._build_overlay()
        self._show_mode(self.mode)

    # ---------------------------------------------------------------
    # D button and popup menu
    # ---------------------------------------------------------------
    def _draw_d_button(self):
        c = self.d_btn_canvas
        c.delete("all")
        c.configure(bg=self.theme["bg"])
        if PIL_AVAILABLE and not hasattr(self, '_d_logo_img'):
            raw = base64.b64decode(DOSE_LOGO_B64)
            pil_img = Image.open(io.BytesIO(raw)).resize((48, 48), Image.LANCZOS)
            self._d_logo_img = ImageTk.PhotoImage(pil_img)
        if hasattr(self, '_d_logo_img'):
            c.create_image(24, 24, image=self._d_logo_img)
        else:
            self._canvas_rounded_rect(c, 2, 2, 46, 46, 12, fill="#5B9BFF", outline="")
            c.create_text(25, 26, text="D", fill="#FFFFFF",
                          font=tkfont.Font(family=self.font_family, size=22, weight="bold"))

    def _on_d_pressed(self, event=None):
        if self.dispensing_active:
            return
        if self.menu_visible:
            self._hide_menu()
        else:
            self._show_menu()

    def _show_menu(self):
        if self.menu_visible:
            return
        self.menu_visible = True
        t = self.theme

        # Clear old children
        for w in self.popup_frame.winfo_children():
            w.destroy()

        items = [("Standby", "standby"), ("Storage", "storage"), ("Settings", "settings")]
        for label, mode in items:
            btn = tk.Label(
                self.popup_frame, text=label, font=self.font_md,
                bg=t["popup_bg"], fg=t["fg"],
                padx=24, pady=14, anchor="w"
            )
            btn.pack(fill="x")
            btn.bind("<Button-1>", lambda e, m=mode: self._menu_pick(m))
            btn.bind("<Enter>", lambda e, b=btn: b.configure(bg=t["btn_active"]))
            btn.bind("<Leave>", lambda e, b=btn: b.configure(bg=t["popup_bg"]))

        # Place wider menu, right-aligned with D button, above it
        self.popup_frame.place(x=608, y=280, width=180)
        self._raise_widget(self.popup_frame)

        # Auto-dismiss after 45 s
        if self.menu_timeout_id:
            self.root.after_cancel(self.menu_timeout_id)
        self.menu_timeout_id = self.root.after(45000, self._hide_menu)

    def _hide_menu(self):
        self.menu_visible = False
        self.popup_frame.place_forget()
        if self.menu_timeout_id:
            self.root.after_cancel(self.menu_timeout_id)
            self.menu_timeout_id = None

    def _menu_pick(self, mode):
        self._hide_menu()
        self._show_mode(mode)

    # ---------------------------------------------------------------
    # Mode switching
    # ---------------------------------------------------------------
    def _show_mode(self, mode):
        self.mode = mode
        # Hide all mode frames
        self.standby_frame.place_forget()
        self.storage_frame.place_forget()
        self.settings_frame.place_forget()
        self.overlay_frame.place_forget()

        if mode == "standby":
            self.standby_frame.place(x=0, y=0, width=800, height=480)
            self._update_standby()
        elif mode == "storage":
            self.storage_frame.place(x=0, y=0, width=800, height=480)
            self._update_storage()
        elif mode == "settings":
            self.settings_frame.place(x=0, y=0, width=800, height=480)
            self._update_settings()

        # Keep D button on top
        self._raise_widget(self.d_btn_canvas)

    # ---------------------------------------------------------------
    # Clock
    # ---------------------------------------------------------------
    def _tick_clock(self):
        try:
            now = datetime.now().strftime("%-I:%M %p")
        except ValueError:
            now = datetime.now().strftime("%I:%M %p").lstrip("0")
        self._current_time_str = now

        # Update standby clock
        if hasattr(self, "_standby_clock_label"):
            self._standby_clock_label.configure(text=now)

        self.root.after(1000, self._tick_clock)

    # ---------------------------------------------------------------
    # Canvas helper: rounded rectangle
    # ---------------------------------------------------------------
    @staticmethod
    def _canvas_rounded_rect(canvas, x1, y1, x2, y2, r, **kw):
        points = [
            x1 + r, y1,
            x2 - r, y1,
            x2, y1,
            x2, y1 + r,
            x2, y2 - r,
            x2, y2,
            x2 - r, y2,
            x1 + r, y2,
            x1, y2,
            x1, y2 - r,
            x1, y1 + r,
            x1, y1,
            x1 + r, y1,
        ]
        return canvas.create_polygon(points, smooth=True, **kw)

    # ===================================================================
    # STANDBY MODE
    # ===================================================================
    def _build_standby(self):
        if hasattr(self, "standby_frame"):
            self.standby_frame.destroy()
        t = self.theme
        self.standby_frame = tk.Frame(self.main_frame, bg=t["bg"])

        # Clock
        self._standby_clock_label = tk.Label(
            self.standby_frame, text="", font=self.font_clock,
            bg=t["bg"], fg=t["fg"], anchor="w"
        )
        self._standby_clock_label.place(x=MARGIN_LEFT, y=40)

        # WiFi icon (canvas with arcs)
        wifi_c = tk.Canvas(self.standby_frame, width=36, height=30,
                           bg=t["bg"], highlightthickness=0)
        wifi_c.place(x=720, y=42)
        self._draw_wifi(wifi_c, t["fg"])

        # "NEXT DOSE" label — small caps tracking style
        tk.Label(
            self.standby_frame, text="NEXT DOSE", font=self.font_label,
            bg=t["bg"], fg=t["muted"]
        ).place(x=MARGIN_LEFT, y=180)

        # Find next dose
        next_time, next_pills = self._compute_next_dose()

        # Next dose time — hero element, big
        self._standby_next_time = tk.Label(
            self.standby_frame, text=next_time, font=self.font_xl,
            bg=t["bg"], fg=t["fg"]
        )
        self._standby_next_time.place(x=MARGIN_LEFT, y=205)

        # Pills count
        self._standby_next_pills = tk.Label(
            self.standby_frame, text=next_pills, font=self.font_lg,
            bg=t["bg"], fg=t["muted"]
        )
        self._standby_next_pills.place(x=MARGIN_LEFT, y=270)

        # Camera status
        if not CAMERA_AVAILABLE:
            tk.Label(
                self.standby_frame, text="Camera not connected", font=self.font_xs,
                bg=t["bg"], fg=t["muted"]
            ).place(x=MARGIN_LEFT, y=440)

        # Bind tap on entire standby frame to trigger demo scan
        self.standby_frame.bind("<Button-1>", lambda e: self._start_dispense("blue"))

    def _update_standby(self):
        try:
            now = datetime.now().strftime("%-I:%M %p")
        except ValueError:
            now = datetime.now().strftime("%I:%M %p").lstrip("0")
        self._standby_clock_label.configure(text=now)
        next_time, next_pills = self._compute_next_dose()
        self._standby_next_time.configure(text=next_time)
        self._standby_next_pills.configure(text=next_pills)

    def _compute_next_dose(self):
        """Find the soonest upcoming dose across all meds."""
        now = datetime.now()
        today_name = now.strftime("%a")  # Mon, Tue, ...
        best_time = None
        count = 0
        for key, med in self.meds.items():
            if today_name in med.get("schedule_days", []):
                try:
                    t = datetime.strptime(med["schedule_time"], "%I:%M %p")
                    dose_dt = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
                    if dose_dt > now:
                        if best_time is None or dose_dt < best_time:
                            best_time = dose_dt
                            count = 1
                        elif dose_dt == best_time:
                            count += 1
                except Exception:
                    pass
        if best_time:
            try:
                tstr = best_time.strftime("%-I:%M %p")
            except ValueError:
                tstr = best_time.strftime("%I:%M %p").lstrip("0")
            return tstr, f"{count} Pill{'s' if count != 1 else ''}"
        return "No doses today", ""

    @staticmethod
    def _draw_wifi(canvas, color):
        cx, cy = 18, 26
        for i, r in enumerate([22, 16, 10]):
            canvas.create_arc(
                cx - r, cy - r, cx + r, cy + r,
                start=45, extent=90, style="arc",
                outline=color, width=2
            )
        canvas.create_oval(cx - 2, cy - 2, cx + 2, cy + 2, fill=color, outline=color)

    # ===================================================================
    # STORAGE MODE
    # ===================================================================
    def _build_storage(self):
        if hasattr(self, "storage_frame"):
            self.storage_frame.destroy()
        t = self.theme
        self.storage_frame = tk.Frame(self.main_frame, bg=t["bg"])

        # Left panel - pill list (wider for breathing room)
        left = tk.Frame(self.storage_frame, bg=t["bg"], width=240)
        left.place(x=0, y=0, width=240, height=480)
        left.pack_propagate(False)

        tk.Label(left, text="MEDICATIONS", font=self.font_label,
                 bg=t["bg"], fg=t["muted"]).pack(anchor="w", padx=24, pady=(20, 12))

        self._pill_rows = {}
        self._pill_row_canvases = {}
        for key in ["blue", "red", "green", "yellow"]:
            med = self.meds[key]

            # Use a canvas-based row for the left accent bar
            row_frame = tk.Frame(left, bg=t["bg"], cursor="hand2")
            row_frame.pack(fill="x", padx=12, pady=2)

            # Accent dot (slightly larger)
            dot_c = tk.Canvas(row_frame, width=16, height=16, bg=t["bg"], highlightthickness=0)
            dot_c.pack(side="left", padx=(10, 8), pady=14)
            dot_c.create_oval(2, 2, 14, 14, fill=med["accent"], outline="")

            lbl = tk.Label(
                row_frame, text=f"Pill {med['name']}", font=self.font_md,
                bg=t["bg"], fg=t["fg"], anchor="w"
            )
            lbl.pack(side="left", fill="x", expand=True, pady=14)

            self._pill_rows[key] = (row_frame, lbl, dot_c)
            for widget in (row_frame, lbl, dot_c):
                widget.bind("<Button-1>", lambda e, k=key: self._select_pill(k))

        # Right panel
        self._storage_right = tk.Frame(self.storage_frame, bg=t["bg"])
        self._storage_right.place(x=240, y=0, width=560, height=480)

        self._build_storage_detail()

    def _select_pill(self, key):
        self.selected_pill = key
        self._update_storage()

    def _build_storage_detail(self):
        """Build the right-side detail view for the selected pill."""
        for w in self._storage_right.winfo_children():
            w.destroy()

        t = self.theme
        med = self.meds[self.selected_pill]
        parent = self._storage_right

        # Internal padding from left edge of right panel: 40px
        px = 40

        # Pill visual indicator (larger colored rounded rect)
        pill_c = tk.Canvas(parent, width=140, height=70, bg=t["bg"], highlightthickness=0)
        pill_c.place(x=px, y=20)
        self._canvas_rounded_rect(pill_c, 4, 4, 136, 66, 24,
                                  fill=med["accent"], outline="")
        pill_c.create_text(70, 35, text=med["name"], fill="#FFFFFF",
                           font=self.font_bold_md)

        # Pills left — vertically centered with pill visual
        self._storage_pills_left_label = tk.Label(
            parent, text=f"{med['pills_left']} Pills Left",
            font=self.font_bold_lg, bg=t["bg"], fg=t["fg"]
        )
        self._storage_pills_left_label.place(x=px + 160, y=38)

        # ---- Schedule time ----
        tk.Label(parent, text="SCHEDULE", font=self.font_label,
                 bg=t["bg"], fg=t["muted"]).place(x=px, y=120)

        time_frame = tk.Frame(parent, bg=t["bg"])
        time_frame.place(x=px, y=148)

        # Parse current schedule time
        try:
            st = datetime.strptime(med["schedule_time"], "%I:%M %p")
            self._sch_hour = st.hour % 12 or 12
            self._sch_minute = st.minute
            self._sch_ampm = "AM" if st.hour < 12 else "PM"
        except Exception:
            self._sch_hour = 8
            self._sch_minute = 0
            self._sch_ampm = "AM"

        # Hour control
        h_frame = tk.Frame(time_frame, bg=t["bg"])
        h_frame.pack(side="left", padx=(0, 4))

        h_up = tk.Label(h_frame, text="▲", font=self.font_sm,
                        bg=t["btn_bg"], fg=t["fg"], width=4, pady=6)
        h_up.pack()
        h_up.bind("<Button-1>", lambda e: self._adjust_schedule("hour", 1))

        self._sch_hour_label = tk.Label(
            h_frame, text=f"{self._sch_hour:d}", font=self.font_bold_lg,
            bg=t["bg"], fg=t["fg"], width=4
        )
        self._sch_hour_label.pack()

        h_down = tk.Label(h_frame, text="▼", font=self.font_sm,
                          bg=t["btn_bg"], fg=t["fg"], width=4, pady=6)
        h_down.pack()
        h_down.bind("<Button-1>", lambda e: self._adjust_schedule("hour", -1))

        tk.Label(time_frame, text=":", font=self.font_bold_lg,
                 bg=t["bg"], fg=t["fg"]).pack(side="left")

        # Minute control
        m_frame = tk.Frame(time_frame, bg=t["bg"])
        m_frame.pack(side="left", padx=(4, 8))

        m_up = tk.Label(m_frame, text="▲", font=self.font_sm,
                        bg=t["btn_bg"], fg=t["fg"], width=4, pady=6)
        m_up.pack()
        m_up.bind("<Button-1>", lambda e: self._adjust_schedule("minute", 1))

        self._sch_min_label = tk.Label(
            m_frame, text=f"{self._sch_minute:02d}", font=self.font_bold_lg,
            bg=t["bg"], fg=t["fg"], width=4
        )
        self._sch_min_label.pack()

        m_down = tk.Label(m_frame, text="▼", font=self.font_sm,
                          bg=t["btn_bg"], fg=t["fg"], width=4, pady=6)
        m_down.pack()
        m_down.bind("<Button-1>", lambda e: self._adjust_schedule("minute", -1))

        # AM/PM toggle
        self._sch_ampm_label = tk.Label(
            time_frame, text=self._sch_ampm, font=self.font_bold_md,
            bg=t["btn_bg"], fg=t["fg"], padx=12, pady=8
        )
        self._sch_ampm_label.pack(side="left", padx=(8, 0))
        self._sch_ampm_label.bind("<Button-1>", lambda e: self._adjust_schedule("ampm", 0))

        # ---- Day of week selector ----
        tk.Label(parent, text="DAYS", font=self.font_label,
                 bg=t["bg"], fg=t["muted"]).place(x=px, y=268)

        days_frame = tk.Frame(parent, bg=t["bg"])
        days_frame.place(x=px, y=294)

        self._day_buttons = {}
        for day in ALL_DAYS:
            active = day in med.get("schedule_days", [])
            bg_col = med["accent"] if active else t["btn_bg"]
            fg_col = "#FFFFFF" if active else t["muted"]
            btn = tk.Label(
                days_frame, text=day[:2], font=self.font_bold_sm,
                bg=bg_col, fg=fg_col, width=4, pady=8
            )
            btn.pack(side="left", padx=2)
            btn.bind("<Button-1>", lambda e, d=day: self._toggle_day(d))
            self._day_buttons[day] = btn

        # ---- Instructions ----
        tk.Label(parent, text="INSTRUCTIONS", font=self.font_label,
                 bg=t["bg"], fg=t["muted"]).place(x=px, y=360)

        info_text = f"Take with: {med['take_with']}"
        tk.Label(
            parent, text=info_text, font=self.font_sm,
            bg=t["bg"], fg=t["fg"], wraplength=480, justify="left", anchor="nw"
        ).place(x=px, y=386)

        safety_label = tk.Label(
            parent, text=med["safety"], font=self.font_xs,
            bg=t["bg"], fg=t["muted"], wraplength=480, justify="left", anchor="nw"
        )
        safety_label.place(x=px, y=420)

    def _adjust_schedule(self, field, delta):
        if field == "hour":
            self._sch_hour = ((self._sch_hour - 1 + delta) % 12) + 1
            self._sch_hour_label.configure(text=f"{self._sch_hour:d}")
        elif field == "minute":
            self._sch_minute = (self._sch_minute + delta * 5) % 60
            self._sch_min_label.configure(text=f"{self._sch_minute:02d}")
        elif field == "ampm":
            self._sch_ampm = "PM" if self._sch_ampm == "AM" else "AM"
            self._sch_ampm_label.configure(text=self._sch_ampm)

        # Save
        time_str = f"{self._sch_hour}:{self._sch_minute:02d} {self._sch_ampm}"
        self.meds[self.selected_pill]["schedule_time"] = time_str
        self._save_config()

    def _toggle_day(self, day):
        med = self.meds[self.selected_pill]
        days = med.get("schedule_days", [])
        if day in days:
            days.remove(day)
        else:
            days.append(day)
        med["schedule_days"] = days
        self._save_config()

        # Update button appearance
        t = self.theme
        active = day in days
        bg_col = med["accent"] if active else t["btn_bg"]
        fg_col = "#FFFFFF" if active else t["muted"]
        self._day_buttons[day].configure(bg=bg_col, fg=fg_col)

    def _update_storage(self):
        t = self.theme
        med = self.meds[self.selected_pill]
        # Highlight selected row with card_bg and a left accent bar
        for key, (row, lbl, dot_c) in self._pill_rows.items():
            if key == self.selected_pill:
                row.configure(bg=t["card_bg"])
                lbl.configure(bg=t["card_bg"])
                dot_c.configure(bg=t["card_bg"])
            else:
                row.configure(bg=t["bg"])
                lbl.configure(bg=t["bg"])
                dot_c.configure(bg=t["bg"])

        self._build_storage_detail()

    # ===================================================================
    # SETTINGS MODE
    # ===================================================================
    def _build_settings(self):
        if hasattr(self, "settings_frame"):
            self.settings_frame.destroy()
        t = self.theme
        self.settings_frame = tk.Frame(self.main_frame, bg=t["bg"])

        # Big bold page title
        tk.Label(
            self.settings_frame, text="Settings", font=self.font_name,
            bg=t["bg"], fg=t["fg"]
        ).place(x=MARGIN_LEFT, y=40)

        # Day/Night toggle row
        row1 = tk.Frame(self.settings_frame, bg=t["bg"])
        row1.place(x=MARGIN_LEFT, y=140, width=696, height=60)

        tk.Label(row1, text="Day / Night Mode", font=self.font_lg,
                 bg=t["bg"], fg=t["fg"]).place(x=0, rely=0.5, anchor="w")

        self._night_toggle_canvas = tk.Canvas(
            row1, width=60, height=32, bg=t["bg"], highlightthickness=0
        )
        self._night_toggle_canvas.place(x=640, rely=0.5, anchor="w")
        self._draw_toggle(self._night_toggle_canvas, self.settings["night_mode"])
        self._night_toggle_canvas.bind("<Button-1>", self._toggle_night)

        # Divider
        tk.Frame(self.settings_frame, bg=t["muted"], height=1).place(
            x=MARGIN_LEFT, y=208, width=696)

        # Alarm toggle row
        row2 = tk.Frame(self.settings_frame, bg=t["bg"])
        row2.place(x=MARGIN_LEFT, y=220, width=696, height=60)

        tk.Label(row2, text="Alarm Sound", font=self.font_lg,
                 bg=t["bg"], fg=t["fg"]).place(x=0, rely=0.5, anchor="w")

        self._alarm_toggle_canvas = tk.Canvas(
            row2, width=60, height=32, bg=t["bg"], highlightthickness=0
        )
        self._alarm_toggle_canvas.place(x=640, rely=0.5, anchor="w")
        self._draw_toggle(self._alarm_toggle_canvas, self.settings["alarm_sound"])
        self._alarm_toggle_canvas.bind("<Button-1>", self._toggle_alarm)

    def _draw_toggle(self, canvas, on):
        canvas.delete("all")
        t = self.theme
        canvas.configure(bg=t["bg"])
        if on:
            # Track
            self._canvas_rounded_rect(canvas, 0, 2, 58, 30, 14, fill="#3478F6", outline="")
            # Knob
            canvas.create_oval(30, 4, 56, 28, fill="#FFFFFF", outline="")
        else:
            self._canvas_rounded_rect(canvas, 0, 2, 58, 30, 14, fill=t["muted"], outline="")
            canvas.create_oval(2, 4, 28, 28, fill="#FFFFFF", outline="")

    def _toggle_night(self, event=None):
        self.settings["night_mode"] = not self.settings["night_mode"]
        if self.settings["night_mode"]:
            self.theme = dict(DARK_THEME)
        else:
            self.theme = dict(LIGHT_THEME)
        self._save_config()
        self._apply_theme()

    def _toggle_alarm(self, event=None):
        self.settings["alarm_sound"] = not self.settings["alarm_sound"]
        self._draw_toggle(self._alarm_toggle_canvas, self.settings["alarm_sound"])
        self._save_config()

    def _update_settings(self):
        pass  # Already built fresh via _build_settings

    # ===================================================================
    # DISPENSING FLOW
    # ===================================================================
    def _build_overlay(self):
        """Pre-build the overlay frame structure (redrawn per state)."""
        for w in self.overlay_frame.winfo_children():
            w.destroy()

    def _start_dispense(self, pill_key):
        if self.dispensing_active:
            return
        self.dispensing_active = True
        self.dispense_pill_key = pill_key
        self._show_dispense_read()

    def _end_dispense(self):
        self.dispensing_active = False
        self.dispense_state = None
        self.dispense_pill_key = None
        if self.dispense_return_id:
            self.root.after_cancel(self.dispense_return_id)
            self.dispense_return_id = None
        self.overlay_frame.place_forget()
        self._show_mode(self.mode)

    def _show_dispense_read(self):
        """State 1: READ - show pill info with Yes/No."""
        self.dispense_state = "READ"
        t = self.theme
        med = self.meds[self.dispense_pill_key]

        for w in self.overlay_frame.winfo_children():
            w.destroy()
        self.overlay_frame.configure(bg=t["bg"])
        self.overlay_frame.place(x=0, y=0, width=800, height=480)
        self._raise_widget(self.overlay_frame)
        self._raise_widget(self.d_btn_canvas)

        # Pill name — centered, accent color
        tk.Label(
            self.overlay_frame, text=f"Pill {med['name']}", font=self.font_name,
            bg=t["bg"], fg=med["accent"]
        ).place(relx=0.5, y=60, anchor="n")

        # Take with label
        tk.Label(
            self.overlay_frame, text="TAKE WITH", font=self.font_label,
            bg=t["bg"], fg=t["muted"]
        ).place(relx=0.5, y=120, anchor="n")

        # Take with instructions
        tk.Label(
            self.overlay_frame, text=med["take_with"], font=self.font_md,
            bg=t["bg"], fg=t["fg"], wraplength=600, justify="center"
        ).place(relx=0.5, y=148, anchor="n")

        # Question
        tk.Label(
            self.overlay_frame, text="Would you like to take this medication?",
            font=self.font_bold_md, bg=t["bg"], fg=t["fg"]
        ).place(relx=0.5, y=240, anchor="n")

        # Yes button — wider (240px)
        yes_c = tk.Canvas(self.overlay_frame, width=240, height=56,
                          bg=t["bg"], highlightthickness=0)
        yes_c.place(relx=0.5, y=300, anchor="n", x=-130)
        self._canvas_rounded_rect(yes_c, 0, 0, 240, 56, 12, fill="#3478F6", outline="")
        yes_c.create_text(120, 28, text="Yes", fill="#FFFFFF", font=self.font_bold_md)
        yes_c.bind("<Button-1>", lambda e: self._show_dispense_hold())

        # No button — wider (240px)
        no_c = tk.Canvas(self.overlay_frame, width=240, height=56,
                         bg=t["bg"], highlightthickness=0)
        no_c.place(relx=0.5, y=300, anchor="n", x=130)
        self._canvas_rounded_rect(no_c, 0, 0, 240, 56, 12,
                                  fill=t["btn_bg"], outline=t["muted"])
        no_c.create_text(120, 28, text="No", fill=t["fg"], font=self.font_bold_md)
        no_c.bind("<Button-1>", lambda e: self._end_dispense())

    def _show_dispense_hold(self):
        """State 2: HOLD - hold to confirm with progress bar."""
        self.dispense_state = "HOLD"
        t = self.theme

        for w in self.overlay_frame.winfo_children():
            w.destroy()
        self.overlay_frame.configure(bg=t["bg"])
        self.overlay_frame.place(x=0, y=0, width=800, height=480)
        self._raise_widget(self.overlay_frame)
        self._raise_widget(self.d_btn_canvas)

        tk.Label(
            self.overlay_frame, text="Hold to confirm", font=self.font_name,
            bg=t["bg"], fg=t["fg"]
        ).place(relx=0.5, y=140, anchor="n")

        tk.Label(
            self.overlay_frame, text="Keep pressing to proceed...", font=self.font_md,
            bg=t["bg"], fg=t["muted"]
        ).place(relx=0.5, y=200, anchor="n")

        # Progress bar — wider (600px centered)
        self._hold_bar_canvas = tk.Canvas(
            self.overlay_frame, width=600, height=24,
            bg=t["bg"], highlightthickness=0
        )
        self._hold_bar_canvas.place(relx=0.5, y=260, anchor="n")
        # Track
        self._canvas_rounded_rect(self._hold_bar_canvas, 0, 0, 600, 24, 12,
                                  fill=t["btn_bg"], outline="")
        # Fill (starts at 0)
        self._hold_fill_id = self._canvas_rounded_rect(
            self._hold_bar_canvas, 0, 0, 1, 24, 12,
            fill="#3478F6", outline=""
        )

        # Touch area — bind to ALL widgets so tapping anywhere works
        for widget in [self.overlay_frame] + list(self.overlay_frame.winfo_children()):
            widget.bind("<ButtonPress-1>", self._hold_press)
            widget.bind("<ButtonRelease-1>", self._hold_release)

        self.hold_start = None
        self.hold_timer_id = None

    def _hold_press(self, event=None):
        if self.dispense_state != "HOLD":
            return
        self.hold_start = time.time()
        self._hold_update()

    def _hold_release(self, event=None):
        if self.dispense_state != "HOLD":
            return
        # Cancel
        self.hold_start = None
        if self.hold_timer_id:
            self.root.after_cancel(self.hold_timer_id)
            self.hold_timer_id = None
        # Reset bar
        try:
            self._hold_bar_canvas.delete(self._hold_fill_id)
            self._hold_fill_id = self._canvas_rounded_rect(
                self._hold_bar_canvas, 0, 0, 1, 24, 12,
                fill="#3478F6", outline=""
            )
        except Exception:
            pass

    def _hold_update(self):
        if self.hold_start is None or self.dispense_state != "HOLD":
            return
        elapsed = time.time() - self.hold_start
        frac = min(elapsed / 3.0, 1.0)
        width = max(2, int(600 * frac))

        try:
            self._hold_bar_canvas.delete(self._hold_fill_id)
            self._hold_fill_id = self._canvas_rounded_rect(
                self._hold_bar_canvas, 0, 0, width, 24, 12,
                fill="#3478F6", outline=""
            )
        except Exception:
            pass

        if frac >= 1.0:
            # Unbind to prevent accidental re-triggers
            self.overlay_frame.unbind("<ButtonPress-1>")
            self.overlay_frame.unbind("<ButtonRelease-1>")
            self.hold_start = None
            self._show_dispense_confirm()
            return

        self.hold_timer_id = self.root.after(50, self._hold_update)

    def _show_dispense_confirm(self):
        """State 3: CONFIRM - confirmed, press to dispense."""
        self.dispense_state = "CONFIRM"
        t = self.theme
        med = self.meds[self.dispense_pill_key]

        for w in self.overlay_frame.winfo_children():
            w.destroy()
        self.overlay_frame.configure(bg=t["bg"])
        self.overlay_frame.place(x=0, y=0, width=800, height=480)
        self._raise_widget(self.overlay_frame)
        self._raise_widget(self.d_btn_canvas)

        tk.Label(
            self.overlay_frame, text="CONFIRMED", font=self.font_name,
            bg=t["bg"], fg="#3478F6"
        ).place(relx=0.5, y=140, anchor="n")

        tk.Label(
            self.overlay_frame, text="Press down to dispense", font=self.font_md,
            bg=t["bg"], fg=t["muted"]
        ).place(relx=0.5, y=200, anchor="n")

        # Dispense button
        disp_c = tk.Canvas(self.overlay_frame, width=260, height=64,
                           bg=t["bg"], highlightthickness=0)
        disp_c.place(relx=0.5, y=270, anchor="n")
        self._canvas_rounded_rect(disp_c, 0, 0, 260, 64, 14,
                                  fill=med["accent"], outline="")
        disp_c.create_text(130, 32, text="Dispense", fill="#FFFFFF",
                           font=self.font_bold_lg)
        disp_c.bind("<Button-1>", lambda e: self._do_dispense())

    def _do_dispense(self):
        """Actually dispense: decrement count, play sound, show state 4."""
        med = self.meds[self.dispense_pill_key]
        if med["pills_left"] > 0:
            med["pills_left"] -= 1
        self._save_config()

        # Play alarm sound if enabled
        if self.settings.get("alarm_sound", True):
            self._play_sound()

        self._show_dispense_done()

    def _show_dispense_done(self):
        """State 4: DISPENSED - show confirmation, auto-return after 4s."""
        self.dispense_state = "DISPENSED"
        t = self.theme
        med = self.meds[self.dispense_pill_key]

        for w in self.overlay_frame.winfo_children():
            w.destroy()
        self.overlay_frame.configure(bg=t["bg"])
        self.overlay_frame.place(x=0, y=0, width=800, height=480)
        self._raise_widget(self.overlay_frame)
        self._raise_widget(self.d_btn_canvas)

        tk.Label(
            self.overlay_frame, text=f"Dispensed: {med['name']}", font=self.font_name,
            bg=t["bg"], fg=med["accent"]
        ).place(relx=0.5, y=180, anchor="n")

        # Bottom strip — full width with proper padding
        strip = tk.Frame(self.overlay_frame, bg=t["card_bg"], height=48)
        strip.place(x=0, y=432, width=800, height=48)
        tk.Label(
            strip, text="Please check before taking medication",
            font=self.font_md, bg=t["card_bg"], fg=t["muted"]
        ).place(relx=0.5, rely=0.5, anchor="center")

        # Auto-return
        self.dispense_return_id = self.root.after(4000, self._end_dispense)

    def _play_sound(self):
        try:
            subprocess.Popen(
                ["aplay", "/usr/share/sounds/alsa/Front_Center.wav"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except Exception:
            try:
                self.root.bell()
            except Exception:
                pass

    # ===================================================================
    # CAMERA / QR
    # ===================================================================
    def _start_camera(self):
        if not CAMERA_AVAILABLE:
            return
        try:
            self.camera = Picamera2()
            config = self.camera.create_still_configuration(
                main={"size": (320, 240)}
            )
            self.camera.configure(config)
            self.camera.start()
            self.camera_running = True
            self.camera_thread = threading.Thread(target=self._camera_loop, daemon=True)
            self.camera_thread.start()
        except Exception:
            self.camera_running = False

    def _camera_loop(self):
        while self.camera_running:
            try:
                frame = self.camera.capture_array()
                img = Image.fromarray(frame)
                codes = pyzbar_decode(img)
                for code in codes:
                    data = code.data.decode("utf-8").strip().lower()
                    if data in self.meds:
                        self.root.after(0, self._start_dispense, data)
                        time.sleep(3)
                        break
            except Exception:
                pass
            time.sleep(0.5)

    # ===================================================================
    # Run
    # ===================================================================
    def run(self):
        self.root.mainloop()
        # Cleanup
        self.camera_running = False
        if self.camera:
            try:
                self.camera.stop()
            except Exception:
                pass


# ===================================================================
# Entry point
# ===================================================================
if __name__ == "__main__":
    app = DoseApp()
    app.run()
