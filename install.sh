#!/bin/bash
# Installer for keylight-mk2. Idempotent: safe to re-run.
#
# Mints a client certificate from the PKI inside Elgato Control Center, installs
# the daemon into ~/Library/Application Support/keylight-mk2, discovers the
# light, starts the daemon under launchd, and cycles the light as a smoke test.

set -euo pipefail

LABEL="com.lostmineinteractive.keylight-mk2"
APP_DIR="$HOME/Library/Application Support/keylight-mk2"
CERTS="$APP_DIR/certs"
VENV="$APP_DIR/venv"
CONFIG="$APP_DIR/config.json"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_FILE="$HOME/Library/Logs/keylight-mk2.log"
PKI_NAME="pki_Key_Light_Air_Mk2"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PKI_ARG=""
APP_ARG=""
PYTHON_ARG=""
PORT_ARG=""
HOSTS=()
PYPI=0
ASSUME_YES=0
REMINT=0
REDISCOVER=0
SMOKE=1

usage() {
    cat <<'EOF'
Usage: ./install.sh [options]

  --host <name>     Light hostname or IP; skips mDNS discovery (repeatable)
  --pki <dir>       Path to the pki_Key_Light_Air_Mk2 directory
  --app <path>      Path to the Control Center app (any name, any location)
  --port <n>        HTTP port for the bridge (default 9124)
  --python <path>   Python 3.9+ for the virtualenv (default: Apple's /usr/bin/python3)
  --pypi            Install the Python packages from public PyPI, ignoring any
                    pip config or PIP_* settings that point at another index
  --rediscover      Replace the lights in an existing config.json
  --remint          Mint a fresh client certificate even if one exists
  --no-smoke-test   Skip the on/off/on cycle at the end
  -y, --yes         Answer yes to prompts (e.g. expanding a downloaded zip)
  -h, --help        Show this help
EOF
}

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    \033[33mwarning:\033[0m %s\n' "$*" >&2; }
die() {
    printf '\n\033[31merror:\033[0m %s\n' "$*" >&2
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --host | --pki | --app | --port | --python)
            [ $# -ge 2 ] || die "$1 needs a value"
            case "$1" in
                --host) HOSTS+=("$2") ;;
                --pki) PKI_ARG="$2" ;;
                --app) APP_ARG="$2" ;;
                --port) PORT_ARG="$2" ;;
                --python) PYTHON_ARG="$2" ;;
            esac
            shift 2
            ;;
        --rediscover) REDISCOVER=1 && shift ;;
        --remint) REMINT=1 && shift ;;
        --no-smoke-test) SMOKE=0 && shift ;;
        --pypi) PYPI=1 && shift ;;
        -y | --yes) ASSUME_YES=1 && shift ;;
        -h | --help) usage && exit 0 ;;
        *) usage >&2 && die "unknown option: $1" ;;
    esac
done

WORK="$(mktemp -d "${TMPDIR:-/tmp}/keylight-mk2.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

confirm() {
    [ "$ASSUME_YES" = 1 ] && return 0
    [ -t 0 ] || return 1
    local reply
    read -r -p "    $1 [y/N] " reply
    case "$reply" in [yY]*) return 0 ;; esac
    return 1
}

# ---------------------------------------------------------------- 1. preflight

step "Checking prerequisites"
[ "$(uname -s)" = "Darwin" ] || die "this installer only supports macOS"
for tool in openssl curl launchctl; do
    command -v "$tool" >/dev/null 2>&1 || die "'$tool' not found on PATH"
done

python_ok() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; }

# Which Python runs the daemon matters. macOS only lets a program started by
# launchd onto the local network once the user allows it, and it tracks that
# permission by code signature. Apple's own Python (Xcode or the Command Line
# Tools) is properly signed, so the permission sticks. Homebrew's Python is
# ad hoc signed and was still refused in testing with the permission granted.
# (Without the developer tools /usr/bin/python3 is a stub that pops up an
# install dialog, hence the xcode-select check.)
if [ -n "$PYTHON_ARG" ]; then
    PYTHON="$PYTHON_ARG"
elif xcode-select -p >/dev/null 2>&1 && python_ok /usr/bin/python3; then
    PYTHON="/usr/bin/python3"
else
    PYTHON="$(command -v python3 || true)"
