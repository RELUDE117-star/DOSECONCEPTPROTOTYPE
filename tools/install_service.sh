#!/bin/bash
# Install the DOSE Home Station systemd user unit.
#
# Run this ON THE PI, AS THE KIOSK USER (rjarv1) — not as root, not as
# claudeagent. It needs no sudo: user units live in the user's own
# ~/.config/systemd/user.
#
#     bash ~/dose-home-station/tools/install_service.sh
#
# It is idempotent and reversible. Before it enables anything it:
#   * backs up any existing unit file
#   * verifies DOSE.sh actually exists where the unit points
#   * refuses to run if another copy of the app is already running, so you
#     never end up with two instances fighting over the microphone
#
# Undo with:  bash ~/dose-home-station/tools/install_service.sh --uninstall

set -u

APP_DIR="$HOME/dose-home-station"
UNIT_NAME="dose-home-station.service"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_DST="$UNIT_DIR/$UNIT_NAME"
UNIT_SRC="$APP_DIR/tools/$UNIT_NAME"

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
    say "Disabling and removing $UNIT_NAME ..."
    systemctl --user disable --now "$UNIT_NAME" 2>/dev/null
    rm -f "$UNIT_DST"
    systemctl --user daemon-reload
    say "Removed. The app is no longer managed by systemd."
    say "Nothing was deleted from $APP_DIR."
    exit 0
fi

# ── sanity ────────────────────────────────────────────────────────────
[ "$(id -u)" -ne 0 ] || die "do not run this as root — it installs a USER unit"
[ -d "$APP_DIR" ] || die "no app directory at $APP_DIR"
[ -f "$APP_DIR/DOSE.sh" ] || die \
    "no DOSE.sh at $APP_DIR/DOSE.sh — the unit would start nothing.
     The audit found the launcher being run from a Desktop folder instead.
     Copy it into the app directory first, then re-run this."
[ -f "$UNIT_SRC" ] || die "unit file missing at $UNIT_SRC"

# Refuse to fight an app that is already up by other means.
RUNNING=$(pgrep -f "python3 $APP_DIR/dose_app.py" | head -5)
if [ -n "$RUNNING" ]; then
    say "WARNING: dose_app.py is already running (pid: $(echo $RUNNING | tr '\n' ' '))."
    say "It was probably started from the Desktop launcher or a terminal."
    say "Stop it first, or you will end up with two instances competing for"
    say "the microphone — which is its own class of bug."
    say ""
    printf 'Stop it now and continue? [y/N] '
    read -r ans
    case "$ans" in
        y|Y)
            # TERM, not KILL: the app must get the chance to release the
            # USB capture device cleanly. A SIGKILLed recorder strands the
            # ALSA PCM and the mic becomes unopenable until reboot.
            pkill -TERM -f "python3 $APP_DIR/dose_app.py"
            for _ in $(seq 1 20); do
                pgrep -f "python3 $APP_DIR/dose_app.py" >/dev/null || break
                sleep 1
            done
            if pgrep -f "python3 $APP_DIR/dose_app.py" >/dev/null; then
                die "it did not exit after 20s — stop it by hand and re-run"
            fi
            say "Stopped cleanly."
            ;;
        *) die "aborted — nothing changed" ;;
    esac
fi

# ── install ───────────────────────────────────────────────────────────
mkdir -p "$UNIT_DIR" "$APP_DIR/logs"

if [ -f "$UNIT_DST" ]; then
    BACKUP="$UNIT_DST.bak.$(date +%Y%m%d-%H%M%S)"
    cp -p "$UNIT_DST" "$BACKUP"
    say "Backed up existing unit -> $BACKUP"
fi

cp "$UNIT_SRC" "$UNIT_DST"
systemctl --user daemon-reload || die "daemon-reload failed"

# Validate before enabling, so a broken unit is never left enabled.
if ! systemctl --user cat "$UNIT_NAME" >/dev/null 2>&1; then
    die "systemd will not parse $UNIT_NAME — nothing enabled"
fi

systemctl --user enable "$UNIT_NAME" || die "enable failed"
say "Enabled $UNIT_NAME (starts with the graphical session)."

# Survive the user not being logged in graphically, if that ever matters.
if command -v loginctl >/dev/null 2>&1; then
    loginctl enable-linger "$USER" 2>/dev/null \
        && say "Lingering enabled for $USER."
fi

systemctl --user start "$UNIT_NAME" || die "start failed — see: systemctl --user status $UNIT_NAME"

sleep 3
say ""
systemctl --user --no-pager status "$UNIT_NAME" | head -15
say ""
say "Done. Useful from here:"
say "  systemctl --user status  $UNIT_NAME"
say "  systemctl --user restart $UNIT_NAME"
say "  journalctl --user -u $UNIT_NAME -f"
say "  tail -f $APP_DIR/logs/dose.log"
say ""
say "IMPORTANT: remove the old Desktop launcher so it cannot start a"
say "second instance at login. The audit found it at:"
say "  ~/Desktop/DOSECONCEPTPROTOTYPE-claude-quirky-brown-vkHwi/DOSE.sh"
