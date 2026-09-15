"""THE AMY MIGRATION — the bug that made the last update do nothing.

The previous release removed Amy from the code, and on a fresh install
that worked. On a station that ALREADY had Amy it did nothing at all,
because the download and the cleanup both lived inside the "voice
model missing" branch — and a station with Amy on disk isn't missing a
voice. It probed as perfectly healthy and kept the slow voice forever.

That is the failure this suite exists to prevent. Every check below
runs against a directory seeded exactly like the affected device.

The rule now: ONE voice, checked BY NAME, on EVERY launch. Any other
.onnx is not a fallback — it is something to replace.
"""
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

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


sys.modules.setdefault("tkinter", types.ModuleType("tkinter"))
sys.modules["tkinter"].font = types.ModuleType("tkinter.font")
sys.modules.setdefault("tkinter.font", sys.modules["tkinter"].font)
import importlib.util                                      # noqa: E402

spec = importlib.util.spec_from_file_location(
    "dose_app_mig", os.path.join(ROOT, "dose_app.py"))
da = importlib.util.module_from_spec(spec)
spec.loader.exec_module(da)
import dose_voice as dv                                    # noqa: E402

VOICE = da.DoseApp.VOICE_NAME
ONNX_MAGIC = b"\x08" + b"\x00" * 20


def seed(*names, cache=0):
    """A voice directory exactly like a real device's."""
    d = tempfile.mkdtemp(prefix="dose_mig_")
    for n in names:
        with open(os.path.join(d, n + ".onnx"), "wb") as f:
            f.write(ONNX_MAGIC + b"x" * 1000)
        with open(os.path.join(d, n + ".onnx.json"), "w") as f:
            f.write("{}")
    os.makedirs(os.path.join(d, "cache"), exist_ok=True)
    for i in range(cache):
        open(os.path.join(d, "cache", "say_%d.wav" % i), "wb").close()
    return d


def names(d):
    return sorted(f for f in os.listdir(d) if f.endswith(".onnx"))


def app():
    a = object.__new__(da.DoseApp)
    a._swap_voice_engine = lambda: None
    return a


print("== 1. the exact device that got stuck ==")
# Amy installed, working, nothing "missing" — the old code's blind spot
d = seed("en_US-amy-medium", VOICE, cache=4)
gone = app()._retire_other_voices(d)
ok(names(d) == [VOICE + ".onnx"],
   "Amy is removed once her voice is present (left: %s)" % names(d))
ok(any("amy" in g for g in gone), "and it is reported as removed")
ok(not os.listdir(os.path.join(d, "cache")),
   "clips pre-rendered in the old voice are dropped too")
ok(os.path.exists(os.path.join(d, VOICE + ".onnx.json")),
   "her config file is kept")

print("== 2. it NEVER leaves the station mute ==")
d = seed("en_US-amy-medium", cache=2)
app()._retire_other_voices(d)
ok(names(d) == [], "retire alone would remove Amy...")
# ...which is why migrate_voice downloads FIRST and only then retires
src = open(os.path.join(ROOT, "dose_app.py"), errors="ignore").read()
mig = src.split("def migrate_voice")[1].split("def _voice_download_models")[0]
ok(mig.index("_voice_download_models") > 0
   and "if os.path.exists(mine):" in mig,
   "migrate_voice retires ONLY when her voice is already on disk")
dl = src.split("HER VOICE — exactly one file")[1][:2000]
ok(dl.index("_retire_other_voices") > dl.index("if os.path.exists(onx)"),
   "and the downloader retires only after a successful download")

print("== 3. the migration is not gated on anything being missing ==")
boot = src.split("Voice assistant (\"Hey Dose\")")[1][:900]
ok("self.migrate_voice" in boot,
   "it runs at every launch, not only when a model is missing")
ok("voice_enabled" in boot, "under the normal voice-enabled setting")
sh = open(os.path.join(ROOT, "DOSE.sh"), errors="ignore").read()
ok("One voice, always hers" in sh,
   "the setup script has the same unconditional step")
# and it must sit OUTSIDE the "models missing" block
gate = sh.index("# Voice models: keyed on the actual files")
ok(sh.index("One voice, always hers") > gate,
   "placed after the models-missing block, not inside it")
ok(sh.index("One voice, always hers") > sh.index("Voice installed:"),
   "and after the download, so it never deletes the only voice")

print("== 4. there is exactly ONE voice ==")
ok(dv.VOICE_NAME == VOICE, "engine and app agree on the name (%s)" % VOICE)
vsrc = open(os.path.join(ROOT, "dose_voice.py"), errors="ignore").read()
ok("VOICE_PREFERENCE" not in vsrc, "no list of alternative voices")
ok("_synth_moonshine" not in vsrc, "no second synthesis engine")
ok("kokoro" not in vsrc.lower().replace("kokoro_af_heart", "")
   or "Kokoro and the" in vsrc,
   "Kokoro is not used as a voice")
ok("VOICE_CANDIDATES" not in src, "the app fetches one voice by name")
ok(src.count('VOICE_SUB = "en/en_US/hfc_female/medium"') == 1,
   "from one path")

print("== 5. a stranger voice is replaced, not adopted ==")
# some other Piper voice on disk must NOT be used as if it were hers
probe = vsrc.split("HER voice, or none")[1][:700]
ok('self.reason = "voice model missing"' in probe,
   "an unrecognised .onnx counts as MISSING, so hers gets fetched")
ok("VOICE_NAME in os.path.basename" in probe,
   "the voice is matched by name, never by 'whatever is first'")

d = seed("en_US-lessac-medium", VOICE)
app()._retire_other_voices(d)
ok(names(d) == [VOICE + ".onnx"],
   "any other voice is retired, not just Amy (left: %s)" % names(d))

print("== 6. it is safe to run over and over ==")
d = seed(VOICE, cache=3)
for _ in range(5):
    gone = app()._retire_other_voices(d)
ok(names(d) == [VOICE + ".onnx"], "her voice survives repeated runs")
ok(gone == [], "nothing is reported removed when there is nothing to do")
ok(len(os.listdir(os.path.join(d, "cache"))) == 3,
   "and the pre-rendered clips are NOT wiped every launch")

print("== 7. one recogniser ==")
ok("faster_whisper" not in vsrc.replace("faster-whisper", ""),
   "faster-whisper is gone from the engine")
ok("faster-whisper" not in sh, "and from the setup script")
ok("faster_whisper" not in src.replace("faster-whisper", ""),
   "and from the app's dependency list")
ok("moonshine" in vsrc.lower(), "Moonshine does the hearing")

print()
print("migration suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== MIGRATION: ALL PASSED (one voice, and it actually lands) ===")
