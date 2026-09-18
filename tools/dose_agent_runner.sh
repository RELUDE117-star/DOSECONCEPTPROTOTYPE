#!/bin/bash
# DOSE agent runner — the ONLY path by which anything runs on this Mac.
#
#     bash ~/Documents/dose-agent/runner.sh
#
# It watches one directory for job scripts, runs them in this Mac's real
# shell (so `ssh dose-pi` works), and writes their output to out/.
#
# WHY THIS IS HARDENED
# A process that stands by executing whatever appears in a folder is,
# by definition, the most sensitive thing in this setup. The Raspberry
# Pi cannot reach it — the Pi has no credential for this Mac and SSH
# runs one way only — but anything already running as this user could
# drop a file here. So the runner refuses to execute anything that does
# not look exactly like a job placed deliberately by the owner:
#
#   * NOT owned by the user running the runner   -> refused
#   * group- or world-WRITABLE                   -> refused
#   * a symlink                                  -> refused
#   * not a regular .sh file                     -> refused
#
# Every job is logged with its SHA-256 before it runs, so there is an
# after-the-fact record of exactly what executed and when.
#
# STOP IT when the work is done: Ctrl-C in its window. Nothing else on
# this Mac depends on it.

AG="${DOSE_AGENT_DIR:-$HOME/Documents/dose-agent}"
mkdir -p "$AG/queue" "$AG/out" "$AG/done" "$AG/refused"
TIMEOUT=${TIMEOUT:-900}
LOG="$AG/runner-audit.log"
ME="$(id -un)"

note() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"; }

note "runner started pid=$$ user=$ME watching=$AG/queue"
note "refusing: non-.sh, symlinks, files not owned by $ME, group/world-writable"

refuse() {           # $1=path $2=reason
    local b; b="$(basename "$1")"
    note "REFUSED $b -- $2"
    mv "$1" "$AG/refused/$b.$(date +%s)" 2>/dev/null
}

while true; do
    date +%s > "$AG/heartbeat"
    for f in "$AG/queue"/*.sh; do
        [ -e "$f" ] || continue

        # ── gate, before anything is executed ──────────────────────
        if [ -L "$f" ]; then
            refuse "$f" "is a symlink"; continue
        fi
        if [ ! -f "$f" ]; then
            refuse "$f" "is not a regular file"; continue
        fi
        owner="$(stat -f '%Su' "$f" 2>/dev/null || stat -c '%U' "$f" 2>/dev/null)"
        if [ "$owner" != "$ME" ]; then
            refuse "$f" "owned by '$owner', not '$ME'"; continue
        fi
        perm="$(stat -f '%Sp' "$f" 2>/dev/null || stat -c '%A' "$f" 2>/dev/null)"
        # positions 6 and 9 are group-write and other-write
        gw="${perm:5:1}"; ow="${perm:8:1}"
        if [ "$gw" = "w" ] || [ "$ow" = "w" ]; then
            refuse "$f" "is group/world-writable ($perm)"; continue
        fi

        b="$(basename "$f" .sh)"
        sum="$(shasum -a 256 "$f" 2>/dev/null | cut -c1-16)"
        note "RUN  $b sha256=$sum"

        ( cd "$HOME" && bash "$f" ) > "$AG/out/$b.out" 2>&1 &
        pid=$!
        ( sleep "$TIMEOUT"; kill -TERM $pid 2>/dev/null
          sleep 5; kill -KILL $pid 2>/dev/null ) &
        wd=$!
        wait $pid; rc=$?
        kill $wd 2>/dev/null; wait $wd 2>/dev/null
        printf '\n__EXIT_CODE__=%s\n' "$rc" >> "$AG/out/$b.out"
        mv "$f" "$AG/done/$b.sh" 2>/dev/null
        note "DONE $b exit=$rc"
    done
    sleep 2
done
