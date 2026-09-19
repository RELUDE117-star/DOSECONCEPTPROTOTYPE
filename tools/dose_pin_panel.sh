#!/bin/bash
# Put the DOSE launcher on the Raspberry Pi's bottom bar.
#
# WHY THIS IS A FILE AND NOT A FEW LINES IN A JOB
# -----------------------------------------------
# It was a few lines in a job first, and they did not survive the
# trip. A queue job runs on the Mac, which runs `ssh pi`, which runs
# `sudo -u rjarv1 bash -c "..."`, which ran this script — four levels
# of quoting, and awk's `/^[panel]/` and a heredoc in the middle of
# it. The device's answer:
#
#     bash: -c: line 3: unexpected EOF while looking for matching `"'
#     /^[panel]/: No such file or directory
#     echo : File name too long
#
# Nothing was written, which is the one good thing about it. The fix
# is not more backslashes: it is to stop sending code through four
# shells. This file is copied to the Pi and executed, so the only
# thing that crosses those boundaries is a path.
#
# WHAT IT DOES
# ------------
# Raspberry Pi OS ships two different panels depending on age:
#
#   wf-panel-pi   Wayfire, Bookworm and later — ~/.config/wf-panel-pi.ini
#   lxpanel       X11, older releases         — ~/.config/lxpanel/*/panels/panel
#
# It detects which is actually there rather than assuming, backs the
# file up before touching it, and is safe to run twice. Ryan's
# standing rule: back up any config file before editing it, and
# validate before restarting anything.
set +e

APP_DIR="${DOSE_APP_DIR:-$HOME/dose-home-station}"
DESKTOP_ID="dose.desktop"
WF="$HOME/.config/wf-panel-pi.ini"

echo "panels running:"
for p in wf-panel-pi lxpanel xfce4-panel; do
    pgrep -x "$p" >/dev/null 2>&1 && echo "  $p"
done

# The panel resolves a launcher by desktop-id, looked up in the
# standard applications directories. The desktop file has to be
# THERE, not only on the Desktop, or the entry silently does nothing.
mkdir -p "$HOME/.local/share/applications"
if [ ! -f "$HOME/.local/share/applications/$DESKTOP_ID" ]; then
    if [ -f "$HOME/Desktop/DOSE.desktop" ]; then
        cp -f "$HOME/Desktop/DOSE.desktop" \
              "$HOME/.local/share/applications/$DESKTOP_ID"
        echo "copied the desktop entry into ~/.local/share/applications"
    else
        echo "NO DESKTOP ENTRY to pin — nothing to do."
        exit 1
    fi
fi
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null

DONE=no

# ── wf-panel-pi ─────────────────────────────────────────────────────
if [ -f "$WF" ]; then
    echo "found $WF"
    if grep -q "$DESKTOP_ID" "$WF"; then
        echo "  already pinned — leaving it alone"
        DONE=yes
    else
        cp -f "$WF" "$WF.before-dose"
        echo "  backed up to $(basename "$WF").before-dose"
        # NEXT FREE SLOT, not 000. launcher_000 is somebody else's
        # launcher and overwriting it would take a shortcut off his
        # bar to add ours.
        N=0
        while grep -q "^launcher_$(printf %03d $N)=" "$WF"; do
            N=$((N + 1))
        done
        SLOT=$(printf %03d $N)
        if grep -q '^\[panel\]' "$WF"; then
            awk -v line="launcher_${SLOT}=${DESKTOP_ID}" '
                /^\[panel\]/ && !ins { print; print line; ins = 1; next }
                { print }
            ' "$WF" > "$WF.new" && mv -f "$WF.new" "$WF"
        else
            printf '\n[panel]\nlauncher_%s=%s\n' "$SLOT" "$DESKTOP_ID" >> "$WF"
        fi
        if grep -q "$DESKTOP_ID" "$WF"; then
            echo "  added launcher_${SLOT}=${DESKTOP_ID}"
            DONE=yes
        else
            echo "  WRITE FAILED — restoring the backup"
            cp -f "$WF.before-dose" "$WF"
        fi
    fi
fi

# ── lxpanel ─────────────────────────────────────────────────────────
if [ "$DONE" = no ]; then
    LX=$(ls "$HOME"/.config/lxpanel/*/panels/panel 2>/dev/null | head -1)
    if [ -n "$LX" ]; then
        echo "found $LX"
        if grep -q "$DESKTOP_ID" "$LX"; then
            echo "  already pinned — leaving it alone"
            DONE=yes
        else
            cp -f "$LX" "$LX.before-dose"
            echo "  backed up"
            {
                printf '\nPlugin {\n'
                printf '  type=launchbar\n'
                printf '  Config {\n'
                printf '    Button {\n'
                printf '      id=%s\n' "$DESKTOP_ID"
                printf '    }\n'
                printf '  }\n'
                printf '}\n'
            } >> "$LX"
            if grep -q "$DESKTOP_ID" "$LX"; then
                echo "  appended a launchbar entry"
                DONE=yes
            else
                echo "  WRITE FAILED — restoring the backup"
                cp -f "$LX.before-dose" "$LX"
            fi
        fi
    fi
fi

if [ "$DONE" = no ]; then
    echo "NO PANEL CONFIG FOUND. The desktop icon still works;"
    echo "there is just nothing here to pin it to."
    exit 2
fi

# ── reload, so he does not have to reboot to see it ─────────────────
if pgrep -x wf-panel-pi >/dev/null 2>&1; then
    # wf-panel-pi re-reads its ini on SIGHUP on recent builds; on
    # older ones it has to be restarted. Try the cheap one first and
    # only kill it if the entry still is not showing.
    kill -HUP "$(pgrep -x wf-panel-pi | head -1)" 2>/dev/null
    sleep 2
    if ! pgrep -x wf-panel-pi >/dev/null 2>&1; then
        (setsid wf-panel-pi >/dev/null 2>&1 &)
        echo "wf-panel-pi restarted"
    else
        echo "wf-panel-pi signalled to reload"
    fi
elif pgrep -x lxpanel >/dev/null 2>&1; then
    lxpanelctl restart 2>/dev/null || true
    echo "lxpanel restarted"
fi

sleep 3
echo "--- the pin, as written ---"
grep -n "$DESKTOP_ID" "$WF" 2>/dev/null
grep -n "$DESKTOP_ID" "$HOME"/.config/lxpanel/*/panels/panel 2>/dev/null
echo "--- panels running after ---"
for p in wf-panel-pi lxpanel; do
    pgrep -x "$p" >/dev/null 2>&1 && echo "  $p ok"
done
exit 0
