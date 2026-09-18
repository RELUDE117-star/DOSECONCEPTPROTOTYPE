#!/usr/bin/env python3
"""Two places pull code onto this station. Both must obey the brake.

WHY THIS TEST EXISTS
--------------------
dose_app.py has honoured DOSE_FREEZE since the auto-update brakes went
in — "whatever is installed stays installed", the switch to set before
a demo. DOSE.sh, five hundred lines away in another language, pulls the
same four files from raw.githubusercontent on EVERY launch and had
never heard of it.

Observed on the device, twice in a row:

    install dose_voice.py as e3e52dc, verify byte for byte  -> match
    start the service, wait for the heartbeat               -> running
    read the same path thirty seconds later                 -> c06b6681

c06b6681 is the branch build, without the fix. The in-app updater was
innocent — it had already declined, exactly as DOSE_FREEZE told it to —
and I spent an hour reading it anyway.

This is the same lesson as the duplicate autostart entry, in a
different file: A FIX THE PROGRAM UNDOES AT STARTUP IS NOT A FIX.

Run:  python3 tests/test_freeze.py
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILURES = []
CHECKS = [0]


def check(label, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


SH = open(os.path.join(ROOT, "DOSE.sh"), encoding="utf-8").read()
APP = open(os.path.join(ROOT, "dose_app.py"), encoding="utf-8").read()

print("\n── the launcher is valid shell ──────────────────────────────")

r = subprocess.run(["bash", "-n", os.path.join(ROOT, "DOSE.sh")],
                   capture_output=True, text=True)
check("DOSE.sh parses", r.returncode == 0, r.stderr.strip()[:200])

print("\n── both pullers honour the same switch ──────────────────────")

check("the app's updater checks DOSE_FREEZE",
      'os.environ.get("DOSE_FREEZE"' in APP)
check("the launcher checks DOSE_FREEZE too",
      "DOSE_FREEZE" in SH,
      "this is the half that was missing")

print("\n── the guard actually BRACKETS the download ─────────────────")
# Checking that the word appears somewhere is not a test. The four
# curl calls that overwrite the application must be INSIDE the guard.

i_guard = SH.find("DOSE_FREEZE")
i_open = SH.find('if [ "${UPDATE_OK:-1}" = "0" ]', i_guard)
i_close = SH.find("# end of the DOSE_FREEZE guard", i_open)
check("the guard opens after the switch is read",
      i_guard != -1 and i_open > i_guard, (i_guard, i_open))
check("the guard closes", i_close > i_open, (i_open, i_close))

block = SH[i_open:i_close] if (i_open != -1 and i_close > i_open) else ""
for f in ("dose_app.py", "dose_voice.py", "dose_nlu.py", "DOSE.sh"):
    check("the download of %s is inside the guard" % f,
          ('for F in dose_app.py dose_voice.py dose_nlu.py DOSE.sh' in block)
          and f in block)
check("the fetch loop's curl is inside the guard",
      'curl -fsSL "$RAW_URL/$F' in block)
check("the copy over APP_DIR is inside the guard",
      'cp "$STAGE/$F" "$APP_DIR/$F"' in block)
check("the logo/QR refetch is inside the guard too",
      'dose_logo.png?nocache' in block)

# Nothing outside the guard may overwrite the application files.
outside = SH[:i_open] + SH[i_close:]
bad = [ln.strip() for ln in outside.splitlines()
       if 'curl' in ln and 'RAW_URL' in ln
       and 'dose_app.py' not in ln and '$F' not in ln
       and 'logo' not in ln and 'demo_qr' not in ln]
check("no other unguarded fetch of application code",
      all('$MOD' in b for b in bad), bad)
check("the one exception is the self-heal for a MISSING module, "
      "which cannot overwrite anything",
      'if [ ! -f "$APP_DIR/$MOD" ]; then' in SH)

print("\n── the switch parses the way the app's does ─────────────────")

CASE = re.search(r'case "\$\(printf .*?DOSE_FREEZE.*?\n(.*?)esac', SH,
                 re.S)
check("the launcher lower-cases before matching",
      CASE is not None and "tr 'A-Z' 'a-z'" in SH)

script = ('case "$(printf \'%s\' "${DOSE_FREEZE:-}" | tr \'A-Z\' \'a-z\')" '
          'in 1|true|yes|on) echo FROZEN;; *) echo LIVE;; esac')
for val, want in (("1", "FROZEN"), ("true", "FROZEN"), ("YES", "FROZEN"),
                  ("On", "FROZEN"), ("0", "LIVE"), ("", "LIVE"),
                  ("no", "LIVE"), ("false", "LIVE")):
    env = dict(os.environ)
    env["DOSE_FREEZE"] = val
    got = subprocess.run(["bash", "-c", script], capture_output=True,
                         text=True, env=env).stdout.strip()
    check("DOSE_FREEZE=%-5r -> %s" % (val, want), got == want, got)

# The app's own accepted values, so the two cannot drift apart.
m = re.search(r'os\.environ\.get\("DOSE_FREEZE", ""\)\.lower\(\) in \(\s*'
              r'([^)]*)\)', APP)
check("the app accepts the same words",
      m is not None and set(re.findall(r'"([a-z0-9]+)"', m.group(1)))
      == {"1", "true", "yes", "on"},
      m.group(1) if m else "not found")

print("\n── an unset switch still updates ───────────────────────────")
env = dict(os.environ)
env.pop("DOSE_FREEZE", None)
got = subprocess.run(["bash", "-c", script], capture_output=True,
                     text=True, env=env).stdout.strip()
check("with no switch at all the station still self-updates",
      got == "LIVE", got)

print("\n── the turn test hook is OFF unless asked for ──────────────")
# Turn timing — endpointing, the escalation decision, the language
# layer, time to first sound — is only measurable on a REAL turn, and a
# real turn starts when somebody holds the logo. Nothing may tap this
# device's screen, so there is a file hook. A hook that ships enabled
# would be a way to make a medicine cabinet start listening, so it is
# default-off and lives inside a directory only the kiosk user can
# write.
sys.path.insert(0, ROOT)
import dose_voice                                            # noqa: E402

VOICE = open(os.path.join(ROOT, "dose_voice.py"), encoding="utf-8").read()
VCODE = "\n".join(ln for ln in VOICE.splitlines()
                   if not ln.lstrip().startswith("#"))

check("the hook exists", "ptt_request" in VCODE)
check("it is gated on an explicit variable",
      "TEST_HOOKS = os.environ.get(\"DOSE_TEST_HOOKS\"" in VCODE)
check("and it is OFF with nothing set",
      dose_voice.TEST_HOOKS is False)
check("the loop checks the gate BEFORE looking for the file",
      VCODE.index("if TEST_HOOKS and self.state ==")
      < VCODE.index('hook = os.path.join(VOICE_DIR, "ptt_request")'))
check("the flag is consumed, so one touch is one turn",
      "os.remove(hook)" in VCODE)
check("it opens no port and adds no listener",
      "socket(" not in VCODE.split("ptt_request")[1][:1200])
check("it joins the SAME path a held logo uses, with no special case",
      "self._ptt_requested = True" in VCODE)
for val, want in (("1", True), ("true", True), ("on", True),
                  ("yes", True), ("0", False), ("", False),
                  ("no", False)):
    got = val.strip().lower() in ("1", "true", "yes", "on")
    check("DOSE_TEST_HOOKS=%-5r -> %s" % (val, want), got == want)

print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("freeze OK")
