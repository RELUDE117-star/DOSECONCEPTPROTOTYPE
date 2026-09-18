#!/bin/bash
# Build and install "DOSE PI CONNECTOR" into /Applications.
#
# WHAT THIS PUTS ON THE MAC
#   /Applications/DOSE PI CONNECTOR.app   the icon you click
#   ~/.dose-server/                       token, GitHub token, log
#   ~/.dose-server/venv/                  faster-whisper, installed once
#
# WHAT IT DOES NOT PUT ANYWHERE
#   Nothing in a login item, nothing in launchd, nothing that starts by
#   itself, nothing listening until you open the app. Close the app and
#   both listeners go with it.
#
# THE TWO LISTENERS, AND WHY THEY ARE SEPARATE
#   The control panel binds 127.0.0.1 only. It holds a GitHub token and
#   can open an SSH session, so nothing on the network may reach it —
#   not the Pi, not the router, not a phone on the Wi-Fi.
#   The speech server binds ONE LAN address, wants a bearer token, and
#   has exactly one route that takes a WAV and returns a string.
#   Two jobs with two different blast radii do not share a listener.
#
#   bash tools/mac_app/install_dose_app.sh
set -u

APP_NAME="DOSE PI CONNECTOR"
APP="/Applications/${APP_NAME}.app"
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
STATE="$HOME/.dose-server"
VENV="$STATE/venv"
PY="$(command -v python3)"

say() { printf '  %s\n' "$*"; }

echo "=== installing ${APP_NAME} ==="
say "source:   $SRC"
say "python:   $PY  ($("$PY" --version 2>&1))"

# ── 1. a place for state, readable only by you ───────────────────────
mkdir -p "$STATE"
chmod 700 "$STATE"
say "state:    $STATE (mode 700)"

# ── 2. the models, in a venv of their own ────────────────────────────
# Apple's system python is 3.9 and must not be written into. A venv
# keeps faster-whisper and its dependencies out of the system entirely,
# and deleting the directory undoes all of it.
if [ ! -x "$VENV/bin/python3" ]; then
    say "creating a virtual environment (once)"
    "$PY" -m venv "$VENV" 2>&1 | tail -3 | sed 's/^/    /'
fi
if [ ! -x "$VENV/bin/python3" ]; then
    # NOT fatal any more, and this matters. The first version did
    # `exit 1` here, so a Mac without the venv got no app at all —
    # and the launcher it would have written exec'd that very venv,
    # so even a partial install produced a double-click that did
    # nothing whatsoever. The panel needs no venv. Install it.
    say "venv:     COULD NOT BE CREATED"
    say "          The control panel will still work — it is standard"
    say "          library only. The speech server will not, until"
    say "          this is fixed. Re-run this script to retry."
else
    say "installing faster-whisper (once; this is the only download)"
    "$VENV/bin/python3" -m pip install --quiet --upgrade pip >/dev/null 2>&1
    if "$VENV/bin/python3" -m pip install --quiet faster-whisper; then
        say "models:   faster-whisper ready"
    else
        say "models:   pip FAILED — the panel will work and will say"
        say "          the speech server is unavailable."
    fi
fi

# ── 3. the bundle ────────────────────────────────────────────────────
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!-- No DOCTYPE line. The usual one carries an apple.com URL, which is
     never fetched by anything — but a URL in a file on this device is
     a URL the egress guard has to be told to ignore, and an allowlist
     with an entry nobody can justify is how allowlists rot. macOS does
     not need it. -->
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>DOSE PI CONNECTOR</string>
  <key>CFBundleDisplayName</key><string>DOSE PI CONNECTOR</string>
  <key>CFBundleIdentifier</key><string>com.dose.piconnector</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>DOSE</string>
  <key>CFBundleIconFile</key><string>DOSE</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

# The launcher.
#
# IT MUST NEVER FAIL SILENTLY. The first version was one line —
#
#     exec "$VENV/bin/python3" "$SRC/tools/dose_panel.py"
#
# — and this app is LSUIElement, so it has no dock icon, no window and
# no terminal. When that venv did not exist, a double-click did
# absolutely nothing: no bounce, no error, no log. Ryan double-clicked
# it, then dragged it to the Desktop and tried again, and reasonably
# asked whether he had broken something. He had not; the app had no way
# to tell him anything.
#
# Three changes, all of them about being honest:
#   • the panel is pure standard library, so it falls back to the
#     system python when the venv is missing. The venv is only needed
#     by the SPEECH SERVER, which the panel starts on request — so a
#     half-finished install now gives a working control panel that
#     says the speech half is not ready, instead of nothing at all.
#   • the panel and server are copied INTO the bundle, so moving or
#     deleting the source checkout cannot break the app.
#   • anything that goes wrong is written to a log AND put on screen
#     with osascript, because an app with no window cannot report an
#     error any other way.
cat > "$APP/Contents/MacOS/DOSE" <<'LAUNCH'
#!/bin/bash
export DOSE_PI_HOST="${DOSE_PI_HOST:-dose-pi}"
export DOSE_PI_ADDR="${DOSE_PI_ADDR:-192.168.4.154}"
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

HERE="$(cd "$(dirname "$0")/.." && pwd)"          # …/Contents
RES="$HERE/Resources"
STATE="$HOME/.dose-server"
LOG="$STATE/launch.log"
mkdir -p "$STATE" 2>/dev/null
chmod 700 "$STATE" 2>/dev/null

note() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

fail() {
    note "FAILED: $*"
    /usr/bin/osascript -e "display dialog \"DOSE PI CONNECTOR could not start.

$1

Details were written to:
~/.dose-server/launch.log\" with title \"DOSE PI CONNECTOR\" buttons {\"OK\"} default button 1 with icon caution" >/dev/null 2>&1
    exit 1
}

note "launch: bundle at $HERE"

# The panel, from the bundle first, then the source checkout.
PANEL=""
for c in "$RES/dose_panel.py" "__SRC__/tools/dose_panel.py"; do
    [ -f "$c" ] && { PANEL="$c"; break; }
done
[ -n "$PANEL" ] || fail "The control panel is missing from the app bundle.
Re-run the installer:  bash tools/mac_app/install_dose_app.sh"
note "panel: $PANEL"

# The interpreter. The venv is PREFERRED because the speech server
# needs it, but the panel itself is standard library only, so a missing
# venv must not stop the panel opening.
PY=""
for c in "$HOME/.dose-server/venv/bin/python3" /usr/bin/python3 \
         /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    [ -x "$c" ] && { PY="$c"; break; }
done
[ -n "$PY" ] || fail "No Python 3 could be found on this Mac."
note "python: $PY"
case "$PY" in
    *"/.dose-server/venv/"*) ;;
    *) note "NOTE: running on the system python; the speech server \
needs the venv (re-run the installer to create it)."
       export DOSE_SPEECH_UNAVAILABLE=1 ;;
esac

note "starting panel"
"$PY" "$PANEL" >>"$LOG" 2>&1
RC=$?
note "panel exited rc=$RC"
[ "$RC" -eq 0 ] || fail "The control panel stopped unexpectedly (exit $RC).
The last lines of the log will say why."
LAUNCH
# The source path is substituted in rather than expanded by the
# heredoc, so the rest of the script above stays literal.
python3 - "$APP/Contents/MacOS/DOSE" "$SRC" <<'PYSUB' 2>/dev/null || \
  sed -i '' "s|__SRC__|$SRC|g" "$APP/Contents/MacOS/DOSE"
import sys
p, src = sys.argv[1], sys.argv[2]
t = open(p).read().replace("__SRC__", src)
open(p, "w").write(t)
PYSUB
chmod 755 "$APP/Contents/MacOS/DOSE"

# The panel and the server, copied IN, so the app does not depend on a
# checkout that can move, be renamed, or be deleted.
for f in dose_panel.py dose_server.py; do
    [ -f "$SRC/tools/$f" ] && cp "$SRC/tools/$f" "$APP/Contents/Resources/$f"
done
say "bundled:  dose_panel.py, dose_server.py"

# ── 4. the icon ──────────────────────────────────────────────────────
ICON_SRC="$SRC/tools/mac_app/dose_icon_1024.png"
if [ -f "$ICON_SRC" ]; then
    TMP="$(mktemp -d)/DOSE.iconset"
    mkdir -p "$TMP"
    for s in 16 32 64 128 256 512; do
        sips -z $s $s "$ICON_SRC" --out "$TMP/icon_${s}x${s}.png" >/dev/null 2>&1
        d=$((s*2))
        sips -z $d $d "$ICON_SRC" --out "$TMP/icon_${s}x${s}@2x.png" >/dev/null 2>&1
    done
    sips -z 1024 1024 "$ICON_SRC" --out "$TMP/icon_512x512@2x.png" >/dev/null 2>&1
    iconutil -c icns "$TMP" -o "$APP/Contents/Resources/DOSE.icns" 2>/dev/null \
        && say "icon:     installed" || say "icon:     iconutil declined; using the png"
    [ -f "$APP/Contents/Resources/DOSE.icns" ] || \
        cp "$ICON_SRC" "$APP/Contents/Resources/DOSE.png"
else
    say "icon:     not found at $ICON_SRC"
fi

# ── 5. instructions, where a Claude session will always find them ────
# Ryan: "have the app include instructions so you Claude Cowork can
# always see it and instantly know what it needs to do and how to use
# it." Two stable paths: inside the bundle, and in the state directory,
# which is the first place to look and does not move when the app is
# reinstalled.
INSTR="$SRC/tools/mac_app/CLAUDE_INSTRUCTIONS.md"
if [ -f "$INSTR" ]; then
    cp "$INSTR" "$APP/Contents/Resources/CLAUDE_INSTRUCTIONS.md"
    cp "$INSTR" "$STATE/CLAUDE_INSTRUCTIONS.md"
    chmod 600 "$STATE/CLAUDE_INSTRUCTIONS.md"
    say "notes:    $STATE/CLAUDE_INSTRUCTIONS.md"
fi

# ── 6. make Finder notice ────────────────────────────────────────────
touch "$APP"
/usr/bin/touch "$APP/Contents/Info.plist"
killall Finder >/dev/null 2>&1 || true

echo
say "installed: $APP"
say "open it from Applications, or: open -a \"${APP_NAME}\""
echo
echo "  The panel listens on 127.0.0.1 only. The speech server listens"
echo "  on this Mac's LAN address, wants a token, and can do exactly one"
echo "  thing: take a WAV and give back a string. Nothing starts by"
echo "  itself and nothing is reachable from outside this house."