fi
[ -n "$PYTHON" ] && [ -x "$PYTHON" ] ||
    die "python3 not found. Install it with 'xcode-select --install' or pass --python <path>"
python_ok "$PYTHON" || die "$PYTHON is not a working Python 3.9 or newer; pass --python <path>"
PYTHON_BASE="$("$PYTHON" -c 'import os, sys; print(os.path.realpath(sys.base_prefix))')"
ADHOC=0
if codesign -dv "$("$PYTHON" -c 'import os, sys; print(os.path.realpath(sys.executable))')" 2>&1 |
    grep -q 'Signature=adhoc'; then
    ADHOC=1
fi
info "macOS $(sw_vers -productVersion), $("$PYTHON" --version 2>&1) ($PYTHON), $(openssl version | cut -d' ' -f1-2)"
if [ "$ADHOC" = 1 ]; then
    warn "$PYTHON is ad hoc signed (typical of Homebrew). macOS may keep the daemon off"
    warn "the local network; if the smoke test fails, install Apple's Python with"
    warn "'xcode-select --install' and re-run, or pass --python /usr/bin/python3."
fi

# ------------------------------------------------------------ 2. locate the PKI

pki_valid() {
    [ -r "$1/root.crt.pem" ] && [ -r "$1/intermediate.crt.pem" ] && [ -r "$1/intermediate.key.pem" ]
}

# Print the first PKI directory at or below $1 (an app, a folder, or the dir itself).
pki_in() {
    local root="${1%/}" hit
    [ -e "$root" ] || return 1
    if [ -d "$root" ] && [ "$(basename "$root")" = "$PKI_NAME" ]; then
        printf '%s\n' "$root"
        return 0
    fi
    hit="$(find "$root" -type d -name "$PKI_NAME" 2>/dev/null | head -n1 || true)"
    [ -n "$hit" ] && printf '%s\n' "$hit"
}

# The app has shipped as both "Elgato Control Center.app" and "ControlCenter.app"
# and can live anywhere, so search for the PKI directory rather than an app path.
pki_from_system() {
    local found
    found="$(
        {
            mdfind -name 'ControlCenter.app' 2>/dev/null
            mdfind -name 'Elgato Control Center.app' 2>/dev/null
        } | while read -r app; do
            find "$app" -type d -name "$PKI_NAME" 2>/dev/null
        done
        find /Applications "$HOME/Applications" "$HOME/Downloads" \
            -type d -name "$PKI_NAME" 2>/dev/null
    )" || true
    printf '%s\n' "$found" | head -n1
}

pki_from_zip() {
    local zip="$1" dest
    dest="$(mktemp -d "$WORK/unzip.XXXXXX")"
    ditto -x -k "$zip" "$dest" 2>/dev/null || unzip -q "$zip" -d "$dest" 2>/dev/null || return 1
    pki_in "$dest"
}

# Control Center zips in ~/Downloads, newest first. The real download is named
# like ElgatoControlCenter-1.9.2.21032.app.zip, so match loosely.
downloaded_zips() {
    find "$HOME/Downloads" -maxdepth 1 -type f -iname '*control*center*.zip' \
        -exec stat -f '%m %N' {} + 2>/dev/null | sort -rn | cut -d' ' -f2-
}

# Terminal escapes or quotes a path that is dragged into the window.
clean_path() {
    local p="$1"
    p="${p#"${p%%[![:space:]]*}"}"
    p="${p%"${p##*[![:space:]]}"}"
    case "$p" in
        \'*\') p="${p#\'}" && p="${p%\'}" ;;
        \"*\") p="${p#\"}" && p="${p%\"}" ;;
        *) p="$(printf '%s' "$p" | sed 's/\\\(.\)/\1/g')" ;;
    esac
    case "$p" in "~"*) p="$HOME${p#"~"}" ;; esac
    printf '%s\n' "$p"
}

pki_from_path() {
    case "$1" in
        *.zip | *.ZIP) pki_from_zip "$1" ;;
        *) pki_in "$1" ;;
    esac
}

