#!/bin/bash
# The desktop icon. It CANNOT start a second copy of the station.
#
# WHY THIS EXISTS
# ---------------
# Ryan:
#
#     "the app that you open on the pi versus the app that I open on
#      the pi through dose.sh seem to be different and sometimes both
#      of them are on at the same time ... this could be a cause of
#      one of the issues we were having that two sessions are going on
#      at the same time which is why their could be glitches"
#
# He is right, and this project has already paid for that lesson once.
# CLAUDE.md, under "TWO COPIES OF THE APP WERE RUNNING":
#
#     pid 2543  ppid 1440  1126 MB  cgroup session-1.scope
#     pid 2549  ppid 2001  1387 MB  cgroup dose-home-station.svc
#
# Two complete instances, 2.5 GB of a 3.8 GB board, two copies of
# every speech model, and two processes fighting over one USB
# microphone — which is where pages of `AlsaOpen failed` in the log
# came from. Not a driver mystery. The other instance was holding the
# device.
#
# THE DESKTOP SHORTCUT WAS DOING EXACTLY THAT. It read:
#
#     Exec=/bin/bash /home/rjarv1/dose-home-station/DOSE.sh
#
# DOSE.sh starts the application. So every time he opened the station
# from his desktop while the systemd service was already running, he
# got a second one. The glitches he has been describing are the
# documented symptom of the thing his own shortcut was doing.
#
# It also had no `Icon=` line at all, which is why there was no logo
# to click and why he ended up running DOSE.sh by hand in the first
# place. The missing icon and the duplicate instance are the same
# bug wearing two faces.
#
# WHAT THIS DOES INSTEAD
# ----------------------
#   1. Is the station already running? -> raise its window. Start
#      nothing.
#   2. Not running, and there is a systemd unit? -> ask systemd to
#      start it. The unit is the ONLY start path on a set-up station;
#      this file never launches the app itself.
#   3. No unit at all (a station that has never been through setup)?
#      -> run DOSE.sh, because then it really is the start path.
#
# The order matters and so does step 1 being first: the cheapest,
# safest answer to "open the app" when the app is open is to show it
# to him.
set +e

APP_DIR="${DOSE_APP_DIR:-$HOME/dose-home-station}"
UNIT="dose-home-station.service"

# ── FIND IT BY /proc, NOT BY pgrep ──────────────────────────────────
#
# `pgrep -f dose_app.py` counts the shell running the pgrep, and this
# project has been bitten by that EIGHT times — including once inside
# the very check written to detect duplicate instances, which reported
# "instances: 2" when the second was its own `bash -c`.
#
# A python process has comm `python3`; a shell that merely mentions
# the string has comm `bash`. Match comm first, then confirm with the
# command line, and skip our own pid.
running_pid() {
    local d pid comm cmd
    for d in /proc/[0-9]*; do
        pid="${d#/proc/}"
        [ "$pid" = "$$" ] && continue
        comm=$(cat "$d/comm" 2>/dev/null) || continue
        case "$comm" in python*) ;; *) continue;; esac
        cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
        case "$cmd" in *dose_app.py*) echo "$pid"; return 0;; esac
    done
    return 1
}

# ── BRING IT TO THE FRONT ───────────────────────────────────────────
#
# Best effort, and it says so. The station runs full-screen as a
# kiosk, so in practice it is already the visible window and there is
# nothing to raise — but if he has dropped to the desktop, this is
# what he actually wanted when he clicked the icon.
raise_window() {
    if command -v wmctrl >/dev/null 2>&1; then
        wmctrl -a "DOSE" 2>/dev/null && return 0
    fi
    if command -v xdotool >/dev/null 2>&1; then
        local w
        w=$(xdotool search --name "DOSE" 2>/dev/null | head -1)
        [ -n "$w" ] && xdotool windowactivate "$w" 2>/dev/null && return 0
    fi
    return 1
}

say() {
    # On a touchscreen with no terminal, a message has to be a dialog
    # or it may as well not exist.
    if command -v zenity >/dev/null 2>&1; then
        zenity --info --timeout=4 --title="DOSE" --text="$1" 2>/dev/null &
    elif command -v notify-send >/dev/null 2>&1; then
        notify-send "DOSE" "$1" 2>/dev/null
    fi
    echo "$1"
}

# ── ASK FOR THE LATEST BUILD, EVERY TIME THE ICON IS CLICKED ────────
#
# Ryan: "make sure it set up for this one that you are adding directly
# into my desktop on the raspberry pi that it auto updates to the most
# recent github version when you start it up".
#
# Half of that was already true. Starting the station runs its own
# update check two seconds in. What was NOT covered is clicking the
# icon while it is already running — which must not start a second
# copy, so it cannot go through the start path at all.
#
# So: leave a request. dose_app.py consumes voice/update_request on
# its one-second tick and runs the ordinary check, WITH its ordinary
# brakes — the persisted fifteen-minute cooldown and the three-tries
# -per-hash limit that exist because six pushes in an evening once
# meant six update-and-restart cycles. Clicking an icon repeatedly
# must not be able to drive that loop.
#
# Written BEFORE the branch below, so it happens on every route: if
# the app is up it is consumed within a second; if it is starting, it
# is consumed by the new process on its first tick.
request_update() {
    mkdir -p "$APP_DIR/voice" 2>/dev/null
    : > "$APP_DIR/voice/update_request" 2>/dev/null
}
request_update

PID=$(running_pid)
if [ -n "$PID" ]; then
    # ALREADY RUNNING. This is the whole point of the file.
    if raise_window; then
        exit 0
    fi
    say "DOSE is already running. Checking for updates."
    exit 0
fi

# Not running. Prefer systemd, which is the documented single start
# path and the thing that will restart it if it ever falls over.
if systemctl --user list-unit-files "$UNIT" >/dev/null 2>&1 && \
   systemctl --user cat "$UNIT" >/dev/null 2>&1; then
    systemctl --user start "$UNIT" 2>/dev/null
    # Give it a moment so a second click does not race the first.
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        sleep 1
        [ -n "$(running_pid)" ] && exit 0
    done
    say "DOSE is starting. Give it a moment."
    exit 0
fi

# No unit — this station has never been set up, so DOSE.sh really is
# the start path. Left as a fallback rather than removed: a fresh
# board has to be able to start somehow.
exec /bin/bash "$APP_DIR/DOSE.sh"
