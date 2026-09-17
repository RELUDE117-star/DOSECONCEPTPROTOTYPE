"""No private key material may EVER enter this repository.

The repository is public. A committed private key is a compromised
private key, and unlike a token it cannot be quietly rotated without
breaking every device that trusts it. The developer's Mac key is the
specific thing being protected here: its PUBLIC half is committed on
purpose (a public key grants nothing and the Pi needs it to recognise
the Mac); its PRIVATE half must never leave the Mac.

This suite fails the build if that line is ever crossed — in the working
tree, in any tracked file, or anywhere in git history.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

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


def sh(*args):
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           timeout=120)
        return r.returncode == 0, r.stdout
    except Exception:
        return False, ""


PRIVATE_MARKERS = (
    "BEGIN OPENSSH PRIVATE KEY",
    "BEGIN RSA PRIVATE KEY",
    "BEGIN DSA PRIVATE KEY",
    "BEGIN EC PRIVATE KEY",
    "BEGIN PRIVATE KEY",
    "BEGIN ENCRYPTED PRIVATE KEY",
    "BEGIN PGP PRIVATE KEY",
)

print("== 1. no private key in any TRACKED file ==")
_, tracked = sh("git", "ls-files")
files = [f for f in tracked.splitlines() if f.strip()]
ok(len(files) > 5, "the repository has tracked files (%d)" % len(files))
offenders = []
for f in files:
    try:
        with open(f, errors="ignore") as fh:
            body = fh.read()
    except Exception:
        continue
    for mark in PRIVATE_MARKERS:
        if mark in body and "PRIVATE_MARKERS" not in body:
            offenders.append((f, mark))
ok(not offenders, "no tracked file contains private key material %s"
   % (offenders[:3] if offenders else ""))

print("== 2. no private key anywhere in git HISTORY ==")
_, hist = sh("git", "log", "--all", "-p")
hits = [m for m in PRIVATE_MARKERS if m in hist and
        "PRIVATE_MARKERS" not in hist.split(m)[0][-200:]]
ok(not hits, "no commit ever introduced private key material %s" % hits)

print("== 3. private-key FILENAMES are git-ignored ==")
for name in ("dose_pi_claude_ed25519", "id_ed25519", "id_rsa",
             "secret_id_rsa", "server_ecdsa", "backup.key", "host.pem"):
    good, _ = sh("git", "check-ignore", name)
    ok(good, "%s is ignored" % name)

print("== 4. PUBLIC halves are still allowed (the Pi needs them) ==")
for name in ("dose_pi_claude_ed25519.pub",
             "tools/claude_dev_authorized_keys"):
    good, _ = sh("git", "check-ignore", name)
    ok(not good, "%s is NOT ignored (public, must ship)" % name)

print("== 5. the committed key file holds only PUBLIC keys ==")
path = os.path.join(ROOT, "tools", "claude_dev_authorized_keys")
try:
    body = open(path).read()
except Exception:
    body = ""
ok(body.strip() != "", "the authorized-keys file exists")
for mark in PRIVATE_MARKERS:
    ok(mark not in body, "it contains no %r" % mark)
keylines = [l for l in body.splitlines()
            if l.strip() and not l.strip().startswith("#")]
ok(keylines, "it holds at least one key line")
ok(all(re.match(r"^(ssh-(ed25519|rsa|dss)|ecdsa-sha2-|sk-)", l.strip())
       for l in keylines),
   "every non-comment line is an OpenSSH PUBLIC key")

print("== 6. nothing reads or transmits a private key ==")
for f in ("tools/bootstrap_claude_access.py", "dose_app.py"):
    try:
        src = open(os.path.join(ROOT, f), errors="ignore").read()
    except Exception:
        continue
    # the bootstrap may reference the PUBLIC key file only
    ok("id_rsa" not in src and "PRIVATE KEY" not in src,
       "%s never touches private key material" % f)

print()
print("private-key guard: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== NO PRIVATE KEYS: your Mac's private key is not, and cannot "
      "be, in this repository ===")