locate_pki() {
    local pki="" zip answer
    if [ -n "$PKI_ARG" ]; then
        pki="$(pki_in "$PKI_ARG" || true)"
        [ -n "$pki" ] || die "--pki: no $PKI_NAME directory at or under '$PKI_ARG'"
    elif [ -n "$APP_ARG" ]; then
        pki="$(pki_from_path "$APP_ARG" || true)"
        [ -n "$pki" ] || die "--app: no $PKI_NAME directory inside '$APP_ARG'"
    fi
    [ -n "$pki" ] || pki="$(pki_from_system)"

    if [ -z "$pki" ]; then
        # fd 3 carries the list so the prompt can still read the terminal on stdin.
        while IFS= read -r zip <&3; do
            [ -n "$zip" ] || continue
            info "Control Center is not installed, but found $(basename "$zip") in Downloads."
            if confirm "Expand it to a temporary folder to read the PKI?"; then
                pki="$(pki_from_zip "$zip" || true)"
                [ -n "$pki" ] && break
                warn "no $PKI_NAME directory inside $(basename "$zip")"
            fi
        done 3<<EOF
$(downloaded_zips)
EOF
    fi

    if [ -z "$pki" ] && [ -t 0 ]; then
        info "Could not find the Control Center PKI automatically."
        info "Drag the Control Center app (or its zip, or the $PKI_NAME folder) here"
        read -r -p "    and press Return, or just press Return to give up: " answer
        answer="$(clean_path "$answer")"
        [ -n "$answer" ] && pki="$(pki_from_path "$answer" || true)"
    fi

    [ -n "$pki" ] || die "Elgato Control Center not found. Its bundled PKI is required to mint a
       client certificate. Install Control Center from https://www.elgato.com/downloads
       and re-run, or point at it with --app <path> / --pki <dir>."
    pki_valid "$pki" || die "$pki is missing root.crt.pem, intermediate.crt.pem or intermediate.key.pem"
    PKI="$pki"
}

certs_present() {
    [ -s "$CERTS/client-chain.pem" ] && [ -s "$CERTS/client.key" ] && [ -s "$CERTS/root.crt.pem" ]
}

PKI=""
if certs_present && [ "$REMINT" = 0 ]; then
    step "Locating the Control Center PKI"
    info "Skipped: a client certificate is already minted."
else
    step "Locating the Control Center PKI"
    locate_pki
    info "Found $PKI"
fi

# ------------------------------------------------------- 3. app-support folder

step "Preparing $APP_DIR"
mkdir -p "$CERTS" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
chmod 700 "$APP_DIR" "$CERTS"

# ------------------------------------------------------ 4. client certificate

mint_cert() {
    local dir="$WORK/mint" passphrase=""
    mkdir -p "$dir"
    (
        umask 077 && cd "$dir" &&
            openssl ecparam -name prime256v1 -genkey -noout -out client.key &&
            openssl req -new -key client.key -out client.csr \
                -subj "/C=US/O=Elgato/CN=ElgatoLightsClient" &&
            printf 'basicConstraints=CA:FALSE\nkeyUsage=digitalSignature\nextendedKeyUsage=clientAuth\n' \
                >client_ext.cnf
    ) || die "could not generate the client key and signing request"

    # Sign with the intermediate CA key that ships in Control Center. The serial
    # file is kept in our temp dir so nothing is written into the app bundle.
    sign() {
        openssl x509 -req -in "$dir/client.csr" \
            -CA "$PKI/intermediate.crt.pem" -CAkey "$PKI/intermediate.key.pem" \
            -CAserial "$dir/intermediate.srl" -CAcreateserial \
            -days 3650 -sha256 -extfile "$dir/client_ext.cnf" \
            -out "$dir/client.crt.pem" "$@" </dev/null 2>"$dir/sign.err"
    }
    if grep -q ENCRYPTED "$PKI/intermediate.key.pem"; then
        [ -t 0 ] || die "the intermediate CA key is passphrase-protected; re-run in a terminal to enter it"
        read -r -s -p "    Passphrase for Control Center's intermediate CA key (not stored): " passphrase
        printf '\n'
        ELGATO_CA_PASSPHRASE="$passphrase" sign -passin env:ELGATO_CA_PASSPHRASE ||
            die "signing failed (wrong passphrase?): $(tail -n1 "$dir/sign.err")"
    else
        sign || die "signing the client certificate failed: $(tail -n1 "$dir/sign.err")"
    fi

    openssl verify -CAfile "$PKI/root.crt.pem" -untrusted "$PKI/intermediate.crt.pem" \
        "$dir/client.crt.pem" >/dev/null 2>&1 ||
        die "the minted certificate does not chain to the Elgato Lights Root CA"
    openssl x509 -in "$dir/client.crt.pem" -noout -text | grep -A1 "Extended Key Usage" |
        grep -q "TLS Web Client Authentication" ||
        die "the minted certificate is missing the clientAuth extended key usage"

    # Leaf + intermediate, so the light can build the path to its root.
    cat "$dir/client.crt.pem" "$PKI/intermediate.crt.pem" >"$dir/client-chain.pem"
    install -m 600 "$dir/client.key" "$CERTS/client.key"
    install -m 600 "$dir/client-chain.pem" "$CERTS/client-chain.pem"
    install -m 600 "$PKI/root.crt.pem" "$CERTS/root.crt.pem"
    rm -rf "$dir"
}

