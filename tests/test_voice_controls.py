"""Hold-to-talk and the mic picker must survive a voice-engine restart."""
import sys, os, types, importlib.util
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0,ROOT)
sys.modules.setdefault("tkinter", types.ModuleType("tkinter"))
sys.modules["tkinter"].font = types.ModuleType("tkinter.font")
sys.modules.setdefault("tkinter.font", sys.modules["tkinter"].font)
spec=importlib.util.spec_from_file_location("dose_app", os.path.join(ROOT,"dose_app.py"))
da=importlib.util.module_from_spec(spec); spec.loader.exec_module(da)
P=F=0
def ok(c,l):
    global P,F
    if c: P+=1
    else: F+=1; print("  FAIL",l)

class FakeVoice:
    def __init__(s, avail=True): s.available=avail; s.listened=False; s.card=None; s.stopped=False
    def request_listen(s): s.listened=True; return s.available
    def force_card(s,c,d): s.card=(c,d)
    def stop(s): s.stopped=True
    def start(s): pass

app = object.__new__(da.DoseApp)
app._draw_frame=lambda: None
app.mode="btaudio"; app._meter_peak=0; app._full_test_status=""
app._voice_row_tap=lambda: setattr(app,"_fellback",True)
app._fellback=False

# 1) normal: hold-to-talk listens
app.voice=FakeVoice()
app._voice_push_to_talk()
ok(app.voice.listened, "hold-to-talk starts listening when engine is up")
ok(not app._fellback, "does not bounce to the audio screen")

# 2) engine briefly gone (mid dependency install): must self-start
app.voice=None; app._fellback=False
started={}
def fake_start():
    app.voice=FakeVoice(); started["yes"]=True
app._start_voice=fake_start
app._voice_push_to_talk()
ok(started.get("yes"), "hold-to-talk brings the engine up if missing")
ok(app.voice and app.voice.listened, "…and then actually listens")

# 3) mic picker with engine up
app.voice=FakeVoice()
app._meter_pick(5,1)
ok(app.voice.card==(5,1), "mic picker selects card 5,1")
ok("Switched to card 5,1" in app._full_test_status, "picker confirms on screen")

# 4) mic picker while engine restarting: recovers, never silent
app.voice=None; started.clear()
app._meter_pick(5,1)
ok(started.get("yes"), "mic picker brings the engine up if missing")
ok(app.voice and app.voice.card==(5,1), "…and applies the selection")

# 5) the swap NEVER leaves voice as None
app.voice=FakeVoice()
old=app.voice
import dose_voice  # real module present
def boom(self): raise RuntimeError("engine cannot start")
orig=da.DoseApp._swap_voice_engine
sys.modules["dose_voice"].DoseVoice = lambda a: (_ for _ in ()).throw(RuntimeError("nope"))
res = da.DoseApp._swap_voice_engine(app)
ok(res is False, "failed swap reports failure")
ok(app.voice is old, "failed swap KEEPS the working engine (never None)")

print("== tap the logo once to talk — no holding ==")
APPSRC = open(os.path.join(ROOT, "dose_app.py"), errors="ignore").read()
rel = APPSRC.split("def _on_canvas_release")[1].split("\n    def ")[0]
ok("_voice_push_to_talk" in rel,
   "a QUICK TAP on the logo starts talking, not just a long hold")
ok("_voice_dismiss" in rel,
   "and tapping it again stops")
ok(rel.index("_voice_state") < rel.index('self._nav("home")'),
   "an active conversation is checked before Home navigation")
ok('self.mode == "home"' in rel,
   "from other screens the tap still navigates Home")
press = APPSRC.split("def _on_canvas_press")[1].split("\n    def ")[0]
ok("_home_longpress_fire" in press,
   "holding it from any screen still works")

print()
print("voice-controls suite: %d passed, %d failed" % (P,F))
if not F: print("=== HOLD-TO-TALK + MIC PICKER: ALL PASSED ===")
sys.exit(1 if F else 0)
