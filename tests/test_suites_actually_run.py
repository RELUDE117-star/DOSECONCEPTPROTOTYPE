#!/usr/bin/env python3
"""A test that dies on import is not a failing test. It is no test.

2026-09-19. `tests/test_security.py` — the adversarial suite: spoken
prompt-injection, the no-dispense invariant under attack, QR payload
fuzzing, learning-store poisoning — had this in the middle of it:

    spec = importlib.util.spec_from_file_location(
        'dose_app', '/home/user/DOSECONCEPTPROTOTYPE/dose_app.py')

An absolute path on a machine nobody has. It raised
FileNotFoundError and took the whole file down, so EVERYTHING BELOW
that line had not run in who knows how long — including the section
that proves a crafted sticker cannot inject a command or reach the
dispenser.

Nothing was actually broken: with the path fixed, all 124 checks
pass. That is the part worth sitting with. For weeks the security
suite reported a red line in a list of suites, right next to the
genuinely-red ones that need a display, and it read as the same kind
of thing. A suite that cannot run looks exactly like a suite that
fails, and neither one is telling you anything about the code.

So this file checks the checkers. Two properties, both cheap:

  1. No test refers to an absolute path outside this repository.
     That is the specific fault above, and it is invisible until
     someone runs the file on the one machine it was written on.

  2. Every test file at least IMPORTS. Not passes — imports. A suite
     that needs a display or a microphone is allowed to fail; it is
     not allowed to fall over before it reaches its first assertion,
     because then its result means nothing at all.

Run:  python3 tests/test_suites_actually_run.py
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")

FAILURES = []
CHECKS = [0]


def check(label, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


FILES = sorted(f for f in os.listdir(TESTS)
               if f.startswith("test_") and f.endswith(".py"))

print("\n── no test points at a path outside this repo ──────────────")
# Paths that legitimately appear: the device's own locations, which
# tests reference as STRINGS to assert about, never to open. The
# fault is an absolute path handed to something that OPENS it.
OPENERS = re.compile(
    r"(?:open|spec_from_file_location|read_text|Path)\s*\(\s*[^)]*?"
    r"['\"](/(?:home|Users|tmp|var|opt)/[^'\"]*)['\"]", re.S)
ALLOWED_PREFIXES = ("/home/rjarv1/", "/Users/", "/tmp/", "/var/", "/opt/")
offenders = []
for fn in FILES:
    path = os.path.join(TESTS, fn)
    src = open(path, encoding="utf-8", errors="replace").read()
    for m in OPENERS.finditer(src):
        p = m.group(1)
        # A path under the device's own app dir is how the on-device
        # harnesses find the station, and they are given a
        # DOSE_APP_DIR override. Those are fine. A path under some
        # developer's home directory is not.
        if p.startswith("/home/rjarv1/") or "DOSE_APP_DIR" in src:
            continue
        line = src[:m.start()].count("\n") + 1
        offenders.append("%s:%d %s" % (fn, line, p))
check("nothing opens an absolute path from another machine",
      not offenders, "; ".join(offenders[:3]))

print("\n── every suite gets as far as its first assertion ──────────")
# Import only. A suite that needs a display, a microphone or the Pi
# is entitled to FAIL; it is not entitled to die before it starts,
# because a suite that cannot run is indistinguishable in a results
# table from one that ran and found something.
SKIP = {"test_suites_actually_run.py"}
broken = []
skipped = []
for fn in FILES:
    if fn in SKIP:
        continue
    path = os.path.join(TESTS, fn)
    r = subprocess.run(
        [sys.executable, "-c",
         "import py_compile,sys;"
         "py_compile.compile(sys.argv[1], doraise=True)", path],
        capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        broken.append("%s: will not compile" % fn)
        continue
    # Does it blow up on a MISSING FILE or a bad import specifically?
    # Run it and look at the exception type, not the exit code — a
    # suite is allowed to exit 1 because it found a real problem.
    r = subprocess.run([sys.executable, path], capture_output=True,
                       text=True, timeout=300,
                       cwd=ROOT, env=dict(os.environ, DOSE_TEST_QUIET="1"))
    err = (r.stderr or "")
    # A SUITE THAT NEEDS HARDWARE IS NOT A SUITE THAT IS DEAD.
    #
    # The UI suites need a display; CLAUDE.md says to run them under
    # xvfb. Reporting those as broken here would recreate the exact
    # problem this file exists to catch — a red line that means
    # something different from the red line next to it, so both stop
    # being read. They are listed separately, by name, as skips.
    ENVIRONMENTAL = ("tkinter", "PIL", "cv2", "pyaudio", "sounddevice",
                     "picamera", "numpy", "faster_whisper", "vosk",
                     "onnxruntime", "rapidfuzz", "jellyfish")
    m = re.search(r"ModuleNotFoundError: No module named '([^']+)'", err)
    if m and m.group(1).split(".")[0] in ENVIRONMENTAL:
        skipped.append("%s (needs %s)" % (fn, m.group(1)))
        continue
    for fatal in ("FileNotFoundError", "SyntaxError",
                  "ModuleNotFoundError", "ImportError"):
        if fatal in err:
            last = [l for l in err.strip().splitlines() if fatal in l]
            broken.append("%s: %s" % (fn, (last or [fatal])[-1][:90]))
            break
check("no suite dies before it can assert anything",
      not broken, " | ".join(broken[:3]))
if broken:
    print()
    for b in broken:
        print("       BROKEN  " + b)
if skipped:
    print("\n  these need hardware this machine has not got, which is")
    print("  a different thing from being broken, and is why they are")
    print("  named here rather than counted as failures:")
    for sk in skipped:
        print("       skip    " + sk)

print("\n── and the security suite in particular reaches its end ────")
r = subprocess.run([sys.executable, os.path.join(TESTS, "test_security.py")],
                   capture_output=True, text=True, timeout=300, cwd=ROOT)
out = (r.stdout or "") + (r.stderr or "")
check("test_security.py prints its own summary line",
      "security suite:" in out,
      "it used to stop at a hardcoded path, a third of the way in, "
      "with the QR fuzzing never reached")
m = re.search(r"security suite: (\d+) passed", out)
check("...and runs the whole adversarial set, not a third of it",
      bool(m) and int(m.group(1)) >= 100,
      "only %s checks ran" % (m.group(1) if m else "?"))

print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("THE CHECKERS CHECK OUT")
