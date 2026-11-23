#!/bin/bash
# Camera auto-trigger for keylight-mk2-bridge (optional, and the most fragile part).
#
# Watches for the camera turning on or off and tells the bridge: camera on ->
# light on with the call preset, camera off -> light off.
#
#   camera-watcher.sh run          watch and drive the light (what launchd runs)
#   camera-watcher.sh calibrate    find the log lines THIS macOS emits for the camera
#   camera-watcher.sh test         fire the on/off actions once, without the camera
#   camera-watcher.sh set KEY VAL  change a setting in camera-watcher.conf
#
# By default it tails the unified log. The lines macOS logs for camera use change
# between releases, so if `run` never reacts, run `calibrate`. See extras/README.md.

set -u

APP_DIR="${KEYLIGHT_MK2_BRIDGE_HOME:-$HOME/Library/Application Support/keylight-mk2-bridge}"
CONF="$APP_DIR/camera-watcher.conf"
LOG=/usr/bin/log

# Known camera signals, most general first. `calibrate` tries each of them.
#   sensor-indicators  the menu-bar camera indicator: any camera, any app. macOS logs
#                      it in two wordings ("[cam] App" and "cam:bundle.id"); both match.
#   uvc                USB (UVC) cameras only.
#   avcapture          apps that capture through AVCaptureSession.
# All three were checked against a real call on macOS 27.0; older releases are untested.
CANDIDATES=(sensor-indicators uvc avcapture)
PREDICATES=(
    'subsystem == "com.apple.controlcenter" AND category == "sensor-indicators" AND (eventMessage BEGINSWITH "Sorted active attributions" OR eventMessage BEGINSWITH "Active activity attributions changed")'
    'subsystem == "com.apple.UVCExtension" AND eventMessage CONTAINS "Post PowerLog"'
    'subsystem == "com.apple.cameracapture" AND (eventMessage CONTAINS "AVCaptureSessionDidStartRunningNotification" OR eventMessage CONTAINS "AVCaptureSessionDidStopRunningNotification")'
)
# A line matching the start pattern means "camera on"; any other line matching
# the stop pattern means "camera off". Extended regular expressions.
STARTS=(
    'attributions.*[^a-z]cam[]:]'
    'VDCAssistant_Power_State" = On'
    'AVCaptureSessionDidStartRunningNotification'
)
STOPS=(
    'attributions (from SystemStatus update|changed to)'
    'VDCAssistant_Power_State" = Off'
    'AVCaptureSessionDidStopRunningNotification'
)

# Settings; override any of them in camera-watcher.conf.
BRIDGE_URL="http://127.0.0.1:9124"
BRIDGE_SECRET=""
ON_BODY='{"on":1,"brightness":40,"kelvin":4500}' # the call preset; empty = plain /on
OFF_DELAY=3                                      # seconds the camera must stay off
BACKEND="log"                                    # "log", or "probe" for the Swift helper
PREDICATE="${PREDICATES[0]}"
START_REGEX="${STARTS[0]}"
STOP_REGEX="${STOPS[0]}"
# shellcheck source=/dev/null
[ -r "$CONF" ] && . "$CONF"

say() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

post() {
    local path="$1" body="${2-}" code args
    args=(-sS --max-time 20 -o /dev/null -w '%{http_code}' -X POST)
    [ -n "$BRIDGE_SECRET" ] && args+=(-H "X-Bridge-Secret: $BRIDGE_SECRET")
    [ -n "$body" ] && args+=(-d "$body")
    code="$(curl "${args[@]}" "$BRIDGE_URL$path" 2>/dev/null)" || code="no answer"
    say "POST $path -> $code"
}

camera_on() {
    say "camera on"
    if [ -n "$ON_BODY" ]; then post /set "$ON_BODY"; else post /on; fi
}

camera_off() {
    say "camera off"
    post /off
}

conf_set() {
    local key="$1" value="$2" tmp="$CONF.tmp"
    : >"$tmp"
    chmod 600 "$tmp"
    [ -f "$CONF" ] && grep -v "^$key=" "$CONF" >>"$tmp"
    printf '%s=%q\n' "$key" "$value" >>"$tmp"
    mv "$tmp" "$CONF"
}

# Run a line source in the background and interleave a "__tick__" line every
# second, so the reader can time out the off-delay (bash 3.2 cannot tell a read
# timeout from end-of-input). Ends when the source exits.
with_ticks() {
    "$@" &
    local pid=$!
    trap 'kill "$pid" 2>/dev/null' EXIT
    while kill -0 "$pid" 2>/dev/null; do
        sleep 1
        echo "__tick__"
    done
}

# Read lines, track the camera state, and fire on transitions. Turning off is
# delayed: apps often stop and restart the camera within a second.
react() {
    local state="off" off_at=0 line
    while IFS= read -r line; do
        case "$line" in
            __tick__) ;;
            "Filtering the log data"* | "Timestamp "*) continue ;;
            *)
                if [[ $line =~ $START_REGEX ]]; then
                    off_at=0
                    if [ "$state" = off ]; then
                        state="on"
                        camera_on
                    fi
                elif [[ $line =~ $STOP_REGEX ]]; then
                    [ "$state" = on ] && [ "$off_at" = 0 ] && off_at=$((SECONDS + OFF_DELAY))
                fi
                ;;
        esac
        if [ "$off_at" != 0 ] && [ "$SECONDS" -ge "$off_at" ]; then
            off_at=0
            state="off"
            camera_off
        fi
    done
}

