# Optional: light on when the camera is on

Turns the light on with a call preset (40%, 4500 K) when any app starts using
the camera, and off when the camera stops. It is a separate, opt-in LaunchAgent
that simply calls the bridge's HTTP API.

**This is the most fragile part of the project.** macOS has no supported way
for a script to be told "the camera turned on", so this relies on either log
lines that Apple changes between releases (approach A) or a small compiled
helper (approach B). The bridge itself does not depend on any of it.

## Install

The bridge must already be installed (`../install.sh`).

```sh
./install-camera-trigger.sh            # approach A: unified-log watcher
./install-camera-trigger.sh --swift    # approach B: CoreMediaIO probe (needs swiftc)
./install-camera-trigger.sh --uninstall
```

Then start a video preview (Photo Booth, FaceTime) and watch the light. Activity
is logged to `~/Library/Logs/keylight-mk2-camera.log`.

## Approach A: the unified log (default, no compiler)

`camera-watcher.sh` runs `log stream` with a predicate and matches two
patterns: one for "camera on", one for "camera off". It ships three known
signals:

| Signal | What it is | Covers |
| --- | --- | --- |
| `sensor-indicators` (default) | Control Center's menu-bar camera indicator | Any camera, any app |
| `uvc` | Power state of USB (UVC) cameras | External USB cameras only |
| `avcapture` | `AVCaptureSession` start/stop notifications | Apps using AVFoundation; can also fire for microphone-only capture |

All three were checked against a real video call on macOS 27.0. Other macOS
releases are untested and Apple changes these log lines without notice, so
treat the default as a starting point rather than a guarantee.

### Calibrating

If the light does not react, find out what *your* macOS logs:

```sh
"$HOME/Library/Application Support/keylight-mk2-bridge/bin/camera-watcher.sh" calibrate
```

It records the candidate log lines while you turn the camera on and then off,
shows which signals tracked it, saves the first one that did to
`camera-watcher.conf`, and tells you how to restart the watcher.

If none of the known signals matched, it leaves the captured lines in two files
and prints the commands for saving your own predicate and patterns:

```sh
camera-watcher.sh set PREDICATE   'subsystem == "..." AND eventMessage CONTAINS "..."'
camera-watcher.sh set START_REGEX 'text that appears when the camera turns on'
camera-watcher.sh set STOP_REGEX  'text that appears when it turns off'
```

A line that matches `START_REGEX` means on; any other line that matches
`STOP_REGEX` means off. Both are extended regular expressions.

## Approach B: the CoreMediaIO probe (robust)

`camera-probe.swift` is a ~50-line helper that asks CoreMediaIO once a second
whether any camera is running in any process
(`kCMIODevicePropertyDeviceIsRunningSomewhere`). It does not open the camera,
needs no camera permission, and does not depend on log wording, so it is the
reliable long-term path. It needs the Swift compiler from the Xcode command
line tools (`xcode-select --install`).

`./install-camera-trigger.sh --swift` builds it and switches the watcher to it.
Re-run without `--swift` to go back to the log watcher.

## Settings

`~/Library/Application Support/keylight-mk2-bridge/camera-watcher.conf`, changed
with `camera-watcher.sh set KEY VALUE` (then restart the watcher):

| Key | Default | Meaning |
| --- | --- | --- |
| `ON_BODY` | `{"on":1,"brightness":40,"kelvin":4500}` | Body POSTed to `/set` when the camera turns on. Empty means plain `/on`, which keeps the light's last settings. |
| `OFF_DELAY` | `3` | Seconds the camera must stay off before the light turns off. Apps often stop and restart the camera within a second. |
| `BACKEND` | `log` | `log` (approach A) or `probe` (approach B). |
| `PREDICATE`, `START_REGEX`, `STOP_REGEX` | the `sensor-indicators` signal | What approach A watches for. |
| `BRIDGE_URL`, `BRIDGE_SECRET` | from the bridge's config | Where the bridge is. |

```sh
launchctl kickstart -k gui/$(id -u)/com.lostmineinteractive.keylight-mk2-camera   # restart the watcher
camera-watcher.sh test                                          # fire on, then off, without the camera
```

## Limits

- The watcher turns the light off when the camera stops even if you had turned
  it on yourself beforehand.
- It reacts to *any* camera use, including a quick Photo Booth session.
