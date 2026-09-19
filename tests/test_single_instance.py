#!/usr/bin/env python3
"""Clicking the icon may never start a second station.

Ryan, describing the symptom before anybody had named it:

    "the app that you open on the pi versus the app that I open on the
     pi through dose.sh seem to be different and sometimes both of
     them are on at the same time ... this could be a cause of one of
     the issues we were having that two sessions are going on at the
     same time which is why their could be glitches"

He is right, and this project has already paid for it once. CLAUDE.md
records two complete instances on a 3.8 GB board, two copies of every
speech model, and pages of `AlsaOpen failed` that were not a driver
fault at all — the other instance was holding the microphone.

That was fixed for the AUTOSTART path and never for the obvious one:
a person clicking the shortcut while systemd already has the station
up. **The shortcut this project writes pointed straight at DOSE.sh**,
so the icon on his desktop was a button that made the bug.

It also had no `Icon=` line, so there was nothing recognisable to
click, which is why he was running DOSE.sh by hand. The missing logo
and the duplicate instance are the same bug wearing two faces, and
this file covers both.

Run:  python3 tests/test_single_instance.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SH = open(os.path.join(ROOT, "DOSE.sh"), encoding="utf-8").read()
LAUNCH = open(os.path.join(ROOT, "tools", "dose_launch.sh"),
              encoding="utf-8").read()

FAILURES = []
CHECKS = [0]


def check(label, cond, detail=""):
    CHECKS[0] += 1
    if cond:
        print("  ok   %s" % label)
    else:
        print("  FAIL %s %s" % (label, detail))
        FAILURES.append(label)


print("\n── the desktop entry does not launch the app ───────────────")
# SKIP PAST THE OPENING HEREDOC MARKER. `entry.index("EOF\n")` found
# the `<< EOF` that OPENS the heredoc, not the one that closes it, so
# `entry` came out as four characters and every check below failed on
# a file that was correct. Take everything after the first newline,
# then cut at the closing marker.
# The body moved into a function, because writing this file on every
# single start is what kept clearing its "trusted" flag and bringing
# back the "Execute in Terminal" dialog Ryan kept hitting.
entry = SH.split("_dose_desktop_body() {")[1]
entry = entry[entry.index("\n") + 1:]
entry = entry[:entry.index("\nEOF")]
exec_line = [ln for ln in entry.splitlines() if ln.startswith("Exec=")]
check("there is exactly one Exec line", len(exec_line) == 1, exec_line)
check("...and it runs the LAUNCHER",
      "dose_launch.sh" in (exec_line[0] if exec_line else ""),
      exec_line)
check("...not DOSE.sh itself",
      "DOSE.sh" not in (exec_line[0] if exec_line else ""),
      "this is the line that made a second instance every time he "
      "opened his own station from his own desktop")

print("\n── the trust flag survives a restart ───────────────────────")
# 2026-09-19, measured on the device: trusted at 09:20, untrusted
# again by 09:47. Rewriting a .desktop file discards its gio
# metadata, and this script rewrote it on every start — so the
# station un-trusted its own icon every time it came up, and Ryan got
# the "Execute in Terminal" dialog again.
check("the entry is only written when it would CHANGE",
      'cmp -s "$_DESK.new" "$_DESK"' in SH,
      "an unconditional rewrite clears metadata::trusted every start")
check("...and it is re-trusted every start anyway",
      "_dose_trust " in SH and "metadata::trusted true" in SH)
check("the re-trust uses the session bus, not a throwaway one",
      "/run/user/$(id -u)/bus" in SH,
      "`dbus-launch gio set` spawns a NEW private bus, writes the "
      "flag into it, and discards it — it could never have worked")
# STRIP THE COMMENTS FIRST. The eleventh self-matching pattern in
# this project: the line explaining why dbus-launch was removed
# contains the word "dbus-launch", so the check failed on code that
# no longer calls it. The file has a code_only() helper further down
# for precisely this; this check predates it in the file, so it does
# the same thing inline.
SH_CODE = "\n".join(ln for ln in SH.splitlines()
                    if not ln.lstrip().startswith("#"))
check("dbus-launch is gone",
      "dbus-launch" not in SH_CODE,
      "it looked like a fallback and was a no-op")

print("\n── and it has a logo ───────────────────────────────────────")
icon = [ln for ln in entry.splitlines() if ln.startswith("Icon=")]
check("there is an Icon line", len(icon) == 1, icon)
# THE ICON BLOCK ONLY. Checking the ordering of two filenames across
# the WHOLE of DOSE.sh matched an unrelated install-copy line 15,000
# characters earlier and failed on correct code. "Assert the property
# from the line it is about" is already in CLAUDE.md, twice.
ICONBLK = SH.split("THE ICON HE POINTED AT")[1]
ICONBLK = ICONBLK[:ICONBLK.index("DOSE_ICON=\"$HOME")]
check("...through a variable resolved just above",
      (icon[0] if icon else "") == "Icon=$DOSE_ICON")
check("...which is set to an installed path, not the checkout",
      'DOSE_ICON="$HOME/.local/share/icons/dose.png"' in SH,
      "an Icon= line pointing into a directory that every update "
      "rewrites is one reshuffle away from a blank square")
check("the icon is COPIED there at launch",
      ".local/share/icons" in SH and "cp -f" in SH)
check("...preferring the image Ryan actually pointed at",
      ICONBLK.index("dose_icon_1024.png") < ICONBLK.index("dose_logo.png"),
      "he sent an image byte-identical to the mac_app icon; "
      "dose_logo.png is a DIFFERENT picture and was preferred first")
check("a fresh install copies that icon into place",
      "$APP_DIR/tools/mac_app/dose_icon_1024.png" in SH,
      "the branch updater syncs tools/, but the FIRST run copies a "
      "named list and the icon was not on it")
check("...and the launcher too",
      "$APP_DIR/tools/dose_launch.sh" in SH)
check("...and that image is in the repo",
      os.path.exists(os.path.join(ROOT, "tools", "mac_app",
                                  "dose_icon_1024.png")))
check("the name is short enough for a desktop label",
      any(ln.strip() == "Name=DOSE" for ln in entry.splitlines()),
      "'DOSE Home Station' wraps to three lines under an icon")
check("it is installed in the applications menu too",
      ".local/share/applications" in SH,
      "a desktop file is easy to lose behind a full-screen kiosk")

print("\n── DOSE.sh refuses to be the second copy ───────────────────")
check("it looks for an already-running app",
      "dose_app.py*" in SH and "_dose_other" in SH)
check("...before it does any work",
      SH.index("_dose_other") < SH.index("NEVER BLOCK, AND NEVER DIE"),
      "a guard that runs after the apt preflight has already cost 80 "
      "seconds and may already have touched the audio device")
check("...and exits WITHOUT starting anything",
      "Not starting a second copy" in SH and "exit 0" in
      SH.split("Not starting a second copy")[1][:900])
check("systemd is exempt, because the unit already guarantees one",
      'if [ -z "$INVOCATION_ID" ]; then' in SH,
      "without this, the service itself would refuse to start")
check("it says what to do instead",
      "systemctl --user restart dose-home-station" in SH,
      "'already running' with no next step is a dead end on a "
      "touchscreen")

print("\n── neither file trusts pgrep ───────────────────────────────")
# Eight times in this project, including inside the check written to
# FIND duplicate instances: `pgrep -f dose_app.py` matches the shell
# that is running the pgrep, and a script whose text contains the
# words matches too.
def code_only(src):
    """Strip comments before grepping for a tool name.

    "the launcher reads /proc, not pgrep" failed on a launcher that
    never calls pgrep — because its COMMENT explains why it does not.
    That is the ninth self-matching pattern in this project and the
    second one I have written today.
    """
    return "\n".join(ln for ln in src.splitlines()
                      if not ln.lstrip().startswith("#"))


for name, src in (("DOSE.sh guard",
                   code_only(SH.split("_dose_other=\"\"")[1][:1800])),
                  ("the launcher", code_only(LAUNCH))):
    check("%s reads /proc, not pgrep" % name,
          "/proc/" in src and "pgrep" not in src,
          "pgrep counts the process doing the asking")
    check("...%s matches comm first" % name,
          "comm" in src,
          "a shell that merely mentions dose_app.py has comm `bash`; "
          "only comm separates them")
    check("...%s skips its own pid" % name, "$$" in src)

print("\n── the launcher starts NOTHING when it is already up ───────")
first = LAUNCH.split("PID=$(running_pid)")[1]
first = first[:first.index("# Not running")]
check("the running case exits without starting anything",
      "systemctl" not in first and "DOSE.sh" not in first,
      first[:200])
check("...and tries to raise the window instead",
      "raise_window" in first)
check("when it IS down, it asks systemd rather than launching directly",
      "systemctl --user start" in LAUNCH
      and LAUNCH.index("systemctl --user start")
      < LAUNCH.index('exec /bin/bash "$APP_DIR/DOSE.sh"'),
      "the unit is the documented single start path, and it is also "
      "what restarts the station if it falls over")
check("DOSE.sh is the LAST resort, for a station with no unit",
      LAUNCH.rstrip().endswith('exec /bin/bash "$APP_DIR/DOSE.sh"'),
      "a board that has never been set up still has to start somehow")
check("it explains itself on a screen with no terminal",
      "zenity" in LAUNCH or "notify-send" in LAUNCH,
      "a message printed to a tty nobody has is not a message")

print("\n── clicking it asks for the latest build ───────────────────")
APPPY = open(os.path.join(ROOT, "dose_app.py"), encoding="utf-8").read()
check("the launcher leaves an update request",
      "voice/update_request" in LAUNCH)
check("...on EVERY route, not only when it starts the app",
      LAUNCH.index("request_update\n") < LAUNCH.index("PID=$(running_pid)"),
      "clicking it while the station is already up is exactly the "
      "case the start path cannot cover")
check("the app consumes it on its tick",
      "_consume_update_request" in APPPY
      and APPPY.index("self._consume_update_request()")
      < APPPY.index("self._check_presence_changes()"))
check("...deleting the file BEFORE acting",
      APPPY.split("def _consume_update_request(")[1].index("os.remove(path)")
      < APPPY.split("def _consume_update_request(")[1].index("_do_update_check"),
      "a request acted on without being removed fires again next "
      "second, forever")
check("...and it goes through the ordinary check, brakes and all",
      "_do_update_check" in
      APPPY.split("def _consume_update_request(")[1][:1400],
      "the persisted cooldown and the three-tries-per-hash limit "
      "exist because six pushes once meant six restart cycles; an "
      "icon must not be able to drive that loop")
check("...and still obeys DOSE_DISABLE_SELF_INSTALL",
      "DOSE_DISABLE_SELF_INSTALL" in
      APPPY.split("def _consume_update_request(")[1][:1400])

print("\n── and the old autostart lesson is still in force ──────────")
check("the autostart entry is still retired under systemd",
      "Retired the desktop autostart entry" in SH)
check("...and that block still exists at all",
      "$HOME/.config/autostart/dose.desktop" in SH,
      "this is the fix that took three attempts; do not let a "
      "refactor quietly drop it")

print("\n%d checks, %d failed" % (CHECKS[0], len(FAILURES)))
if FAILURES:
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("ONE STATION: the icon opens it, it never doubles it")
