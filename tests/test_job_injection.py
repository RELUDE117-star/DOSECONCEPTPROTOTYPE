#!/usr/bin/env python3
"""The one check that protects the Mac, tested in both directions.

The trust direction in this project is one-way by design: the Mac drives
the Pi over SSH, the Pi holds no credential for the Mac, and everything
the Pi says is DATA. The one way that could break is a job script that
takes the Pi's output and runs it. tools/scan_job_injection.py looks for
exactly that.

A detector is only worth having if BOTH halves are true — it catches the
real thing, and it stays quiet on everything else. The second half is
not a nicety. The grep this replaced produced three separate rounds of
false positives:

  1. `OUT=$(ssh host hostname)` — capture and compare, which is what
     every job here does. Four findings on the first run.
  2. The comment block explaining the rule, because it names the
     patterns it is explaining.
  3. The commit message that shipped the fix for 1 and 2, sitting in a
     quoted heredoc — literal data for `git commit -F -`, not code.

An audit that fails on a clean tree teaches its owner to ignore the
summary line, and then the day it finds something real, he ignores that
too. So the false-positive cases below are tests, not politeness.

Run:  python3 tests/test_job_injection.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools"))

try:
    import scan_job_injection as scan
except ImportError:
    print("FAIL: tools/scan_job_injection.py is missing — the injection "
          "check cannot run")
    sys.exit(1)

FAILURES = []
CHECKS = [0]


def check(label, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


def scan_text(body):
    """Write body to a .sh in a temp dir and return its findings."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "job.sh")
        with open(p, "w") as f:
            f.write(body)
        return scan.scan_file(p)


# ── must be caught ───────────────────────────────────────────────────
DANGEROUS = {
    "eval of captured Pi output":
        'OUT=$(ssh dose-pi "cat /tmp/x")\neval "$OUT"\n',
    "process substitution into bash":
        'bash <(ssh dose-pi cat /tmp/payload.sh)\n',
    "process substitution into sh":
        'sh <(ssh dose-pi cat /tmp/p)\n',
    "source of Pi output":
        'source <(ssh dose-pi env)\n',
    "dot-source of Pi output":
        '. <(ssh dose-pi env)\n',
    "piped into bash":
        "ssh dose-pi 'cat /tmp/x' | bash\n",
    "piped into sh":
        "ssh dose-pi 'cat /tmp/x' | sh\n",
    "ssh in command position":
        '$(ssh dose-pi echo whoami)\n',
    "backtick ssh in command position":
        '`ssh dose-pi echo whoami`\n',
    "eval after a semicolon":
        'X=1; eval "$UNTRUSTED"\n',
}

print("\n1. Real injection must be caught")
for label, body in DANGEROUS.items():
    check(label, bool(scan_text(body)), "NOT flagged")


# ── must NOT be caught ───────────────────────────────────────────────
SAFE = {
    "capture and compare (what every job here does)":
        'OUT=$(ssh dose-pi hostname)\n[ "$OUT" = "raspberrypi" ] && echo ok\n',
    "capture into a variable and print it":
        'A=$(ssh dose-pi md5sum /tmp/f)\necho "$A"\n',
    "a comment explaining the rule":
        '# dangerous forms: eval, bash <(...), | bash, $(ssh ...)\n'
        '# none of them run here\necho hello\n',
    "a commit message in a quoted heredoc":
        "git commit -q -F - <<'MSG'\n"
        "The detector looks for eval, bash <(...), sh <(...),\n"
        "source <(...), | bash, and $(ssh ...) in command position.\n"
        "MSG\necho done\n",
    "sending OUR script to the Pi to run THERE":
        "ssh -o BatchMode=yes dose-pi 'bash -s' <<'PI'\n"
        "echo 'this runs on the Pi, from our own script'\n"
        "PI\n",
    "scp and a plain remote command":
        'scp "$R/dose_voice.py" dose-pi:/tmp/\n'
        'ssh dose-pi "md5sum /tmp/dose_voice.py"\n',
    "a trailing comment mentioning eval":
        'echo hi   # not an eval, just a word\n',
    "the word eval inside a string":
        'echo "the evaluation finished"\n',
}

print("\n2. Safe, ordinary jobs must stay quiet")
for label, body in SAFE.items():
    hits = scan_text(body)
    check(label, not hits,
          "wrongly flagged: %s" % (hits[:1],))


print("\n3. The audit actually calls the scanner")
audit = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools", "security_audit.sh")
check("security_audit.sh exists", os.path.exists(audit))
if os.path.exists(audit):
    src = open(audit, encoding="utf-8").read()
    check("it invokes scan_job_injection.py",
          "scan_job_injection.py" in src)
    check("it no longer uses the old grep that cried wolf",
          "grep -rlE 'eval[[:space:]]" not in src)
    check("a missing scanner is reported, not silently passed",
          "injection check SKIPPED" in src)


print("\n4. Findings are actionable")
hits = scan_text('OUT=$(ssh dose-pi cat /tmp/x)\neval "$OUT"\n')
check("a finding carries a line number", hits and isinstance(hits[0][0], int))
check("a finding says WHICH construct matched",
      hits and "eval" in hits[0][1])
check("a finding quotes the offending line",
      hits and "eval" in hits[0][2])
check("the line number points at the eval, not the capture",
      hits and hits[0][0] == 2, "got line %s" % (hits[0][0] if hits else "-"))


print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("=== JOB INJECTION: the Pi cannot make this Mac run anything ===")
