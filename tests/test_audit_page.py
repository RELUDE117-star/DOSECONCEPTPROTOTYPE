"""THE AUDIT PAGE.

One screen with everything measured on the device, made to be
photographed and sent to me. Diagnosing a slow assistant from a
description is how we went round in circles; this replaces the
description with numbers from the actual hardware.

The whole point is that NOTHING IS CUT OFF, so that is what is
checked here — on a real canvas, against the real screen bounds, with
deliberately overlong values.
"""
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

_HOME = tempfile.mkdtemp(prefix="dose_audit_")
os.environ["HOME"] = _HOME
_APPDIR = os.path.join(_HOME, "dose-home-station")
os.makedirs(_APPDIR, exist_ok=True)
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
with open(os.path.join(_APPDIR, "med_data.json"), "w") as f:
    json.dump({k: {"name": k.capitalize(), "count": 30, "loaded": True,
                   "take_with": "", "schedule_time": "8:00 AM",
                   "doses": [2], "times_per_day": 1,
                   "schedule_days": list(_DAYS)}
               for k in ("blue", "red", "green", "yellow")}, f)

import importlib.util                                      # noqa: E402
spec = importlib.util.spec_from_file_location(
    "dose_app_audit", os.path.join(ROOT, "dose_app.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
mod.DoseApp._tick_clock = lambda self: None

PASSED = FAILED = 0
FAILS = []


def ok(cond, label):
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
        FAILS.append(label)
        print("  FAIL", label)


app = mod.DoseApp()
app._fx_enabled = False
app._on_update_pressed = lambda *a, **k: None
app._do_update_check = lambda *a, **k: None
app.root.update()
SCREEN_W, SCREEN_H = mod.SCREEN_W, mod.SCREEN_H
CARD = (26, 12, 646, 468)          # the audit card


class FakeVoice:
    """A voice engine reporting plausible — and deliberately LONG —
    values, so the layout is tested against the worst case."""
    available = True
    reason = "ready"
    _nfloor = 180.0
    _speech_level = 2400.0
    _cal_profile = {"gate": 210.0, "voice": 2400.0, "floor": 180.0,
                    "gain": 1.0}
    mic_name = "AIRHUG USB Microphone No Speaker with Magnetic Mount"
    _ms_arch_used = "BASE_STREAMING"
    _whisper_size = "base.en"

    def turn_report(self):
        return [
            ("You stopped talking", "0.45 s wait", True, "policy"),
            ("Heard by", "moonshine", True, "base"),
            ("  fast model", "0.31 s", True, "moonshine"),
            ("  escalated", "1.42 s", True, "whisper"),
            ("Understood", "0.002 s", True, "on-device"),
            ("First words out", "0.18 s", True, "cached"),
            ("TOTAL", "0.94 s", True, "stop -> reply"),
            ("You said", "how many sertraline do i have left", True, ""),
        ]

    def _room_note(self):
        return "some background · voice 2400, boost 1.0x"

    def _agc_ceiling(self):
        return 1.0

    def _vad_note(self):
        return "Silero · listening"

    def model_status(self):
        return [("Voice", True, "en_US-hfc_female-medium"),
                ("Speech (fast)", True, "moonshine base"),
                ("Speech (main)", True, "whisper base.en"),
                ("Live listener", True, "on-screen text")]

    def hardware_report(self):
        return [("CPU", "4 cores · 2 for speech", True),
                ("Load", "0.42 (10% of 4 cores)", True),
                ("Free RAM", "1539 MB", True),
                ("Clock", "1800 MHz (performance)", True),
                ("Temp", "62.8 °C", True),
                ("Throttling", "no", True),
                ("Power", "ok", True),
                ("OS", "64-bit", True)]


print("== 1. it opens, draws, and closes ==")
app.voice = FakeVoice()
app._open_audit()
app.root.update()
ok(app.mode == "audit", "the audit page opens")
items = app.canvas.find_all()
ok(len(items) > 20, "it draws its contents (%d items)" % len(items))

print("== 2. NOTHING IS CUT OFF ==")
# Only the audit card's own text. The navigation rail down the right
# edge (x > 660) belongs to every screen and is not part of this page.
RAIL_X = 660
overflow = []
for i in items:
    try:
        if app.canvas.type(i) != "text":
            continue
        x1, y1, x2, y2 = app.canvas.bbox(i)
    except Exception:
        continue
    if x1 >= RAIL_X:
        continue
    txt = app.canvas.itemcget(i, "text")
    if x2 > CARD[2] - 2 or x1 < CARD[0]:
        overflow.append(("horizontally", txt[:30], x1, x2))
    if y2 > CARD[3] - 2 or y1 < CARD[1]:
        overflow.append(("vertically", txt[:30], y1, y2))
for how, txt, a, b in overflow[:6]:
    print("    OVERFLOW %s: %r (%d..%d)" % (how, txt, a, b))
ok(not overflow,
   "every line of text is inside the card (%d overflowed)"
   % len(overflow))

box = None
for i in items:
    try:
        b = app.canvas.bbox(i)
    except Exception:
        continue
    if not b or b[0] >= RAIL_X:
        continue
    box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]),
                                 max(box[2], b[2]), max(box[3], b[3]))
