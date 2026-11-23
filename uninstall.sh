#!/bin/bash
# Uninstaller for keylight-mk2.
#
# Stops and removes the LaunchAgents and the installed files. The minted
# certificates are kept (so a reinstall does not need Control Center) unless
# --purge is passed.

set -euo pipefail

APP_DIR="$HOME/Library/Application Support/keylight-mk2"
AGENTS="$HOME/Library/LaunchAgents"
LABELS=("com.lostmineinteractive.keylight-mk2" "com.lostmineinteractive.keylight-mk2-camera")
LOGS=("$HOME/Library/Logs/keylight-mk2.log" "$HOME/Library/Logs/keylight-mk2-camera.log")
PURGE=0

for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        -h | --help)
            echo "Usage: ./uninstall.sh [--purge]"
            echo "  --purge   also delete the minted certificates and the logs"
            exit 0
            ;;
        *)
            echo "unknown option: $arg" >&2
            exit 1
            ;;
    esac
done

DOMAIN="gui/$(id -u)"
for label in "${LABELS[@]}"; do
    if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
        launchctl bootout "$DOMAIN/$label" >/dev/null 2>&1 || true
        echo "Stopped $label"
    fi
    if [ -e "$AGENTS/$label.plist" ]; then
        rm -f "$AGENTS/$label.plist"
        echo "Removed $AGENTS/$label.plist"
    fi
done

# The CLI symlink and PATH line that install.sh added.
for dir in "$HOME/.local/bin" "$HOME/bin" /usr/local/bin; do
    if [ "$(readlink "$dir/keylight-mk2" 2>/dev/null || true)" = "$APP_DIR/bin/keylight-mk2" ]; then
        rm -f "$dir/keylight-mk2"
        echo "Removed $dir/keylight-mk2"
    fi
done
for profile in "$HOME/.zprofile" "$HOME/.bash_profile"; do
    if grep -qF "# added by keylight-mk2" "$profile" 2>/dev/null; then
        sed -i '' '/# added by keylight-mk2$/d' "$profile"
        echo "Removed the PATH line from $profile"
    fi
done

if [ "$PURGE" = 1 ]; then
    rm -rf "$APP_DIR"
    rm -f "${LOGS[@]}"
    echo "Removed $APP_DIR (including the minted certificates) and the logs"
elif [ -d "$APP_DIR" ]; then
    for item in venv keylight_mk2 bin config.json state.json camera-watcher.conf; do
        rm -rf "${APP_DIR:?}/$item"
    done
    echo "Removed the daemon, virtualenv and config from $APP_DIR"
    if [ -d "$APP_DIR/certs" ]; then
        echo "Kept the minted certificates in $APP_DIR/certs (delete them with --purge)"
    else
        rmdir "$APP_DIR" 2>/dev/null || true
    fi
fi
echo "Uninstalled."
