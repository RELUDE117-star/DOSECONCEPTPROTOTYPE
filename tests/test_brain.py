"""Text-level battery for the DoseVoice brain: every intent, fuzzy
med names, the full add-medication conversation, dispense confirm."""
import sys, re, os, tempfile
os.environ["DOSE_VOICE_DIR"] = tempfile.mkdtemp(prefix="dose_voice_test_")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta
from dose_voice import DoseVoice

class FakeRoot:
    def after(self, _ms, fn, *a):
        fn(*a)

class FakeApp:
    def __init__(self):
        self.root = FakeRoot()
        self.med_data = {
            "blue": {"name": "Sertraline", "loaded": True, "count": 28,
                     "dose_times": ["8:00 AM"],
                     "schedule_days": ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]},
            "yellow": {"name": "Atorvastatin", "loaded": True, "count": 30,
                       "dose_times": ["9:00 PM"],
                       "schedule_days": ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]},
            "demo": {"name": "Demo", "loaded": False, "count": 0},
        }
        self._demo_registered = False
        self.saved = False
        self.dispensed = None
        self.nav_to = None
        self.due = {}
        self.statuses = {}
        self.stats = {"score": 92, "on_time": 11, "late": 1, "missed": 0}
    def _dose_due_map(self):
        return self.due
    def _get_today_schedule(self):
        now = datetime.now()
        return [
            {"key": "blue", "name": "Sertraline", "time": "8:00 AM",
             "count": 28, "sort": now.replace(hour=8, minute=0)},
            {"key": "yellow", "name": "Atorvastatin", "time": "9:00 PM",
             "count": 30, "sort": now.replace(hour=21, minute=0)},
        ]
    def _dose_status(self, key, ts):
        return self.statuses.get((key, ts))
    def _adherence_stats(self):
        return self.stats
    def _start_dispense(self, key):
        self.dispensed = key
    def _nav(self, mode):
        self.nav_to = mode
    def _save_med(self):
        self.saved = True
    def _draw_frame(self):
        pass
    def _med_info_for(self, name):
        info = {"atorvastatin": ["Cholesterol (statin)",
                                 "Avoid grapefruit juice"]}
        return info.get(name.strip().lower(),
                        ["Follow the directions on your label"])

def make_voice(app):
    v = object.__new__(DoseVoice)
    v.app = app
    v._flow = None
    v.state = "idle"
    v._last_reply = ""
    v._last_exchange = None
    v._learn = v._learn_load()
    return v

app = FakeApp()
v = make_voice(app)
FAIL = []

def ask(text, *expect_any, keep=None, forbid=None):
    reply, keep_listening = v.respond(text)
    low = reply.lower()
    ok = all(any(e.lower() in low for e in (exp if isinstance(exp, tuple) else (exp,)))
             for exp in expect_any)
    if forbid and any(f.lower() in low for f in forbid):
        ok = False
    if keep is not None and keep_listening != keep:
        ok = False
    tag = "OK " if ok else "FAIL"
    if not ok:
        FAIL.append((text, reply))
    print(f"{tag} {text!r}\n      -> {reply!r}  (keep={keep_listening})")
    return reply

# identity & personality
ask("who are you", ("dose",))
ask("what are your protocols", "protocol one", "protocol three")
ask("tell me a joke", ("pill", "medication", "skeleton"))
ask("thank you", ("welcome", "protocol", "here for"))
ask("hello", ("ryan",))
ask("how are you", ("nominal", "operational", "functioning"))
ask("help", "add a new medication")

# queries
ask("what time is it", "the time is")
ask("what day is it", "today is")

# remaining today: none taken yet -> both listed
ask("what pills do i still have today", "sertraline", "atorvastatin")
# after taking sertraline -> only atorvastatin
app.statuses[("blue", "8:00 AM")] = "taken"
ask("what do i still have to take today", "atorvastatin", forbid=["sertraline"])

# next dose (nothing due now, sertraline taken -> evening atorvastatin)
r = ask("hey what do i need to take next", ("atorvastatin", "nothing"))
# with a dose due now
app.due = {"yellow": "9:00 PM"}
ask("what should i take", "atorvastatin", "due now")
app.due = {}

# counts + fuzzy names (vosk mangles: "certain lean" ~ sertraline)
ask("how many sertraline do i have left", "28")
ask("how many certain lean pills do i have left", "sertraline", "28")
ask("how many pills do i have", "sertraline", "atorvastatin")

# schedule + info
ask("when do i take atorvastatin", "nine pm", "every day")
ask("how do i take atorvastatin", "grapefruit")
ask("did i take my sertraline today", "affirmative")
ask("did i take atorvastatin", "negative")

# adherence
ask("how am i doing", "92 percent", ("exceptional", "solid"))

# navigation
ask("open the storage", "storage")
assert app.nav_to == "storage"

# dispense: SAFETY — voice never dispenses, never starts the flow
ask("dispense my atorvastatin", "safety protocol", "never dispense",
    "hold", keep=False)
assert app.dispensed is None, "voice must NEVER start a dispense"
assert app.nav_to == "home"
ask("give me my sertraline", "never dispense", keep=False)
assert app.dispensed is None

# empty bottle
app.med_data["yellow"]["count"] = 0
ask("dispense atorvastatin", "empty")
assert app.dispensed is None
app.med_data["yellow"]["count"] = 30