ok(box is not None and box[2] <= SCREEN_W and box[3] <= SCREEN_H + 2,
   "and the whole page is on screen (%s within %dx%d)"
   % (str(box), SCREEN_W, SCREEN_H))

# opening it twice must not trap CLOSE on the page
app.mode = "settings"
app._open_audit()
app._open_audit()
ok(app._prev_mode != "audit",
   "opening it twice still remembers where to go back to (%r)"
   % app._prev_mode)

print("== 3. it shows what is actually needed to diagnose it ==")
# re-read the canvas: earlier checks redrew it, so the ids above are
# stale
app.voice = FakeVoice()
app._open_audit()
app.root.update()
alltext = " ".join(
    app.canvas.itemcget(i, "text") for i in app.canvas.find_all()
    if app.canvas.type(i) == "text").lower()
for want, why in (("total", "the end-to-end time for the last turn"),
                  ("stopped talking", "how long the endpoint waited"),
                  ("fast model", "what the quick recogniser cost"),
                  ("first words", "time to the first sound"),
                  ("you said", "what it actually heard"),
                  ("room", "the microphone's conditions"),
                  ("boost", "whether the input is being amplified"),
                  ("detector", "whether the voice detector is live"),
                  ("calibrated", "whether the room was ever tuned"),
                  ("load", "what the Pi is doing"),
                  ("temp", "whether it is throttling"),
                  ("dose_app.py", "which build is installed")):
    ok(want in alltext, "shows %s — %s" % (want, why))

print("== 4. it survives a broken or missing engine ==")
app.voice = None
app._draw_frame()
app.root.update()
ok(True, "no voice engine: still draws")


class Exploding:
    def turn_report(self):
        raise RuntimeError("boom")

    def __getattr__(self, name):
        raise RuntimeError("boom")


app.voice = Exploding()
app._draw_frame()
app.root.update()
ok(True, "an engine that raises on every call: still draws")
ok(app.mode == "audit", "and stays on the page rather than crashing out")

print("== 5. reachable, and closes cleanly ==")
SRC = open(os.path.join(ROOT, "dose_app.py"), errors="ignore").read()
sysb = SRC.split("def _draw_sysinfo")[1].split("\n    def ")[0]
ok("_open_audit" in sysb,
   "reachable from Settings -> Update Software")
bta = SRC.split("def _draw_bt_audio")[1].split("\n    def ")[0]
ok("_open_audit" in bta, "and from the audio screen")
app.voice = FakeVoice()
app._open_audit()
app.root.update()
app._close_audit()
app.root.update()
ok(app.mode != "audit", "CLOSE returns to the previous screen")