step "Minting the client certificate"
if [ -n "$PKI" ]; then
    mint_cert
    info "Minted a clientAuth certificate, valid until $(openssl x509 -in "$CERTS/client-chain.pem" -noout -enddate | cut -d= -f2)."
else
    chmod 600 "$CERTS/client.key" "$CERTS/client-chain.pem" "$CERTS/root.crt.pem"
    if openssl verify -CAfile "$CERTS/root.crt.pem" -untrusted "$CERTS/client-chain.pem" \
        "$CERTS/client-chain.pem" >/dev/null 2>&1; then
        info "Already minted (valid until $(openssl x509 -in "$CERTS/client-chain.pem" -noout -enddate | cut -d= -f2)); re-mint with --remint."
    else
        warn "the existing certificate no longer verifies; re-run with --remint"
    fi
fi

# ------------------------------------------------------- 5. virtualenv + code

step "Installing the daemon"
# Reuse the virtualenv only if it works and was built from the Python chosen above.
venv_current() {
    [ "$("$VENV/bin/python" -c '
import os, sys
import websockets.asyncio.client, aiohttp, zeroconf
print(os.path.realpath(sys.base_prefix))' 2>/dev/null)" = "$PYTHON_BASE" ]
}
pip_install() {
    if [ "$PYPI" = 1 ]; then
        info "Installing from public PyPI (--pypi); ignoring pip config and PIP_* settings."
        # PIP_CONFIG_FILE=/dev/null skips every pip.conf (global, user, venv);
        # --isolated drops PIP_* environment variables.
        PIP_CONFIG_FILE=/dev/null "$VENV/bin/python" -m pip install --isolated \
            --quiet --disable-pip-version-check \
            --index-url https://pypi.org/simple -r "$REPO/requirements.txt"
    else
        "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check \
            -r "$REPO/requirements.txt"
    fi
}
# After a failed install, say whether pip was pointed at a private index (a
# company mirror such as AWS CodeArtifact or Artifactory), the usual cause on
# managed machines. Credentials embedded in the URL are stripped before printing.
pip_explain() {
    [ "$PYPI" = 1 ] && {
        info "Public PyPI could not be reached. Check the internet connection, or"
        info "drop --pypi to use the package index pip is configured with."
        return
    }
    local indexes
    indexes="$( {
        "$VENV/bin/python" -m pip config list 2>/dev/null |
            sed -n "s/^[^=]*\.\(extra-\)\{0,1\}index-url='\(.*\)'\$/\2/p"
        printf '%s\n' "${PIP_INDEX_URL:-}" "${PIP_EXTRA_INDEX_URL:-}" | tr ' ' '\n'
    } | grep -v -e '^$' -e '^https://pypi\.org/simple/\{0,1\}$' |
        sed -e 's#://[^/@]*@#://#' | sort -u)"
    if [ -n "$indexes" ]; then
        printf '\n' >&2
        warn "pip is configured to use a package index other than public PyPI:"
        printf '%s\n' "$indexes" | sed 's/^/        /' >&2
        warn "A 401 or 403 from it usually means its credentials have expired (AWS"
        warn "CodeArtifact tokens last 12 hours). Either refresh them, e.g. with"
        warn "'aws codeartifact login --tool pip ...', and re-run, or re-run with"
        warn "--pypi to install websockets, aiohttp and zeroconf from public PyPI."
    else
        info "Check the internet connection, or re-run with --python <path>"
        info "pointing at another Python 3."
    fi
}
if venv_current; then
    info "Virtualenv is up to date."
else
    info "Creating the virtualenv and installing websockets, aiohttp, zeroconf..."
    rm -rf "$VENV"
    "$PYTHON" -m venv "$VENV" || die "could not create a virtualenv with $PYTHON"
    if ! pip_install; then
        pip_explain
        die "pip install failed (see above)."
    fi
