#!/usr/bin/env python3
"""Nothing in this station may emit a raw tone.

2026-09-19. Proving the level meter worked, I played a 440 Hz sine
through the station's speaker with `speaker-test`. Ryan was in the
room:

    "there was a failure where it maide liek a whale/siren noise and
     it was really loud like an alarm, make sure that never happens
     again in testing it scared me"

It was not a failure, it was a diagnostic — which is worse, because it
means the device did exactly what it was told and the instruction was
careless. A medication station earns the right to make noise by only
making noise that means something. An unexplained alarm from a medical
device in someone's home spends trust that took weeks to build.

There was never a need for it either: the engine already publishes
peak and rms to voice/live.txt every second, and tools/soak.sh records
them over hours. The tone proved nothing the heartbeat could not.

This test guards the code. The jobs I write are not in this repo, so
they are guarded by the rule written into CLAUDE.md — which is where
the ban is stated in full.

Run:  python3 tests/test_no_loud_tones.py
"""
import os
import re
import sys

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


# Every way this project could make a noise that is not speech.
BANNED = [
    (re.compile(r"speaker-test"), "speaker-test plays tones and noise"),
    (re.compile(r"\bsox\b.*\bsynth\b"), "sox synth generates tones"),
    (re.compile(r"\bbeep\b\s*(?:-f|\()"), "the beep utility"),
    (re.compile(r"-t\s+(?:sine|pink|wav|white)\b"), "a tone/noise type"),
    (re.compile(r"\bplay\b.*\bsynth\b"), "sox play synth"),
]

# Where the station's own code lives. Staging copies and this test are
# not part of the shipped station.
SKIP_DIRS = {".git", "__pycache__", "docs", "stickers"}
SKIP_FILES = {"test_no_loud_tones.py", "CLAUDE.md"}

scanned = 0
hits = []
for base, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for fn in files:
        if fn in SKIP_FILES:
            continue
        if not fn.endswith((".py", ".sh", ".service", ".desktop")):
            continue
        path = os.path.join(base, fn)
        try:
            src = open(path, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        scanned += 1
        for i, line in enumerate(src.splitlines(), 1):
            stripped = line.strip()
            # A line that only TALKS about the ban is fine.
            if stripped.startswith("#") or stripped.startswith('"'):
                continue
            for rx, why in BANNED:
                if rx.search(line):
                    hits.append((os.path.relpath(path, ROOT), i,
                                 stripped[:70], why))

print("\n── %d source files scanned ─────────────────────────────────"
      % scanned)
check("nothing in the station plays a tone", not hits,
      "; ".join("%s:%d %s (%s)" % h for h in hits[:4]))

print("\n── the sanctioned speaker check speaks ─────────────────────")
DV = open(os.path.join(ROOT, "dose_voice.py"), encoding="utf-8").read()
seg = DV.split("def speaker_test(")[1]
seg = seg[:seg.find("\n    def ")]
check("speaker_test synthesises a sentence",
      "self._synth(" in seg and "Speaker test." in seg,
      "the one sanctioned way to make this station make a noise is "
      "to have it SAY something")
check("...and its fallback is a stock spoken/short wav, not a tone",
      "Front_Center.wav" in seg)
check("no tone generator anywhere in dose_voice",
      "speaker-test" not in DV)

print("\n── the rule is written down where the next session reads it ─")
CM = open(os.path.join(ROOT, "CLAUDE.md"), encoding="utf-8").read()
check("CLAUDE.md carries the ban",
      "NEVER PLAY A TONE OUT OF THIS STATION" in CM)
check("...and quotes Ryan, so nobody argues it was a small thing",
      "whale/siren" in CM)
check("...and names what to use instead",
      "voice/live.txt" in CM and "soak.sh" in CM)

print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("QUIET: this station only makes a noise when it has something "
      "to say")
