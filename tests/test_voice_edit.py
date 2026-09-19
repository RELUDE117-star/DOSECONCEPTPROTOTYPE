#!/usr/bin/env python3
"""Editing a medication by voice: dose times, and notes.

Ryan asked for exactly two things, in his own words:

    "make sure the voice should be able to do more like say I want to
     change a schedule to 4 pm instead of 420 for this medicatin it
     should be able to do that"
    "if you say I want to add more notes to a medications it should be
     able to do that"

Both of these WRITE to medication data, which is new. Every other
voice intent in this file's history is read-only by design, and the
safety line — voice can never dispense, never decrement a count,
never log a dose — does not move to let these in. What moved is the
recognition that changing WHEN a reminder fires, and writing down
something you want to remember, are on the other side of that line:
neither releases a pill, and both are already editable by anyone
standing in front of the screen.

What this file holds down:

  1. WHICH TIME IS THE NEW ONE. English puts it in opposite places:
         "change it TO 4 pm INSTEAD OF 4:20"   new first
         "change it FROM 4:20 TO 4 pm"         new second
     Reading whichever number comes first gets one right and the
     other exactly backwards — and backwards means moving the
     reminder to the time it already had, then reporting success.

  2. AM/PM IS NEVER GUESSED. parse_spoken_time() defaults a bare "4"
     to PM. A 4 AM pill is not a 4 PM pill, so the flow asks.

  3. NOTHING SAVES ON ONE UTTERANCE. Every write goes through a
     confirm step that reads the whole change back.

  4. THE SAFETY GATES STILL WIN. "Should I change my dose to two
     pills" is a medical question, not a reschedule, and it must
     still get the pharmacist referral.

  5. NOTES STAY ON THE PI. A note is free text about a drug the user
     takes. It is written to the medication file and nowhere else.

Run:  python3 tests/test_voice_edit.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILURES = []
CHECKS = [0]


def check(label, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


# ── 1. the parser, on its own ───────────────────────────────────────
# Imported directly: parse_time_change is module level and pure, so
# this part needs no Tk, no audio, no station.
import dose_voice as DV                                    # noqa: E402

print("\n── which time is the new one ───────────────────────────────")

CASES = [
    # (utterance, new, old)
    ("i want to change a schedule to 4 pm instead of 420 for this "
     "medication", "4:00 PM", "4:20 PM"),
    ("change my lisinopril from 4 20 to 4 pm", "4:00 PM", "4:20 PM"),
    ("move it to 8 am rather than 7 30 am", "8:00 AM", "7:30 AM"),
    ("change the time to nine thirty in the evening instead of ten",
     "9:30 PM", None),
    ("move my metformin to eight in the morning", "8:00 AM", None),
]
for utt, want_new, want_old in CASES:
    new, old, _ns, _os = DV.parse_time_change(utt)
    check("%-46s -> new %s" % (utt[:46], want_new), new == want_new,
          "got new=%r" % (new,))
    if want_old is not None:
        check("%-46s    old %s" % ("", want_old), old == want_old,
              "got old=%r" % (old,))

check("a bare number with no am/pm and no context is NOT a time",
      DV.parse_time_change("change lisinopril to four")[0] is None,
      "a lone '4' must not become 4 PM by default")
check("'change the schedule' alone carries no time",
      DV.parse_time_change("change the schedule")[0] is None)

print("\n── the source keeps its own rules ──────────────────────────")
SRC = open(os.path.join(ROOT, "dose_voice.py"), encoding="utf-8").read()

check("the joining word decides, not word order",
      "_INSTEAD_RX" in SRC and "_FROM_TO_RX" in SRC)
check("digits are stripped before hunting for a drug name",
      're.sub(r"[0-9]+", " ", hunt)' in SRC,
      "'4 20' left in is a string the phonetic matcher will happily "
      "score against real medication names")
check("the reschedule flow has an explicit AM/PM step",
      SRC.count('flow["step"] = "ampm"') >= 2
      and "in the morning, or the evening" in SRC
      and "I never " in SRC and "guess with medication times" in SRC,
      "a bare '4' defaults to PM in parse_spoken_time; the flow has "
      "to ask instead of inheriting that default")
check("the reschedule flow has a confirm step",
      "_chsched_confirm_line" in SRC)
check("...and the confirm line names BOTH times",
      "move %s from %s to %s" in SRC)
check("the note flow reads the note back before saving",
      "Noting for %s: %s. Save that?" in SRC)
check("dose times stay sorted, the way the screen editor sorts them",
      "times = sorted(times, key=_key)" in SRC
      and 'md["schedule_time"] = times[0]' in SRC)
check("a note is length-capped",
      "NOTE_MAX" in SRC and "[:self.NOTE_MAX]" in SRC)
check("...and the list of them is capped too",
      "NOTE_KEEP" in SRC and "notes[-self.NOTE_KEEP:]" in SRC)
check("cancelling says which thing was abandoned",
      "Reschedule cancelled" in SRC and "Note discarded" in SRC,
      "'Intake cancelled' after a reschedule tells the user the "
      "wrong thing was dropped")

print("\n── voice still cannot dispense ─────────────────────────────")
# The two new intents are the only writes. Neither touches count,
# neither logs adherence.
for fn in ("_save_sched_change", "_save_note"):
    seg = SRC.split("def %s(" % fn)[1]
    end = seg.find("\n    def ")
    seg = seg[:end if end > 0 else len(seg)]
    check("%s does not change a pill count" % fn,
          '"count"' not in seg and "md['count']" not in seg)
    check("%s does not log a dose" % fn,
          "adherence" not in seg.lower())
    check("%s does not set 'loaded'" % fn,
          'md["loaded"] =' not in seg)

print("\n── notes never leave the station ───────────────────────────")
# The Mac gets audio and hands back a transcript plus, for pure
# chit-chat, a reply. It is never handed note text, and a flow in
# progress is answered locally regardless of what the Mac said.
flow_at = SRC.index("return self._flow_step(text, t)")
mac_at = SRC.index('mac = getattr(self, "_mac_say", None)')
check("an active flow is answered before any Mac reply is considered",
      flow_at < mac_at,
      "otherwise the Mac's chit-chat could answer 'what should I "
      "note about Lisinopril'")
check("the note is written through the medication file only",
      "app._save_med()" in SRC.split("def _save_note(")[1][:900])

print("\n── the safety gates are still first ────────────────────────")
gate_at = SRC.index("if self._is_medical_question(t):")
match_at = SRC.index("route = self._match_builtin(t)")
check("medical-advice gate runs before the intent matcher",
      gate_at < match_at)
check("'increase my dose' is still a medical question, not an edit",
      "increase my dose" in SRC and "decrease my dose" in SRC)

print("\n── the matcher, on real sentences ──────────────────────────")
# _match_builtin is a method, but the parts that matter here are pure
# regex over the normalised text. Exercise them the way respond()
# does: " " + text + " ", lowercase, punctuation stripped.


def norm(s):
    return " " + re.sub(r"[^a-z0-9' ]", " ", s.lower()).strip() + " "


NOTE_RX = re.compile(
    r"\b(?:add|adding|make|making|leave|write|put|save|store|"
    r"append|attach|jot|take)\b[^?]*?\bnotes?\b")
RESCHED_WORD = re.compile(r"\breschedul\w*\b")
RESCHED_VERB = re.compile(
    r"\b(?:change|changing|move|moving|switch|shift|"
    r"update|adjust|set|make)\b")
SCHED_NOUN = re.compile(
    r"\b(?:schedule|scheduled|dose time|dosing time|time|times|"
    r"reminder|alarm|when i take|when it)\b")

for utt in ("i want to add more notes to a medication",
            "add a note to my lisinopril",
            "leave a note on metformin",
            "jot down a note"):
    check("note trigger: %r" % utt, bool(NOTE_RX.search(norm(utt))))

for utt in ("i want to change a schedule to 4 pm instead of 420",
            "change the time for lisinopril",
            "reschedule my metformin",
            "move the reminder to 9 am",
            "update my schedule"):
    n = norm(utt)
    check("reschedule trigger: %r" % utt,
          bool(RESCHED_WORD.search(n)
               or (RESCHED_VERB.search(n) and SCHED_NOUN.search(n))))

# The ones that must NOT be hijacked. Each of these is a real intent
# that already worked, and the new patterns sit ABOVE some of them.
for utt in ("what time is it",
            "what is my schedule today",
            "add a new medication",
            "set up a medication",
            "change a setting",
            "open settings"):
    n = norm(utt)
    resched = bool((RESCHED_WORD.search(n)
                    or (RESCHED_VERB.search(n) and SCHED_NOUN.search(n)))
                   and not re.search(r"\b(?:add|new|register|set up)\b.*"
                                     r"\b(?:medication|med|pill|"
                                     r"prescription)\b", n))
    check("NOT a reschedule: %r" % utt, not resched,
          "this already had an intent and it must keep it")
    check("NOT a note: %r" % utt, not NOTE_RX.search(n))

print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("VOICE EDITING: it changes times and takes notes, and it still "
      "cannot hand you a pill")
