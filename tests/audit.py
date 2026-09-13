"""Full-app audit: boot the real DoseApp under Xvfb and drive every
screen, click zone, and flow. Any uncaught exception = a bug."""
import sys, os, traceback, importlib.util, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

spec = importlib.util.spec_from_file_location('dose_app', 'dose_app.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

FAILURES = []


def check(label, fn):
    try:
        fn()
        print(f"  OK   {label}")
    except Exception:
        FAILURES.append((label, traceback.format_exc()))
        print(f"  FAIL {label}")


app = mod.DoseApp()
# neutralize self-update — clicking UPDATE in the audit would overwrite
# the repo's dose_app.py with the GitHub version
app._fx_enabled = False   # synchronous navigation for the audit
app._on_update_pressed = lambda *a, **k: None
app._do_update_check = lambda *a, **k: None
app._apply_update = lambda *a, **k: None
app.root.update()

for key, name in mod.KNOWN_SLOTS.items():
    md = app.med_data[key]
    md.update({"name": name, "loaded": True, "count": 20})
    app.qr_last_seen[key] = time.time()
app._save_med()

for mode in ("home", "storage", "settings", "user"):
    check(f"draw {mode}", lambda m=mode: (app._nav(m), app.root.update()))

class FakeEvent:
    def __init__(self, x, y):
        self.x, self.y = x, y

def click_all_zones(mode):
    app._nav(mode)
    app.root.update()
    zones = list(app._click_zones)
    for (x1, y1, x2, y2, cb) in zones:
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        app._on_canvas_press(FakeEvent(cx, cy))
        app.root.update()
        if app.mode != mode:
            app._hide_keyboard(save=True)
            app.dispense_state = 0
            app.dispense_pill = None
            app.hold_start = 0
            if app.hold_after_id:
                app.root.after_cancel(app.hold_after_id)
                app.hold_after_id = None
            if app.spin_after_id:
                app.root.after_cancel(app.spin_after_id)
                app.spin_after_id = None
            app.mode = mode
            app._draw_frame()
            app.root.update()

for mode in ("home", "storage", "settings", "user"):
    check(f"click every zone on {mode}", lambda m=mode: click_all_zones(m))

def dispense_flow():
    app._nav("home"); app.root.update()
    app._start_dispense("blue"); app.root.update()
    assert app.mode == "hold", app.mode
    app.hold_start = time.time() - mod.HOLD_TIME - 0.1
    app._hold_update(); app.root.update()
    assert app.mode == "spin", app.mode
    app.spin_start = time.time() - mod.SPIN_TIME - 0.1
    app._spin_update(); app.root.update()
    assert app.mode == "confirmdisp", app.mode
    before = app.med_data["blue"]["count"]
    app._confirm_dispense(); app.root.update()
    assert app.mode == "dispensed", app.mode
    assert app.med_data["blue"]["count"] == before - 1
    app._end_dispense(); app.root.update()
    assert app.dispense_state == 0

check("full dispense flow (hold-spin-confirm-done)", dispense_flow)

def cancel_stages():
    for stage in ("hold", "spin", "confirmdisp"):
        app.mode = "home"
        app._start_dispense("red"); app.root.update()
        if stage in ("spin", "confirmdisp"):
            app.hold_start = time.time() - mod.HOLD_TIME - 0.1
            app._hold_update(); app.root.update()
        if stage == "confirmdisp":
            app.spin_start = time.time() - mod.SPIN_TIME - 0.1
            app._spin_update(); app.root.update()
        assert app.mode == stage, (app.mode, stage)
        cnt = app.med_data["red"]["count"]
        app._cancel_hold(); app.root.update()
        assert app.mode == "home" and app.med_data["red"]["count"] == cnt

check("cancel at hold/spin/confirm stages", cancel_stages)

def editor_flow():
    app._nav("storage")
    app.selected_pill = "yellow"
    app._draw_frame(); app.root.update()
    app._open_time_edit(); app.root.update()
    assert app.mode == "timeedit"
    app._te_adj(0, "h", 1)
    app._te_adj(0, "m", 5)
    app._te_adj(0, "h", 12)
    app._te_add()
    app._te_select(0)
    app._te_toggle_day(0)
    app._te_toggle_day(0)
    app.root.update()
    app._te_remove(1)
    app._te_done(); app.root.update()
    assert app.mode == "storage"
    assert len(app.med_data["yellow"]["dose_times"]) == 1

check("time editor lifecycle (storage)", editor_flow)

def addmed_flow():
    app.mode = "home"
    app._draft_slot = "demo"
    app._draft = {"name": "", "times_per_day": 1, "doses": [2],
                  "dose_times": ["8:00 AM"], "qty": 30, "days": [1] * 7}
    app._prev_mode = "home"
    app.mode = "addmed"
    app._draw_frame(); app.root.update()
    app._show_keyboard(); app.root.update()
    assert app._kbd_overlay is not None
    for ch in "IBUPROFEN":
        app._kbd_text += ch
    app._draw_keyboard(); app.root.update()
    app._hide_keyboard(save=True); app.root.update()
    assert app._draft["name"] == "IBUPROFEN", app._draft["name"]
    app._open_time_edit("draft"); app.root.update()
    assert app.mode == "timeedit"
    app._te_adj(0, "h", 2)
    app._te_done(); app.root.update()
    assert app.mode == "addmed"
    assert app._draft["dose_times"] == ["10:00 AM"]
    app._submit_add_med(); app.root.update()
    assert app.med_data["demo"]["name"] == "IBUPROFEN"
    assert app.med_data["demo"]["dose_times"] == ["10:00 AM"]
    assert app._demo_registered

check("add-med flow: keyboard + editor + submit", addmed_flow)

def kbd_survives_redraw():
    app.mode = "addmed"
    app._draft = {"name": "", "times_per_day": 1, "doses": [2],
                  "dose_times": ["8:00 AM"], "qty": 30, "days": [1] * 7}
    app._draw_frame(); app.root.update()
    app._show_keyboard(); app.root.update()
    ids_before = len(app._kbd_overlay.find_all())
    app._draw_frame(); app.root.update()
    ids_after = len(app._kbd_overlay.find_all())
    assert ids_before == ids_after and ids_after > 30
    assert len(app._kbd_imgs) > 0
    app._hide_keyboard()

check("keyboard survives background redraw", kbd_survives_redraw)

def no_hijack():
    app._demo_registered = False
    for mode in ("timeedit", "addmed", "qtyconfirm", "user", "settings"):
        app.mode = mode
        app._handle_qr_results([(0, '{"med":"New Medication","slot":"demo"}')])
        assert app.mode == mode, (mode, app.mode)
    app.mode = "home"
    app._handle_qr_results([(0, '{"med":"New Medication","slot":"demo"}')])
    assert app.mode == "addmed", app.mode
    app.mode = "home"
    app._demo_registered = True

check("QR events cannot hijack editor/addmed/etc.", no_hijack)

def banner_flow():
    from datetime import datetime
    now = datetime.now()
    ts = mod._fmt_time12(now.hour, now.minute)
    app.med_data["green"]["dose_times"] = [ts]
    app.adherence = {"events": []}
    app._due_prev = set()
    app._check_due_doses()
    assert "green" in app._due_keys, app._due_keys
    app.mode = "user"
    app._draw_frame(); app.root.update()
    zones = [z for z in app._click_zones if z[4] == app._dismiss_banner]
    assert zones, "banner click zone missing"
    app._dismiss_banner(); app.root.update()
    assert app._banner_dismissed
    app.adherence = {"events": [{"key": "green",
                                 "time": now.isoformat()}]}
    app._check_due_doses()
    assert "green" not in app._due_keys
    app.med_data["green"]["dose_times"] = ["12:00 PM"]
    app.mode = "home"

check("due banner: appears / dismisses / clears when taken", banner_flow)

def user_screen():
    from datetime import datetime
    app.adherence = {"events": [
        {"key": "blue", "time": datetime.now().isoformat(),
         "status": "on_time", "sched": ""},
        {"key": "red", "time": datetime.now().isoformat(),
         "status": "late", "sched": ""},
    ]}
    app._nav("user"); app.root.update()
    s = app._adherence_stats()
    assert s["total"] == 2 and s["on_time"] == 1 and s["late"] == 1

check("user screen renders with stats", user_screen)

def long_names():
    longname = "Hydroxychloroquine Sulfate Extended Release"
    app.med_data["blue"]["name"] = longname
    for mode in ("home", "storage"):
        app._nav(mode); app.root.update()
    fitted = app._fit_text(longname, app.font_name, 420)
    assert app.font_name.measure(fitted) <= 420
    app.med_data["blue"]["name"] = "Sertraline"

check("long names are ellipsized, never clipped", long_names)

def qty_flow():
    app.mode = "home"
    app._show_qty_confirm("red", "Lisinopril"); app.root.update()
    assert app.mode == "qtyconfirm"
    app._qty_adjust(1); app._qty_adjust(-1); app.root.update()
    app._qty_cancel(); app.root.update()
    assert app.mode == "home"
    app._qty_cancel_time = 0.0

check("qty confirm overlay", qty_flow)

def alert_flow():
    from datetime import datetime
    now = datetime.now()
    ts = mod._fmt_time12(now.hour, now.minute)
    app.med_data["red"].update({"loaded": True, "count": 5,
                                "dose_times": [ts]})
    app.adherence = {"events": []}
    app._due_prev = set()
    app.mode = "home"
    app._check_due_doses(); app.root.update()
    assert app.mode == "dosealert", app.mode
    assert app._alert_key == "red"
    app._alert_dismiss(); app.root.update()
    assert app.mode == "home" and not app._banner_dismissed
    app._due_prev = set(); app.mode = "home"
    app._check_due_doses(); app.root.update()
    assert app.mode == "dosealert"
    app._alert_dispense(); app.root.update()
    assert app.mode == "hold" and app.dispense_pill == "red"
    app._cancel_hold(); app.root.update()
    app.med_data["red"]["dose_times"] = ["9:00 AM"]
    app._due_prev = set()
    app._due_keys = {}

check("full-screen dose alert flow", alert_flow)

def dose_status():
    from datetime import datetime
    now = datetime.now()
    past_min = max(5, now.hour * 60 + now.minute - 180)
    fut_min = min(23 * 60 + 55, now.hour * 60 + now.minute + 180)
    ts_past = mod._fmt_time12(*divmod(past_min, 60))
    ts_now = mod._fmt_time12(now.hour, now.minute)
    ts_future = mod._fmt_time12(*divmod(fut_min, 60))
    app.adherence = {"events": [{"key": "blue",
                                 "time": now.isoformat()}]}
    assert app._dose_status("blue", ts_now) == "taken"
    assert app._dose_status("red", ts_past) == "missed"
    assert app._dose_status("red", ts_future) is None
    app._nav("home"); app.root.update()

check("per-dose status icons (taken/missed/upcoming)", dose_status)

def polish_fixes():
    app._nav("storage")
    app.selected_pill = "green"
    app.med_data["green"]["dose_times"] = ["8:00 AM"]
    app._open_time_edit(); app.root.update()
    app._te_adj(0, "h", 3)
    app._nav("home"); app.root.update()
    assert app.med_data["green"]["dose_times"] == ["11:00 AM"]
    app._demo_registered = False
    app.mode = "home"
    app._handle_qr_results([(0, '{"med":"New Medication","slot":"demo"}')])
    assert app.mode == "addmed"
    app._cancel_add_med(); app.root.update()
    assert app.mode == "home"
    app._handle_qr_results([(0, '{"med":"New Medication","slot":"demo"}')])
    assert app.mode == "home", "popup reopened during cooldown"
    app._addmed_cancel_time = 0.0
    app._demo_registered = True

check("editor save-on-nav + popup cancel cooldowns", polish_fixes)

def voice_integration():
    # graceful degradation: this python has no voice libs installed,
    # so the app must run with voice disabled and say why
    assert app._voice_status_text() != ""
    # overlay lifecycle works even without the engine
    app._nav("home"); app.root.update()
    app._voice_overlay_update("listening", "test transcript", "")
    app.root.update()
    assert app.canvas.find_withtag("voice_ov")
    app._voice_overlay_update("speaking", "", "Reply text here")
    app.root.update()
    app._voice_overlay_update("idle")
    app.root.update()
    assert not app.canvas.find_withtag("voice_ov")
    # med-info bridge
    lines = app._med_info_for("Atorvastatin")
    assert any("grapefruit" in l.lower() for l in lines)
    # settings row exists
    app._nav("settings"); app.root.update()
    app._toggle_setting("voice_enabled")
    app._toggle_setting("voice_enabled")

check("voice integration: degradation, overlay, bridge, settings",
      voice_integration)

def qr_sanitization():
    evil = '{"med":"' + "X" * 300 + '\\u0007","slot":"demo"}'
    p = app._parse_qr_payload(evil)
    assert p and len(p[1]) <= 40
    assert app._parse_qr_payload('{"med": 5, "slot": ["x"]}') is None
    assert app._parse_qr_payload("A" * 9999) is None

check("QR payload sanitization", qr_sanitization)

def fx_transition():
    # animated nav completes and lands on the right screen
    app._fx_enabled = True
    app.mode = "home"
    app._nav("storage")
    for _ in range(12):
        app.root.update()
        time.sleep(0.03)
    assert app.mode == "storage", app.mode
    assert not app.canvas.find_withtag("fx_veil"), "veil left behind"
    app._fx_enabled = False
    app._nav("home"); app.root.update()

check("soft screen transition completes and cleans up", fx_transition)

def no_clipping_anywhere():
    # extreme data: longest plausible names, 3 dose times, very long
    # label directions — no text may leave the screen bounds
    mod.MED_INFO["stresstestazine"] = [
        "Take with a very large glass of water and a full meal to "
        "protect the stomach lining from irritation, every single "
        "time, without exception, even when traveling",
        "Avoid grapefruit juice, alcohol, antacids, and direct "
        "sunlight for four hours after each dose is taken",
        "Store below twenty five degrees in the original container "
        "away from moisture and out of the reach of children",
    ]
    app.med_data["blue"].update({
        "name": "Stresstestazine Hydrochloride XR 250MG",
        "dose_times": ["8:05 AM", "1:35 PM", "9:55 PM"]})
    app.selected_pill = "blue"

    def assert_in_bounds(where):
        for item in app.canvas.find_all():
            bb = app.canvas.bbox(item)
            if not bb:
                continue
            x1, y1, x2, y2 = bb
            assert x1 >= -2 and y1 >= -2 and x2 <= mod.SCREEN_W + 2                 and y2 <= mod.SCREEN_H + 2, (where, item, bb)

    for mode in ("home", "storage", "settings", "user"):
        app._nav(mode); app.root.update()
        assert_in_bounds(mode)
    app._nav("storage"); app.root.update()
    app._open_time_edit(); app.root.update()
    assert_in_bounds("timeedit")
    app._te_done(); app.root.update()
    app.med_data["blue"].update({"name": "Sertraline",
                                 "dose_times": ["8:00 AM"]})
    app._nav("home"); app.root.update()

check("no clipping anywhere with extreme-length data",
      no_clipping_anywhere)

print()
if FAILURES:
    print(f"=== {len(FAILURES)} FAILURES ===")
    for label, tb in FAILURES:
        print(f"\n--- {label} ---\n{tb}")
    sys.exit(1)
print("=== ALL AUDIT CHECKS PASSED ===")
app.root.destroy()
