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
# Scan every commit EXCEPT this guard's own source. This file necessarily
# contains the exact strings it hunts for, so it matched itself: the old
# self-exclusion only looked 200 characters back from the FIRST hit, which
# reached the early markers in the tuple but never "BEGIN PGP PRIVATE KEY"
# (the 7th) — so this check failed on every run from the commit that
# introduced it. A guard that is permanently red is a guard people learn
# to ignore, which is worse than not having one.
#
# Excluding the file by pathspec removes the self-reference entirely, and
# lets the match below be exact rather than heuristic.
#
# Two further refinements, both learned the hard way:
#
#   --format=""  suppresses commit MESSAGES. Without it, a commit whose
#   message merely *discusses* a marker — such as the one explaining this
#   very fix — trips the check. What matters is whether key material
#   entered the tree, not whether anyone wrote the words down.
#
#   Only '+' lines count, because only ADDED content enters the
#   repository. A commit that REMOVES a leaked key should not keep
#   failing the build forever after the cleanup.
SELF = "tests/test_no_private_keys.py"
_, hist = sh("git", "log", "--all", "-p", "--format=",
             "--", ".", ":(exclude)" + SELF)
added = "\n".join(l for l in hist.splitlines()
                  if l.startswith("+") and not l.startswith("+++"))
hits = [m for m in PRIVATE_MARKERS if m in added]
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
    # A REDACTION DENYLIST IS NOT A LEAK. dose_app.py names
    # "BEGIN ... PRIVATE KEY" inside _SECRET_SHAPES precisely so the
    # audit redactor can STRIP one that reached a crash log by some
    # route nobody planned. Flagging that is backwards: it would push
    # someone to delete the scrubber to make the test green, which is
    # the exact opposite of what this suite exists to enforce.
    #
    # The same exemption check 1 already makes for PRIVATE_MARKERS.
    scrubber = ("_SECRET_SHAPES" in src or "PRIVATE_MARKERS" in src)
    ok("id_rsa" not in src and ("PRIVATE KEY" not in src or scrubber),
       "%s never touches private key material" % f)
    if scrubber:
        # If a file claims to be a scrubber, it had better actually
        # redact — otherwise the exemption above becomes a loophole.
        ok("redact" in src.lower(),
           "%s names key material only to redact it" % f)

# ── EVERY FILE THAT HOLDS A CREDENTIAL IS IGNORED BY NAME ───────────
#
# The Mac speech server's pairing file is `dose_server.conf`: a LAN
# address on line one and a BEARER TOKEN on line two. It matched none
# of the .gitignore credential patterns — not *_key, not *_token —
# because it is named for what it IS rather than for what it HOLDS.
#
# It was never committed, but that was luck. The deploy job that writes
# it onto the Pi printed "git-ignored: CHECK THIS" and that was the
# only reason anyone looked. This repository is public.
print()
print("── every credential file is ignored, by name ──")
IGN = os.path.join(ROOT, ".gitignore")
_ign = open(IGN, encoding="utf-8").read() if os.path.exists(IGN) else ""


def ignored(name):
    """Does .gitignore cover this filename? Asked of git, not of a
    regex of mine — git is the thing that decides."""
    try:
        r = subprocess.run(["git", "check-ignore", "-q", name],
                           cwd=ROOT, capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        # No git here: fall back to asking whether the literal name
        # appears, which is weaker but never falsely reassuring.
        return name in _ign


for secret_file in ("dose_server.conf", "github_token", "groq_key",
                    "hf_token", "id_ed25519", "id_rsa", ".env"):
    ok(ignored(secret_file),
       "%s is git-ignored" % secret_file)

# AND THE MEDICATION DATA, which is the most sensitive thing here and
# was not ignored at all. What a named person takes, when, and whether
# they took it is health data about one identifiable human being, and
# this repository is public. The credential rules above exist because a
# leaked token costs money; this costs somebody their privacy, and it
# was found by widening a test rather than by anyone thinking of it.
print("── and the medication data, which matters more than the tokens ──")
for private_file in ("med_data.json", "med_data.json.bak",
                     "adherence_log.json", "learning.json",
                     "calibration.json", "voice/turns.jsonl",
                     "voice/raw_from_engine.wav"):
    ok(ignored(private_file), "%s is git-ignored" % private_file)

# And none of them is actually IN the repository right now.
try:
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT,
                             capture_output=True, text=True,
                             timeout=15).stdout.split("\n")
except Exception:
    tracked = []
for t in tracked:
    base = os.path.basename(t.strip())
    ok(base not in ("dose_server.conf", "github_token", "groq_key",
                    "hf_token", "id_rsa", "id_ed25519"),
       "no credential file is tracked (%s)" % (base or "-"))
    if base in ("dose_server.conf", "github_token"):
        break

print()
print("private-key guard: %d passed, %d failed" % (PASSED, FAILED))
if FAILED:
    for f in FAILS:
        print(" -", f)
    sys.exit(1)
print("=== NO PRIVATE KEYS: your Mac's private key is not, and cannot "
      "be, in this repository ===")
