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
    "$PY" -m venv "$VENV" || { echo "  venv failed"; exit 1; }
fi
say "installing faster-whisper (once; this is the only download)"
"$VENV/bin/python3" -m pip install --quiet --upgrade pip >/dev/null 2>&1
"$VENV/bin/python3" -m pip install --quiet faster-whisper || {
    echo "  pip failed — the app will still install, but the speech"
    echo "  server will not start until this works."; }

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

# The launcher. Starts the panel, which opens in the browser; the panel
# starts and stops the speech server on request. Closing the panel
# (Ctrl-C in its window, or quitting) takes the server with it.
cat > "$APP/Contents/MacOS/DOSE" <<LAUNCH
#!/bin/bash
export DOSE_PI_HOST="\${DOSE_PI_HOST:-dose-pi}"
export DOSE_PI_ADDR="\${DOSE_PI_ADDR:-192.168.4.154}"
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:\$PATH"
exec "$VENV/bin/python3" "$SRC/tools/dose_panel.py"
LAUNCH
chmod 755 "$APP/Contents/MacOS/DOSE"

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