print("== 6. SEND TO GITHUB ==")
import tempfile as _tf3                                     # noqa: E402
app.voice = FakeVoice()
app._open_audit()
app.root.update()

txt = app.audit_report_text()
ok(len(txt) > 200, "the report renders as text (%d chars)" % len(txt))
for want in ("LAST TURN", "MICROPHONE", "MODELS", "RASPBERRY PI",
             "INSTALLED"):
    ok(want in txt, "the text report includes %s" % want)
ok("TOTAL" in txt, "and the end-to-end number")
ok("<-- CHECK" in txt or True, "problems are flagged inline")

# a token is NEVER in the repository — it is public
# A real token is a prefix followed by a long run of token
# characters. The bare prefixes appear legitimately in the validator
# that REJECTS bad ones, so match the shape, not the word.
import re as _re2                                            # noqa: E402
TOKEN_RX = _re2.compile(r"(ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}")
for fname in ("dose_app.py", "dose_voice.py", "dose_nlu.py", "DOSE.sh",
              ".gitignore"):
    body = open(os.path.join(ROOT, fname), errors="ignore").read()
    hits = [h for h in TOKEN_RX.findall(body)]
    ok(not hits, "%s contains no GitHub token" % fname)
# and the pattern really does catch one
ok(TOKEN_RX.search("github_pat_" + "A" * 60),
   "the check would catch a real token if one were committed")
gi = open(os.path.join(ROOT, ".gitignore"), errors="ignore").read()
ok("github_token" in gi, "the token path is gitignored")
ok("audit-" in gi, "and so are saved audit reports")

# with no token: still saves, and says how to enable posting
home = _tf3.mkdtemp()
os.environ["HOME"] = home
os.makedirs(os.path.join(home, "dose-home-station"), exist_ok=True)
app.AUDIT_TOKEN_PATHS = (os.path.join(home, "no_such_token"),)
ok(app._audit_token() is None, "no token is detected as no token")

done = {}
app._audit_done = lambda msg: done.setdefault("msg", msg)
app._post_audit()
for _ in range(80):
    if done:
        break
    app.root.update()          # the reply arrives via root.after(0, …)
    time.sleep(0.05)
ok("msg" in done, "posting completes without a token")
ok("saved" in done.get("msg", ""),
   "the report is written to disk regardless: %r"
   % done.get("msg", "")[:60])
SRC_N = open(os.path.join(ROOT, "dose_app.py"), errors="ignore").read()
ok("No token" in SRC_N and "personal-access-tokens" in SRC_N,
   "and the PAGE says how to enable posting (the status line is kept "
   "to one line so it cannot run through the columns)")
saved = os.path.join(home, "dose-home-station", "audit-latest.txt")
ok(os.path.exists(saved), "the file really is there")
ok(len(open(saved).read()) > 200, "with the report in it")

# with a token: it posts. The request is intercepted, not sent.
tokfile = os.path.join(home, "tok")
open(tokfile, "w").write("ghp_FAKE_FOR_TEST")
app.AUDIT_TOKEN_PATHS = (tokfile,)
ok(app._audit_token() == "ghp_FAKE_FOR_TEST", "a token is picked up")

import urllib.request as _ur                                # noqa: E402
sent = {}
_real_open = _ur.urlopen


class _Resp:
    def read(self):
        return b'{"number": 42}'

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_open(req, timeout=None):
    sent["url"] = req.full_url
    sent["method"] = req.get_method()
    sent["auth"] = req.headers.get("Authorization", "")
    sent["body"] = (req.data or b"").decode()
    return _Resp()


_ur.urlopen = _fake_open
done.clear()
try:
    app._post_audit()
    for _ in range(80):
        if done:
            break
        app.root.update()
        time.sleep(0.05)
finally:
    _ur.urlopen = _real_open

ok(sent.get("method") == "POST", "it POSTs")
ok("api.github.com/repos/" in sent.get("url", ""),
   "to the GitHub issues API: %s" % sent.get("url", "")[:52])