run() {
    if [ "$BACKEND" = probe ]; then
        [ -x "$APP_DIR/bin/camera-probe" ] || {
            say "BACKEND=probe but $APP_DIR/bin/camera-probe is missing; run install-camera-trigger.sh --swift"
            exit 1
        }
        START_REGEX='^on$'
        STOP_REGEX='^off$'
        say "watching the camera with the CoreMediaIO probe"
        with_ticks "$APP_DIR/bin/camera-probe" | react
    else
        say "watching the unified log: $PREDICATE"
        with_ticks "$LOG" stream --style compact --predicate "$PREDICATE" | react
    fi
    say "camera source exited"
    exit 1 # let launchd restart us
}

pause() {
    printf '\n%s\n' "$1"
    read -r -p "Press Return when done. " _
}

calibrate() {
    local capture on_lines off_lines broad="" i pid mark0 mark1 mark2 starts stops last picked=""
    capture="$(mktemp -t keylight-mk2-camera-calibration)"
    on_lines="$capture.on"
    off_lines="$capture.off"
    for i in "${!CANDIDATES[@]}"; do
        broad="$broad${broad:+ OR }(${PREDICATES[$i]})"
    done

    echo "Calibration: this records the log lines macOS emits while you turn the camera"
    echo "on and off, then picks the signal that tracked it."
    "$LOG" stream --style compact --predicate "$broad" >"$capture" 2>&1 &
    pid=$!
    trap 'kill "$pid" 2>/dev/null' EXIT
    sleep 2
    kill -0 "$pid" 2>/dev/null || {
        echo "error: 'log stream' failed: $(cat "$capture")" >&2
        exit 1
    }

    pause "1. Make sure NOTHING is using the camera (quit Photo Booth, FaceTime, Zoom...)."
    mark0="$(wc -l <"$capture")"
    pause "2. Turn the camera ON (open Photo Booth or start a video preview) and wait 5 seconds."
    mark1="$(wc -l <"$capture")"
    pause "3. Turn the camera OFF (quit that app) and wait 5 seconds."
    sleep 1
    mark2="$(wc -l <"$capture")"
    kill "$pid" 2>/dev/null
    sed -n "$((mark0 + 1)),${mark1}p" "$capture" >"$on_lines"
    sed -n "$((mark1 + 1)),${mark2}p" "$capture" >"$off_lines"

    printf '\n%-20s %-14s %s\n' "signal" "on-lines seen" "off-lines seen"
    for i in "${!CANDIDATES[@]}"; do
        starts="$(grep -Ec -- "${STARTS[$i]}" "$on_lines")"
        stops="$(grep -Ev -- "${STARTS[$i]}" "$off_lines" | grep -Ec -- "${STOPS[$i]}")"
        # The camera ended up off, so the last line this signal produced must be an "off".
        last="$(grep -E -- "${STARTS[$i]}|${STOPS[$i]}" "$off_lines" | tail -n1)"
        if [ -n "$last" ] && [[ $last =~ ${STARTS[$i]} ]]; then stops=0; fi
        printf '%-20s %-14s %s\n' "${CANDIDATES[$i]}" "$starts" "$stops"
        if [ -z "$picked" ] && [ "$starts" -gt 0 ] && [ "$stops" -gt 0 ]; then picked="$i"; fi
    done

    if [ -n "$picked" ]; then
        conf_set PREDICATE "${PREDICATES[$picked]}"
        conf_set START_REGEX "${STARTS[$picked]}"
        conf_set STOP_REGEX "${STOPS[$picked]}"
        conf_set BACKEND log
        rm -f "$capture" "$on_lines" "$off_lines"
        echo
        echo "Using '${CANDIDATES[$picked]}'. Saved to $CONF"
        echo "Restart the watcher to apply:"
        echo "  launchctl kickstart -k gui/$(id -u)/com.lostmineinteractive.keylight-mk2-camera"
    else
        echo
        echo "None of the known signals tracked the camera on this macOS version."
        echo "The captured lines are in:"
        echo "  $on_lines   (while the camera turned on)"
        echo "  $off_lines  (while it turned off)"
        echo "If they are empty, look for your own signal with a broader stream, e.g."
        echo "  $LOG stream --style compact --predicate 'eventMessage CONTAINS[c] \"camera\"'"
        echo "then save a predicate and two patterns that match its on and off lines:"
        echo "  $0 set PREDICATE '<predicate>'"
        echo "  $0 set START_REGEX '<pattern for the on line>'"
        echo "  $0 set STOP_REGEX '<pattern for the off line>'"
        echo "or switch to the CoreMediaIO probe: extras/install-camera-trigger.sh --swift"
        exit 1
    fi
}

case "${1:-run}" in
    run) run ;;
    calibrate) calibrate ;;
    test)
        camera_on
        sleep 3
        camera_off
        ;;
    set)
        [ $# -eq 3 ] || {
            echo "usage: $0 set KEY VALUE" >&2
            exit 1
        }
        conf_set "$2" "$3"
        ;;
    *)
        sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
        exit 1
        ;;
esac
