#!/bin/bash
# Opt-in: turn the light on when the camera starts and off when it stops.
# Installs camera-watcher.sh as its own LaunchAgent. Requires the bridge
# (../install.sh) to be installed first. Safe to re-run.
#
#   ./install-camera-trigger.sh             unified-log watcher (no compiler needed)
#   ./install-camera-trigger.sh --swift     build and use the CoreMediaIO probe instead
#   ./install-camera-trigger.sh --uninstall remove the camera LaunchAgent

set -euo pipefail

LABEL="com.lostmineinteractive.keylight-mk2-camera"
APP_DIR="$HOME/Library/Application Support/keylight-mk2-bridge"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_FILE="$HOME/Library/Logs/keylight-mk2-camera.log"
WATCHER="$APP_DIR/bin/camera-watcher.sh"
CONF="$APP_DIR/camera-watcher.conf"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOMAIN="gui/$(id -u)"

die() {
    echo "error: $*" >&2
    exit 1
}

stop_agent() {
    launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
        sleep 0.5
    done
}

SWIFT=0
case "${1:-}" in
    "") ;;
    --swift) SWIFT=1 ;;
    --uninstall)
        stop_agent
        rm -f "$PLIST" "$WATCHER" "$APP_DIR/bin/camera-probe"
        echo "Removed the camera trigger (settings kept in $CONF)."
        exit 0
        ;;
    *)
        sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
        exit 1
        ;;
esac

[ -x "$APP_DIR/venv/bin/python" ] && [ -f "$APP_DIR/config.json" ] ||
    die "the bridge is not installed yet; run ../install.sh first"

install -m 755 "$HERE/camera-watcher.sh" "$WATCHER"

if [ ! -f "$CONF" ]; then
    # Point the watcher at the bridge as configured (port, optional secret).
    "$APP_DIR/venv/bin/python" - "$APP_DIR/config.json" >"$CONF" <<'PY'
import json, shlex, sys

config = json.load(open(sys.argv[1]))
bind = config.get("bind") or "127.0.0.1"
if bind in ("0.0.0.0", "::"):
    bind = "127.0.0.1"
print("# Settings for camera-watcher.sh; see extras/README.md for every option.")
print(f"BRIDGE_URL={shlex.quote('http://%s:%s' % (bind, config.get('port', 9124)))}")
if config.get("secret"):
    print(f"BRIDGE_SECRET={shlex.quote(str(config['secret']))}")
PY
    chmod 600 "$CONF"
fi

if [ "$SWIFT" = 1 ]; then
    command -v swiftc >/dev/null 2>&1 ||
        die "swiftc not found; install the Xcode command line tools (xcode-select --install)"
    echo "Building the CoreMediaIO camera probe..."
    swiftc -O "$HERE/camera-probe.swift" -o "$APP_DIR/bin/camera-probe"
    "$WATCHER" set BACKEND probe
else
    "$WATCHER" set BACKEND log
fi

render() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/[\\&|]/\\&/g'
}
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
sed -e "s|{{WATCHER}}|$(render "$WATCHER")|g" -e "s|{{LOG}}|$(render "$LOG_FILE")|g" \
    "$HERE/$LABEL.plist.template" >"$PLIST"
plutil -lint "$PLIST" >/dev/null || die "rendered an invalid plist at $PLIST"

stop_agent
launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null ||
    { sleep 2 && launchctl bootstrap "$DOMAIN" "$PLIST"; } ||
    die "launchctl could not load $PLIST"
launchctl enable "$DOMAIN/$LABEL" || true

cat <<EOF
Camera trigger installed ($([ "$SWIFT" = 1 ] && echo "CoreMediaIO probe" || echo "unified-log watcher")).

  Try it:     start a video preview (Photo Booth, FaceTime) and watch the light
  Log:        $LOG_FILE
  Settings:   $CONF
EOF
if [ "$SWIFT" = 0 ]; then
    cat <<EOF

If the light does not react, the log lines differ on this macOS version. Run
  "$WATCHER" calibrate
or switch to the more robust probe:  $0 --swift
EOF
fi