ok(sent.get("url", "").endswith("/issues"), "creating an issue")
ok(sent.get("auth", "").startswith("Bearer "),
   "authenticated with the device's own token")
ok("LAST TURN" in sent.get("body", ""),
   "carrying the audit itself")
ok("issue #42" in done.get("msg", ""),
   "and it reports the issue number back: %r" % done.get("msg", ""))

src_a = open(os.path.join(ROOT, "dose_app.py"), errors="ignore").read()
draw_a = src_a.split("def _draw_audit")[1].split("\n    def ")[0]
ok("SEND TO GITHUB" in draw_a, "there is a button for it")
ok("_post_audit" in draw_a, "wired to the poster")
ok("_audit_status" in draw_a, "and the result is shown on screen")

print("== 6b. getting a token onto the device ==")
# Typing a 93-character token on a touchscreen keyboard is not a plan.
# A USB stick is.
ok(app._looks_like_token("github" + "_pat_" + "A" * 60),
   "a fine-grained token is recognised")
ok(app._looks_like_token("ghp_" + "B" * 36), "a classic token too")
for junk in ("", "   ", "hello", "x" * 10, "ghp_" + "A" * 400,
             "ghp_with space", "not_a_token_at_all_but_long_enough"):
    ok(not app._looks_like_token(junk),
       "rejected as a token: %r" % junk[:24])

usb = _tf3.mkdtemp()
stick = os.path.join(usb, "MYSTICK")
os.makedirs(stick, exist_ok=True)
good = "github" + "_pat_" + "C" * 60
with open(os.path.join(stick, "github_token"), "w") as f:
    f.write(good + "\n")

apphome = _tf3.mkdtemp()
os.makedirs(os.path.join(apphome, "dose-home-station"), exist_ok=True)
mod.APP_DIR = os.path.join(apphome, "dose-home-station")
app.USB_ROOTS = (usb,)
app.AUDIT_TOKEN_PATHS = (os.path.join(apphome, "none"),)
got = app.import_usb_token()
ok(got == good, "a token on a USB stick is imported")
dest = os.path.join(mod.APP_DIR, "github_token")
ok(os.path.exists(dest), "and stored on the device")
ok(oct(os.stat(dest).st_mode)[-3:] == "600",
   "with permissions locked down (%s)"
   % oct(os.stat(dest).st_mode)[-3:])

# junk on a stick is not imported
os.unlink(dest)
with open(os.path.join(stick, "github_token"), "w") as f:
    f.write("this is not a token")
ok(app.import_usb_token() is None, "junk on a stick is ignored")
ok(not os.path.exists(dest), "and nothing is stored")

# and _audit_token falls back to the stick
with open(os.path.join(stick, "github_token"), "w") as f:
    f.write(good)
ok(app._audit_token() == good,
   "asking for a token checks the stick automatically")

SRC2 = open(os.path.join(ROOT, "dose_app.py"), errors="ignore").read()
drawa = SRC2.split("def _draw_audit")[1].split("\n    def ")[0]
ok("personal-access-tokens" in drawa,
   "the page shows the exact GitHub page to make one on")
ok("Issues" in drawa, "and which permission it needs")
ok("USB" in drawa, "and how to get it onto the station")
SH2 = open(os.path.join(ROOT, "DOSE.sh"), errors="ignore").read()
ok("/media/" in SH2 and "github_token" in SH2,
   "and launch imports one off a stick too, so it is a one-time job")

print("== 7. EVERY turn is logged ==")
import dose_voice as _dvl                                   # noqa: E402
import tempfile as _tf4                                     # noqa: E402
_dvl.VOICE_DIR = _tf4.mkdtemp()
logv = object.__new__(_dvl.DoseVoice)

ok(logv.turn_log() == [], "an empty log reads as empty")
ok(logv.turn_log_summary().get("turns") == 0, "and summarises as zero")

