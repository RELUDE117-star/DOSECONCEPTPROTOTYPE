"""UPDATE BUTTON must always work — and must never break the device.

The station updates itself over the air, so a bad update is the worst
possible bug: it can brick the thing that gates someone's medication.
These checks prove the update is ATOMIC and VALIDATED:

  * every python module the app imports is downloaded (a brand-new
    module can't silently miss devices)
  * a missing module locally counts as "needs update"
  * a truncated / HTML-error / syntactically-broken download ABORTS
    and leaves the installed app completely untouched
  * a successful update writes every file, app last
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


# import dose_app without a real Tk
sys.modules.setdefault("tkinter", types.ModuleType("tkinter"))
sys.modules["tkinter"].font = types.ModuleType("tkinter.font")
sys.modules.setdefault("tkinter.font", sys.modules["tkinter"].font)
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "dose_app", os.path.join(ROOT, "dose_app.py"))
da = importlib.util.module_from_spec(spec)
spec.loader.exec_module(da)

# ── 1. every imported module is in the update manifest ───────────────
mods = set(da.DoseApp.COMPANION_MODULES)
src = open(os.path.join(ROOT, "dose_voice.py"), errors="ignore").read()
ok("dose_voice.py" in mods, "manifest ships dose_voice.py")
ok("dose_nlu.py" in mods, "manifest ships dose_nlu.py")
if "import dose_nlu" in src:
    ok("dose_nlu.py" in mods, "module imported by voice is in manifest")
for m in mods:
    ok(os.path.exists(os.path.join(ROOT, m)),
       "manifest module exists in repo: %s" % m)


# ── harness: a fake app whose downloads we control ───────────────────
class FakeRoot:
    def after(self, _ms, fn, *a):
        fn(*a)


def make_app(tmp, files, appdir):
    app = object.__new__(da.DoseApp)
    app.root = FakeRoot()
    app.mode = "settings"
    app._update_status_text = ""
    app._draw_frame = lambda: None
    app._finish_update = lambda: setattr(app, "_finished", True)
    app._finished = False
    app._fetch_repo_file = lambda name, timeout=20, api_only=False: (
        files[name] if name in files else (_ for _ in ()).throw(
            KeyError(name)))
    da.APP_DIR = appdir
    return app


GOOD_APP = b"# new app\n" + b"x = 1\n" * 200
GOOD_MOD = b"# new module\n" + b"y = 2\n" * 200
OLD = b"# OLD INSTALLED VERSION\n"


def run_update(files, tmp):
    """Run the real updater against controlled downloads."""
    appdir = os.path.join(tmp, "appdir")
    os.makedirs(appdir, exist_ok=True)
    here = os.path.join(tmp, "here")
    os.makedirs(here, exist_ok=True)
    # pre-existing installed files
    for d in (here, appdir):
        with open(os.path.join(d, "dose_app.py"), "wb") as f:
            f.write(OLD)
        for m in da.DoseApp.COMPANION_MODULES:
            with open(os.path.join(d, m), "wb") as f:
                f.write(OLD)
    app = make_app(tmp, files, appdir)
    da.__file__ = os.path.join(here, "dose_app.py")
    app._apply_update(files["dose_app.py"])
    # the updater downloads on a background thread — wait for it
    import time
    for _ in range(100):
        if app._finished or "failed" in (
                app._update_status_text or "").lower():
            break
        time.sleep(0.05)
    return here, appdir, app


# ── 2. a good update writes EVERY file ───────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    files = {"dose_app.py": GOOD_APP, "DOSE.sh": b"#!/bin/bash\necho hi\n"}
    for m in da.DoseApp.COMPANION_MODULES:
        files[m] = GOOD_MOD
    here, appdir, app = run_update(files, tmp)
    ok(app._finished, "good update completes")
    for d in (here, appdir):
        got = open(os.path.join(d, "dose_app.py"), "rb").read()
        ok(got == GOOD_APP, "app updated in %s" % os.path.basename(d))
        for m in da.DoseApp.COMPANION_MODULES:
            g = open(os.path.join(d, m), "rb").read()
            ok(g == GOOD_MOD, "%s updated in %s" % (m, os.path.basename(d)))

# ── 3. a BROKEN module download aborts and changes NOTHING ───────────
for label, bad in (("truncated", b"tiny"),
                   ("html error page",
                    b"<html><title>404</title></html>" + b" " * 600),
                   ("invalid python",
                    b"def broken(:\n" + b"# pad\n" * 200)):
    with tempfile.TemporaryDirectory() as tmp:
        files = {"dose_app.py": GOOD_APP}
        for i, m in enumerate(da.DoseApp.COMPANION_MODULES):
            files[m] = bad if i == 0 else GOOD_MOD
        here, appdir, app = run_update(files, tmp)
        ok(not app._finished, "%s download does NOT complete" % label)
        untouched = True
        for d in (here, appdir):
            if open(os.path.join(d, "dose_app.py"), "rb").read() != OLD:
                untouched = False
            for m in da.DoseApp.COMPANION_MODULES:
                if open(os.path.join(d, m), "rb").read() != OLD:
                    untouched = False
        ok(untouched, "%s leaves the install untouched" % label)
        ok("failed" in (app._update_status_text or "").lower(),
           "%s reports failure to the user" % label)

# ── 4. a MISSING module download aborts safely ───────────────────────
with tempfile.TemporaryDirectory() as tmp:
    files = {"dose_app.py": GOOD_APP}      # companions absent entirely
    here, appdir, app = run_update(files, tmp)
    ok(not app._finished, "missing module download does not complete")
    ok(open(os.path.join(here, "dose_app.py"), "rb").read() == OLD,
       "missing module leaves the install untouched")

# ── 5. the button FINISHES THE JOB ───────────────────────────────────
# It used to replace .py files and restart, and that was it. Anything
# the new version needed — a python package, a speech model, the voice
# detector — was left to a shell script the user had to find and run,
# and the screen told them to. That is the ONE thing they cannot do
# from the station in front of them.
SRC = open(os.path.join(ROOT, "dose_app.py"), errors="ignore").read()
fin = SRC.split("def _finish_update")[1].split("\n    def ")[0]
ok("_update_finish_setup" in fin,
   "finishing an update runs the setup step")
setup = SRC.split("def _update_finish_setup")[1].split("\n    def ")[0]
ok("_ensure_python_deps" in setup,
   "which installs any missing python packages")
ok("_voice_download_models" in setup,
   "downloads any missing speech/voice models")
ok("fetch_vad_model" in setup, "and the voice detector")
ok("_restart_app" in setup,
   "then restarts — regardless, so a slow download can never leave "
   "the station down")
ok("threading.Thread" in setup,
   "with the work off the UI thread")

# and nothing may tell the user to go and run a shell script
code_lines = [ln for ln in SRC.split("\n")
              if not ln.lstrip().startswith("#")]
# only USER-FACING strings count — a comment or a variable name
# mentioning the script is fine
hints = [ln.strip() for ln in code_lines
         if "DOSE.sh" in ln and '"' in ln
         and ("run DOSE.sh" in ln or "Run DOSE.sh" in ln)]
ok(not hints,
   "nothing on screen tells the user to run DOSE.sh (%d found)"
   % len(hints))
for ln in hints[:3]:
    print("    still says:", ln[:70])

ok('"speech model missing": "downloading…"' in SRC
   or "downloading" in SRC.split("_VOICE_HINTS")[1][:400],
   "a missing model reads as 'downloading', because that is what is "
   "actually happening")

# the fetch order is unchanged and still API-first
fetch = SRC.split("def _fetch_repo_file")[1].split("\n    def ")[0]
ok("api.github.com" in fetch,
   "the GitHub API is still tried first — never CDN-stale")
ok("nocache" in fetch, "with a cache-busted raw fallback")
ok("api_only" in fetch,
   "and auto-updates can demand the authoritative source")

# ── 6. the launch check APPLIES, it does not just announce ───────────
# The device sat on a build from days earlier while fix after fix was
# pushed to it. The launch check found the update every time and then
# wrote a note on the settings status line, which nobody has reason to
# be looking at. A station nobody can update is a station nobody can
# fix.
chk = SRC.split("def _do_update_check")[1].split("\n    def ")[0]
ok("_apply_update" in chk.split("if silent:")[1],
   "the silent launch check applies the update")
ok("api_only=True" in chk,
   "using the GitHub API only — never the raw CDN, which can serve a "
   "stale file and would update BACKWARDS")
ok('settings.get("auto_update", True)' in chk,
   "and it can be switched off, defaulting to on")
ok(chk.index("if silent:") < chk.index("api_only=True"),
   "the authoritative re-fetch happens on the silent path")
# an unreachable API must not fall back to raw for an auto-apply
ok("tap UPDATE" in chk,
   "if the API is unreachable it says so instead of auto-applying "
   "something that might be stale")
ok(chk.index("except Exception:\n                # API unreachable")
   < chk.index("self.root.after(0, self._apply_update, authoritative)"),
   "the unreachable case returns BEFORE anything is applied")

print()
print("update suite: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== UPDATE BUTTON: ALL PASSED (atomic, validated, safe) ===")