# ── medical-safety gate: hard referral, no advice, ever ──
for q in ("can i take a double dose of sertraline",
          "should i skip my dose tonight",
          "can i drink alcohol with atorvastatin",
          "what happens if i take more pills",
          "can i stop taking metformin"):
    ask(q, "cannot give medical advice", "pharmacist", keep=False)
    assert app.dispensed is None

# ── full add-medication conversation ──
ask("add a new medication", "what is the medication called", keep=True)
ask("ibuprofen", "read me the label", keep=True)
ask("take one tablet by mouth at seven thirty pm thirty pills",
    "confirm intake", "ibuprofen", "30 pills", "seven 30 PM", keep=True)
ask("yes that is correct", "registered", keep=False)
assert app.med_data["demo"]["loaded"]
assert app.med_data["demo"]["name"] == "Ibuprofen"
assert app.med_data["demo"]["count"] == 30
assert app.med_data["demo"]["dose_times"] == ["7:30 PM"]
assert app._demo_registered and app.saved
print("  demo slot:", app.med_data["demo"]["name"],
      app.med_data["demo"]["count"], app.med_data["demo"]["dose_times"])

# add-med when slot occupied
ask("add new medication", "already occupied")

# add-med with missing info -> asks follow-ups
app.med_data["demo"] = {"name": "Demo", "loaded": False, "count": 0}
app._demo_registered = False
ask("add a medication", "called", keep=True)
ask("vitamin c", "label", keep=True)
ask("take with food", "how many pills", keep=True)
ask("sixty", "what time", keep=True)
ask("nine in the morning", "confirm intake", "vitamin c", "60", keep=True)
ask("no", "start over", keep=True)
ask("cancel", "cancelled", keep=False)

# fallback
ask("purple monkey dishwasher",
    ("did not copy", "didn't catch", "insufficient", "unclear"))

# unknown med
ask("how many banana pills do i have left", "do not have")

# ══ LEARNING: corrections teach phrases and vocabulary ══
# 1. unknown phrasing -> fallback -> correction teaches it
ask("check my pill situation", ("did not copy", "didn't catch",
                                "insufficient", "unclear"))
ask("that's wrong", ("what", "say it the way", "correct instruction"), keep=True)
ask("how many pills do i have", "correction stored", "sertraline",
    keep=False)
# the exact phrase now routes directly
ask("check my pill situation", "sertraline", "atorvastatin")
# and a close variant fires through fuzzy matching
ask("check my pills situation", "sertraline")

# 2. misheard med name -> correction creates a vocabulary alias
ask("how many happy pills do i have left", "do not have")
ask("that is wrong", ("what", "say it the way", "correct instruction"), keep=True)
ask("how many sertraline do i have left", "correction stored", "28",
    keep=False)
ask("dispense my happy pills", "sertraline", "never dispense")

# 3. praise + training report
ask("good job", ("reinforcement", "precision", "confidence"))
ask("what have you learned", "learned phrase", "correction")

# 4. learning persists across restarts
v2 = make_voice(app)
r, _ = v2.respond("check my pill situation")
assert "sertraline" in r.lower(), "learning did not persist: " + r
print("OK  persistence: relaunched brain still knows the taught phrase")

# 5. safety gate cannot be shadowed by learning
r, _ = v2.respond("can i take a double dose")
assert "medical advice" in r.lower()
print("OK  safety gate survives learning")

# 6. correcting with still-unknown phrasing changes nothing
ask("that's wrong", ("what", "say it the way", "correct instruction"), keep=True)
ask("fizzbuzz the wombat", ("do not recognize", "no changes"),
    keep=False)

# ══ INTERPRETATION PROTOCOL ══
# capability questions / hypotheticals / quotations are not commands
ask("can you add a new medication", "when you're ready", keep=False)
assert v._flow is None, "capability question must not start a flow"
ask("my friend said dispense my sertraline",
    ("by hand", "nothing happens"), keep=False)
assert app.dispensed in (None, "yellow"), "quotation must not dispense"
ask("what if i asked you to add a medication", "when you're ready")
assert v._flow is None

# negation is never simplified away
ask("don't add a new medication", "taking no action", keep=False)
assert v._flow is None
ask("do not dispense my sertraline", "taking no action", keep=False)

# confirmations expire: a delayed "yes" cannot activate an old proposal
import time as _time
app.med_data["demo"] = {"name": "Demo", "loaded": False, "count": 0}
app._demo_registered = False
ask("add a new medication", "called", keep=True)
v._flow["ts"] = _time.time() - 300
ask("ibuprofen", "expired", keep=False)
assert v._flow is None and not app.med_data["demo"]["loaded"]

# AM/PM is never silently assumed for a medication time
ask("add a new medication", "called", keep=True)
ask("vitamin d", "label", keep=True)
ask("take two tablets at seven thirty, sixty pills",
    ("morning, or the evening",), keep=True)
ask("in the evening", "confirm intake", "seven 30 PM", "60",
    keep=True)
ask("yes", "registered", keep=False)
assert app.med_data["demo"]["dose_times"] == ["7:30 PM"], \
    app.med_data["demo"]["dose_times"]
print("  ampm-clarified time saved:", app.med_data["demo"]["dose_times"])

print()
if FAIL:
    print(f"=== {len(FAIL)} FAILURES ===")
    for t, r in FAIL:
        print("-", t, "->", r)
    sys.exit(1)
print("=== BRAIN BATTERY: ALL PASSED ===")