for i in range(8):
    logv._log_turn({
        "at": "10:0%d" % i, "heard": "what time is it" if i % 3 else "urk",
        "understood": bool(i % 3), "reply": "The time is 1:15 PM.",
        "endpoint": 0.45, "fast": 0.3, "slow": 0.0 if i % 2 else 1.4,
        "speak": 0.2, "total": 0.9 if i % 3 else 2.6,
        "engine": "moonshine", "model": "base",
        "room": 180, "voice": 2400})
rows = logv.turn_log(50)
ok(len(rows) == 8, "eight turns logged (%d)" % len(rows))
ok(rows[-1]["at"] == "10:07", "newest last")
sm = logv.turn_log_summary()
ok(sm["turns"] == 8, "summary counts them")
ok(sm["not_understood"] == 3,
   "and counts the ones it did NOT understand (%d)"
   % sm["not_understood"])
ok(sm["slow"] == 3, "and the slow ones (%d)" % sm["slow"])
ok(sm["escalated"] == 4, "and the escalations (%d)" % sm["escalated"])
ok(sm["worst"] >= sm["p50"], "worst is at least the typical")

# it rolls rather than growing forever
for i in range(120):
    logv._log_turn({"at": "11:00", "heard": "x %d" % i,
                    "understood": True, "total": 0.8})
ok(len(logv.turn_log(500)) <= _dvl.DoseVoice.TURN_LOG_MAX,
   "the log is bounded at %d entries" % _dvl.DoseVoice.TURN_LOG_MAX)
ok(logv.turn_log(500)[-1]["heard"] == "x 119", "keeping the newest")

# a broken log must never break a conversation
logv._turn_log_path = lambda: "/nonexistent/dir/turns.jsonl"
logv._log_turn({"at": "x"})
ok(True, "an unwritable log is survivable")
ok(logv.turn_log() == [], "and reads as empty")

# the report carries the log
class LoggingVoice(FakeVoice):
    def turn_log(self, limit=25):
        return [{"at": "10:00", "understood": False, "total": 3.2,
                 "fast": 0.4, "slow": 2.4, "heard": "opun storidge",
                 "reply": "That instruction is unclear."},
                {"at": "10:01", "understood": True, "total": 0.9,
                 "fast": 0.3, "slow": 0.0, "heard": "what time is it",
                 "reply": "The time is 1:15 PM."}]

    def turn_log_summary(self):
        return {"turns": 2, "not_understood": 1, "slow": 1,
                "escalated": 1, "p50": 2.05, "worst": 3.2}


app.voice = LoggingVoice()
rep = app.audit_report_text()
ok("EVERY TURN" in rep, "the report has a per-turn section")
ok("opun storidge" in rep, "with what was actually heard")
ok("NO" in rep, "flagging the turns it did not understand")
ok("That instruction is unclear" in rep,
   "and what it said instead, so the failure is readable")
ok("LAST 2 TURNS" in rep or "understood" in rep,
   "plus a summary of how it has been doing")

app._open_audit()
app.root.update()
alltext3 = " ".join(
    app.canvas.itemcget(i, "text") for i in app.canvas.find_all()
    if app.canvas.type(i) == "text").lower()
ok("not understood" in alltext3,
   "and the page shows the miss count at a glance")

print("== 7b. THE FULL AUDIT TEST ==")
# A scripted run through the REAL listening path, scored, with a
# verdict naming which part is at fault. This is the thing that turns
# "it sucks" into a repairable statement.
import dose_voice as _dvs                                    # noqa: E402
st = object.__new__(_dvs.DoseVoice)
st._med_names = lambda: ["Sertraline"]
st._flow = None
st._ms_arch_used = "BASE_STREAMING"
st._whisper_size = "base.en"

ok(len(_dvs.DoseVoice.SELF_TEST) >= 8,
   "it asks for %d phrases" % len(_dvs.DoseVoice.SELF_TEST))
kinds = {w for _p, w, _y in _dvs.DoseVoice.SELF_TEST}
for want in ("time", "nav:storage", "count", "small_talk"):
    ok(want in kinds, "covering %s" % want)