fi
rm -rf "$APP_DIR/keylight_mk2"
mkdir -p "$APP_DIR/keylight_mk2" "$APP_DIR/bin"
cp "$REPO"/keylight_mk2/*.py "$APP_DIR/keylight_mk2/"
install -m 755 "$REPO/cli/keylight-mk2" "$APP_DIR/bin/keylight-mk2"
info "Installed the daemon and the 'keylight-mk2' CLI."

# ------------------------------------------------------------- 6. config.json

step "Configuring lights"
configure=("$VENV/bin/python" -u -m keylight_mk2.configure --config "$CONFIG" --certs "$CERTS")
for host in ${HOSTS[@]+"${HOSTS[@]}"}; do configure+=(--host "$host"); done
[ -n "$PORT_ARG" ] && configure+=(--port "$PORT_ARG")
[ "$REDISCOVER" = 1 ] && configure+=(--rediscover)
status=0
(cd "$APP_DIR" && "${configure[@]}" | sed 's/^/    /') || status=$?
case "$status" in
    0) ;;
    3) die "No MK.2 light found on the network. Check that it is powered on and on the same
       network as this Mac, then re-run. To skip discovery, pass its hostname or IP:
       ./install.sh --host elgato-key-light-air-mk-2-xxxx.local" ;;
    *) die "could not write $CONFIG" ;;
esac

cfg() {
    "$VENV/bin/python" -c '
import json, sys
value = json.load(open(sys.argv[1])).get(sys.argv[2])
print("" if value is None else value)' "$CONFIG" "$1"
}
PORT="$(cfg port)"
SECRET="$(cfg secret)"
API_HOST="$(cfg bind)"
case "$API_HOST" in "" | 0.0.0.0 | ::) API_HOST="127.0.0.1" ;; esac
BASE_URL="http://$API_HOST:$PORT"

# ------------------------------------------------------------------ 7. launchd

step "Starting the daemon (launchd)"
render() {
    # Escape for XML first, then for the sed replacement.
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/[\\&|]/\\&/g'
}
sed -e "s|{{PYTHON}}|$(render "$VENV/bin/python")|g" \
    -e "s|{{ENTRYPOINT}}|$(render "$APP_DIR/keylight_mk2/__main__.py")|g" \
    -e "s|{{CONFIG}}|$(render "$CONFIG")|g" \
    -e "s|{{APP_DIR}}|$(render "$APP_DIR")|g" \
    -e "s|{{LOG}}|$(render "$LOG_FILE")|g" \
    "$REPO/launchd/$LABEL.plist.template" >"$PLIST"
plutil -lint "$PLIST" >/dev/null || die "rendered an invalid plist at $PLIST"

DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
# bootout returns before the old job is fully gone; bootstrap fails until it is.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
    sleep 0.5
done
launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null ||
    { sleep 2 && launchctl bootstrap "$DOMAIN" "$PLIST"; } ||
    launchctl load -w "$PLIST" ||
    die "launchctl could not load $PLIST"
launchctl enable "$DOMAIN/$LABEL" || true
info "Loaded $LABEL (starts at login, restarts if it exits)."
info "Log: $LOG_FILE"

# --------------------------------------------------------------- 8. smoke test

api() {
    local method="$1" path="$2" args
    args=(-sS --max-time 20 -X "$method")
    [ -n "$SECRET" ] && args+=(-H "X-Bridge-Secret: $SECRET")
    curl "${args[@]}" "$BASE_URL$path"
}

field() {
    "$VENV/bin/python" -c '
import json, sys
try:
    value = json.load(sys.stdin).get(sys.argv[1])
except ValueError:
    value = None
print("" if value is None else int(value) if isinstance(value, bool) else value)' "$1"
}

smoke_fail() {
    printf '\n\033[31mSmoke test: FAIL\033[0m - %s\n' "$1" >&2
    if [ -s "$LOG_FILE" ]; then
        printf '\nLast lines of %s:\n' "$LOG_FILE" >&2
        tail -n 15 "$LOG_FILE" | sed 's/^/    /' >&2
    fi
    cat >&2 <<EOF

Things to check:
  - The light is powered on and on the same network as this Mac.
  - macOS may be blocking the daemon: System Settings > Privacy & Security >
    Local Network must have Python switched on.
  - Discovery picked the wrong device: re-run with --host <hostname-or-ip>.
See the Troubleshooting section of README.md.
EOF
    exit 1
}

# True when the daemon is refused a route to a light that this shell can reach.
# That combination is macOS Local Network privacy: it applies to programs started
# by launchd, but not to ones started from a terminal that already has access.
blocked_by_macos() {
    local host
    case "$1" in *"No route to host"*) ;; *) return 1 ;; esac
    host="$("$VENV/bin/python" -c '
import json, sys
print(json.load(open(sys.argv[1]))["lights"][0]["host"])' "$CONFIG")" || return 1
    nc -z -G 3 "$host" 9123 >/dev/null 2>&1
}

# switch <on|off> <expected on value> [note]: POST it and check the applied state.
switch() {
    local reply got
    reply="$(api POST "/$1" 2>&1)" || smoke_fail "POST /$1 failed: $reply"
    got="$(printf '%s' "$reply" | field on)"
    [ "$got" = "$2" ] || smoke_fail "POST /$1 should give on=$2 but the bridge answered: $reply"
    info "$(printf '%-9s' "POST /$1") -> on=$got${3:+ ($3)}"
}

if [ "$SMOKE" = 1 ]; then
    step "Smoke test"
    up=0
    for _ in $(seq 1 30); do
        if api GET /status >/dev/null 2>&1; then up=1 && break; fi
        sleep 1
    done
    [ "$up" = 1 ] || smoke_fail "the daemon did not start listening on $BASE_URL"

    reachable=0 waited=0 limit=30 blocked=0
    while [ "$waited" -lt "$limit" ]; do
        status_json="$(api GET /status 2>/dev/null || true)"
        if [ "$(printf '%s' "$status_json" | field reachable)" = 1 ]; then reachable=1 && break; fi
        if [ "$blocked" = 0 ] && [ "$waited" -ge 4 ] && blocked_by_macos "$status_json"; then
            blocked=1 limit=300
            cat <<'EOF'
    macOS is keeping the daemon off the local network (the light itself answers).
    Look for a prompt asking to let "Python" find devices on your local network
    and click Allow. If there is no prompt, switch Python on under
    System Settings > Privacy & Security > Local Network; this opens that page:
      open "x-apple.systempreferences:com.apple.preference.security?Privacy_LocalNetwork"
    Waiting up to 5 minutes for that...
EOF
            if [ "$ADHOC" = 1 ]; then
                info "(This Python is ad hoc signed, so macOS may refuse it even once allowed."
                info " If so, re-run with --python /usr/bin/python3.)"
            fi
        fi
        sleep 2
        waited=$((waited + 2))
    done
    if [ "$reachable" != 1 ] && [ "$blocked" = 1 ]; then
        smoke_fail "macOS has not allowed Python on the local network yet. Allow it (see above); the
       daemon connects by itself within 30 seconds, or re-run ./install.sh to test again."
    fi
    [ "$reachable" = 1 ] || smoke_fail "the daemon is running but cannot reach the light: $status_json"
    info "GET /status -> $status_json"
    was_on="$(printf '%s' "$status_json" | field on)"

    info "Cycling the light: on, off, on (watch it)..."
    switch on 1
    sleep 1.5
    switch off 0
    sleep 1.5
    switch on 1
    if [ "$was_on" = 0 ]; then
        sleep 1.5
        switch off 0 "it was off before the test, so it is off again"
    fi
    printf '\n\033[32mSmoke test: PASS\033[0m\n'
fi

# --------------------------------------------------------------- 9. next steps

cat <<EOF

Installed. The bridge is listening on $BASE_URL

  curl -s $BASE_URL/status
  curl -s -X POST $BASE_URL/on
  curl -s -X POST $BASE_URL/off
  curl -s -X POST $BASE_URL/toggle
  curl -s -X POST $BASE_URL/set -d '{"on":1,"brightness":40,"kelvin":4500}'

Command line:  "$APP_DIR/bin/keylight-mk2" status
               (to put it on your PATH: ln -s "$APP_DIR/bin/keylight-mk2" /usr/local/bin/keylight-mk2)
Shortcuts:     see shortcuts/README.md
Camera auto on/off (optional): see extras/README.md
Full API reference and troubleshooting: README.md
EOF