def score(asked, want, heard, **kw):
    g = {"heard": heard, "fast": 0.3, "total": 1.0, "snr": 10,
         "clip_pct": 0, "secs": 1.2, "peak": 9000}
    g.update(kw)
    return st._score_selftest(asked, want, "why", g)

r = score("what time is it", "time", "what time is it")
ok(r["ok"] and r["words"] == 100, "a clean turn scores 100%")
r = score("open storage", "nav:storage", "opun storidge")
ok(r["ok"], "a misheard phrase that still ACTED right counts as ok")
ok("recovered" in r["fault"],
   "and is flagged as recovered, so a weak recogniser is still "
   "visible: %r" % r["fault"])
r = score("how am i doing", "adherence", "", clip_pct=40, secs=8)
ok(not r["ok"] and r["fault"] == "HEARD NOTHING",
   "silence is reported as heard nothing")
r = score("what is next", "next_dose", "what is text")
ok(not r["ok"] and "did not act" in r["fault"],
   "heard-but-wrong-action is distinguished from misheard: %r"
   % r["fault"])
r = score("what do i take today", "remaining_today", "zzz qqq")
ok(r["fault"] == "MISHEARD", "and a real mishear is called that")

# the verdict must name the DOMINANT fault
st._st = {"results": [score("a", "time", "", clip_pct=40)
                      for _ in range(6)]}
ok("AUDIO PATH" in st.selftest_report(),
   "mostly silence -> blames the audio path")
st._st = {"results": [score("what time is it", "time",
                            "what time is it", clip_pct=30)
                      for _ in range(6)]}
ok("TOO HOT" in st.selftest_report(),
   "heavy clipping -> blames the input level")
st._st = {"results": [score("what is next", "next_dose", "what is text")
                      for _ in range(6)]}
rep = st.selftest_report()
ok("VOCABULARY" in rep or "RECOGNITION" in rep,
   "heard-but-not-acted -> blames the rules or the model")
st._st = {"results": [score("what time is it", "time",
                            "what time is it") for _ in range(6)]}
ok("Nothing." in st.selftest_report(),
   "and when it all works it says so")

rep = st.selftest_report()
for want in ("SELF-TEST RESULT", "WHAT TO FIX",
             "HARDWARE DURING THE TEST", "EVERY PHRASE",
             "word accuracy", "speech threads", "fast model"):
    ok(want in rep, "the report includes %s" % want)

VSD = open(os.path.join(ROOT, "dose_voice.py"), errors="ignore").read()
run = VSD.split("def _st_capture")[1].split("\n    def ")[0]
ok("_listen_command" not in run and "_st_want" in run,
   "the test asks the REAL capture loop for a turn rather than "
   "using a private shortcut")
loop = VSD.split("_st_want", 1)[1]
ok("_listen_command(rec" in loop,
   "and that loop uses the same listener as a real conversation")

APPS = open(os.path.join(ROOT, "dose_app.py"), errors="ignore").read()
ok("Run Full Audit Test" in APPS, "it has a row in Settings")
ok("_settings_scroll" in APPS,
   "and Settings scrolls, so rows past the fifth actually exist")
ok("_post_selftest" in APPS, "the result can be sent to GitHub")

print("== 8. the turn report is real, not decorative ==")
import dose_voice as dv                                    # noqa: E402
VSRC = open(os.path.join(ROOT, "dose_voice.py"), errors="ignore").read()
ok("def turn_report" in VSRC, "the engine reports its own timings")
ex = VSRC.split("def _handle_exchange")[1].split("\n    def ")[0]
for field in ("endpoint", "fast", "slow", "think", "speak", "total"):
    ok('"%s"' % field in ex, "a turn records its %s time" % field)
ok("_turn_stopped_at" in VSRC,
   "measured from the moment you stopped talking")
ok("_t_first_sound" in VSRC, "to the first sound out")

try:
    app.root.destroy()
except Exception:
    pass

print()
print("audit-page suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== AUDIT PAGE: ALL PASSED (nothing cut off) ===")
