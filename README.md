# keylight-mk2

Local control for the **Elgato Key Light Air MK.2** (and other MK.2-generation
lights) from macOS: Shortcuts, shell scripts, `curl`, anything that can make an
HTTP request to localhost.

```sh
curl -X POST http://127.0.0.1:9124/on
curl -X POST http://127.0.0.1:9124/set -d '{"brightness":40,"kelvin":4500}'
```

## Why this exists

Every earlier Key Light had a plain-HTTP REST API. The MK.2 dropped it: it
speaks JSON-RPC 2.0 over a **mutual-TLS WebSocket** on port 9123, which
Shortcuts and most tools cannot do. This project runs a small daemon that holds
that WebSocket open and exposes an ordinary HTTP API on `127.0.0.1`.

```
Shortcuts / curl / scripts ──HTTP──▶ bridge daemon (127.0.0.1:9124) ──mTLS WebSocket──▶ light (:9123)
```

Because the socket is kept warm, requests return in a few milliseconds, and the
daemon hears about changes made from the Elgato app or the remote as they
happen.

The original use case is switching the light on when a video call starts and
off when it ends ([extras/](extras/README.md)), but the bridge is a general
control API.

## Requirements

- macOS with `openssl` and Python 3.9 or newer. The installer uses Apple's
  Python (`/usr/bin/python3`, part of Xcode and the Command Line Tools:
  `xcode-select --install`) when it is there; see *Local Network* below for why.
- **Elgato Control Center** installed, or its downloaded zip in `~/Downloads`.
  The installer mints a client certificate from the PKI bundled inside that
  app; it is needed once, at install time.
- The light set up on the same network as the Mac.

## Install

```sh
./install.sh
```

The installer is idempotent; re-run it any time. It:

1. Finds the `pki_Key_Light_Air_Mk2` directory inside Control Center, wherever
   the app lives and whatever it is named.
2. Mints a client certificate signed by Elgato's intermediate CA into
   `~/Library/Application Support/keylight-mk2/certs/` (mode 600).
3. Creates a virtualenv there and installs `websockets`, `aiohttp`, `zeroconf`.
4. Discovers MK.2 lights over mDNS and writes `config.json`.
5. Installs a LaunchAgent (`com.lostmineinteractive.keylight-mk2`) so the daemon runs at
   login and restarts if it exits.
6. Runs a smoke test that cycles the light on, off, on, and prints PASS or FAIL.

Options:

| Option | Purpose |
| --- | --- |
| `--host <name>` | Light hostname or IP; skips discovery. Repeat for several lights. |
| `--app <path>` / `--pki <dir>` | Where Control Center (or its PKI directory) is, if the search misses it. |
| `--port <n>` | HTTP port (default 9124). |
| `--python <path>` | Interpreter for the virtualenv (default: Apple's `/usr/bin/python3`, else `python3` on `PATH`). |
| `--rediscover` | Replace the lights in an existing `config.json`. |
| `--remint` | Mint a fresh certificate even if one exists. |
| `--no-smoke-test` | Skip the light cycle. |
| `-y`, `--yes` | Answer yes to prompts (expanding a downloaded zip). |

To remove it: `./uninstall.sh` (keeps the minted certificates) or
`./uninstall.sh --purge` (removes everything).

### Local Network permission

macOS (15 and later) keeps any program started by launchd off the local network
until you allow it, even though the same connection works from Terminal. The
first time the daemon starts, macOS asks whether "Python" may find devices on
your local network. Click **Allow**; until you do, the daemon gets `No route to
host` and the installer waits at the smoke test. The switch lives in **System
Settings > Privacy & Security > Local Network**.

This is why the installer prefers Apple's Python. macOS tracks the permission
by code signature, and Apple's Python is properly signed, so the permission
holds. Homebrew's Python is only ad hoc signed: in testing (macOS 27, Homebrew
Python 3.15) it stayed blocked under launchd even with Python switched on in
Settings. If you pass `--python` with such a build and the smoke test fails
this way, re-run with `--python /usr/bin/python3`.

## HTTP API

JSON in, JSON out, on `http://127.0.0.1:9124` by default.

| Request | Effect |
| --- | --- |
| `GET /status` | Current state of every light. |
| `POST /on` | Turn on, at the brightness and temperature the light last had. |
| `POST /off` | Turn off. |
| `POST /toggle` | Off if any light is on, otherwise on. |
| `POST /set` | Set any of `on`, `brightness`, `kelvin` or `temperature`. |
| `GET /light/<name-or-serial>/status` | One light. |
| `POST /light/<name-or-serial>/{on,off,toggle,set}` | One light. |

`POST /set` takes a JSON body with any of:

| Field | Values |
| --- | --- |
| `on` | `true`/`false` or `1`/`0` |
| `brightness` | 0 to 100 |
| `kelvin` | 2900 to 7000 |
| `temperature` | 143 to 344, raw mireds; use this *or* `kelvin` |
| `light` | optional: name or serial of one light (default: all) |

The body is parsed as JSON whatever the `Content-Type`, so plain `curl -d`
works. The same fields are accepted as query parameters
(`POST /set?brightness=40`).

Every response reports the applied state:

```json
{
  "ok": true,
  "on": 1,
  "brightness": 25,
  "kelvin": 5000,
  "temperature": 200,
  "reachable": true,
  "lights": [
    {
      "name": "key",
      "serial": "ACSGBxxxxxxxx",
      "host": "elgato-key-light-air-mk-2-xxxx.local",
      "reachable": true,
      "on": 1,
      "brightness": 25,
      "kelvin": 5000,
      "temperature": 200,
      "heartbeat_age": 4.2,
      "rssi": -30
    }
  ]
}
```

- The top-level `on`, `brightness`, `kelvin`, `temperature` and `reachable`
  mirror the first light, so with a single light you never need to look inside
  `lights`. (With several lights, `on` is 1 if any is on and `reachable` is
  true only if all are.) The `/light/<name>/...` routes return one light's
  fields at the top level instead.
- `heartbeat_age` is the number of seconds since the light was last heard from.
  The light sends a heartbeat about every 30 seconds.
- `kelvin` is derived from the mired value and rounded to the nearest 50 K, the
  steps Control Center shows. `temperature` is the exact value on the device.

Behaviour worth knowing:

- **Values are clamped, never rejected.** `{"kelvin":100000,"brightness":500}`
  applies 7000 K at 100% and the response shows the clamped values.
- **Colour temperature is in mireds on the device** (`1,000,000 / kelvin`), and
  a *higher* mired value is a *warmer* light: 143 is the coolest (7000 K) and
  344 the warmest (2900 K).
- **Errors** are `{"ok": false, "error": "..."}` with a matching status:
  `400` bad input, `404` unknown light or route, `503` light unreachable (the
  request waits about 3 seconds for a reconnect, then gives up rather than
  hanging), `502` the light rejected the request.

## Command line

`keylight-mk2` is a thin wrapper over the API, installed to
`~/Library/Application Support/keylight-mk2/bin/keylight-mk2`:

```sh
keylight-mk2 status
keylight-mk2 on | off | toggle
keylight-mk2 set -b 40 -k 4500 --on
keylight-mk2 brightness 60
keylight-mk2 kelvin 3200
keylight-mk2 -l key off        # one light, by name or serial
```

To put it on your `PATH`:

```sh
ln -s "$HOME/Library/Application Support/keylight-mk2/bin/keylight-mk2" /usr/local/bin/keylight-mk2
```

## Shortcuts and automation

- [shortcuts/README.md](shortcuts/README.md): wiring macOS and iOS Shortcuts to
  the API, with three ready-to-build shortcuts.
- [extras/README.md](extras/README.md): optional auto on/off when the camera
  starts and stops.

## Configuration

`~/Library/Application Support/keylight-mk2/config.json`:

```json
{
  "bind": "127.0.0.1",
  "port": 9124,
  "lights": [
    {"name": "key", "host": "elgato-key-light-air-mk-2-xxxx.local", "serial": "ACSGBxxxxxxxx"}
  ]
}
```

| Key | Meaning |
| --- | --- |
| `bind` | Address the HTTP API listens on. Keep `127.0.0.1` unless you need LAN access (see Security). |
| `port` | HTTP port. |
| `secret` | Optional. If set, every request must carry it in an `X-Bridge-Secret` header. |
| `lights` | One entry per light. `name` is what you use in `/light/<name>/...`. |

The light is addressed by its `.local` hostname, so it survives DHCP changes;
the daemon also remembers the last IP that worked and falls back to it when the
name does not resolve. After editing the config, restart the daemon:

```sh
launchctl kickstart -k gui/$(id -u)/com.lostmineinteractive.keylight-mk2
```

## Troubleshooting

The daemon logs to `~/Library/Logs/keylight-mk2.log`.

**`/status` shows `"reachable": false`.** The `error` field says why.
- *cannot resolve ...* / *timed out* / *Connect call failed*: the light is off,
  on another network, or the hostname is wrong. Check with
  `ping elgato-key-light-air-mk-2-xxxx.local`, or re-run
  `./install.sh --rediscover`.
- *No route to host* while `ping` from Terminal works: macOS is blocking the
  daemon from the local network. Open **System Settings > Privacy & Security >
  Local Network** and switch Python on; the daemon reconnects by itself within
  30 seconds. If it is already on and still blocked, the daemon is probably
  running an ad hoc signed Python (see *Local Network permission* above):
  re-run `./install.sh --python /usr/bin/python3`.
- *certificate verification failed* or a TLS alert: the client certificate was
  not accepted. Re-mint it with `./install.sh --remint`.

**The installer cannot find Control Center.** Pass `--app /path/to/the.app` or
`--pki /path/to/pki_Key_Light_Air_Mk2`. A downloaded
`ElgatoControlCenter-*.zip` in `~/Downloads` is also detected.

**Discovery finds nothing.** Pass the hostname or IP: `./install.sh --host
192.168.1.50`. Lights without the `tls=CERT_1_VER_EN` mDNS marker use the old
plain-HTTP API and are not supported by this project.

**The daemon stopped working after a macOS, Xcode or Python update.** The
virtualenv is tied to the Python it was built from. Re-run `./install.sh`; it
rebuilds the virtualenv when needed and keeps your certificates and config.

**See what the daemon is doing:**

```sh
tail -f ~/Library/Logs/keylight-mk2.log
launchctl print gui/$(id -u)/com.lostmineinteractive.keylight-mk2 | head -20
```

## How it works

The MK.2 protocol is undocumented; the facts below were established against
real hardware (firmware 1.1.11) and match the independent
[ha-elgato-mk2](https://github.com/McMoobud/ha-elgato-mk2) integration (see
[Acknowledgements](#acknowledgements)).

- **Transport:** `wss://<host>:9123/` (the path must be `/`), TLS 1.2 only, with
  a client certificate. The light's server certificate chains to the *Elgato
  Lights Root CA*; the client certificate must too. The daemon verifies the
  light against that root but skips hostname checking, because the light's
  certificate names `ElgatoLightsServerLeaf`, not a hostname.
- **Envelope:** JSON-RPC 2.0, one object per text frame. The request `id` must
  be a **string**: the light silently ignores requests with an integer id.
- **Methods:** `get.elgato.accessory-info`, `get.elgato.lights`, and
  `put.elgato.lights` with
  `{"numberOfLights":1,"lights":[{"on":1,"brightness":30,"temperature":200}]}`.
- **Notifications:** the light pushes `notification.elgato.accessory-info`
  about every 30 seconds (the heartbeat) and `notification.elgato.lights`
  whenever its state changes, including changes from the Elgato app or remote.
- **Error quirks:** an out-of-range value comes back as `-32700 "Parse error"`,
  and an unknown method as `-32600 "Invalid Request"`. The bridge clamps values
  so the first never happens, and reports it as "value out of range" if it does.
- **Discovery:** mDNS service `_elg._tcp`; MK.2 lights carry the TXT record
  `tls=CERT_1_VER_EN`.

The daemon keeps one socket per light. If the socket drops, or the heartbeat
goes quiet for 45 seconds and a direct query gets no answer, it reconnects with
exponential backoff (1 to 30 seconds). An API request that arrives while
disconnected triggers an immediate reconnect attempt.

## Security notes

- **The client certificate is not strong authentication.** The intermediate CA
  private key ships inside Control Center; that is Elgato's design, and it
  means any machine with the app can mint a certificate the light trusts. The
  mutual TLS keeps out clients that lack the app, nothing more. Treat the
  minted `client.key` as a secret anyway: it is stored with mode 600, and
  `.gitignore` excludes certificates, keys and `config.json`.
- **The bridge listens on localhost only** by default, so only software on this
  Mac can reach it. Requests carrying an `Origin` header are refused, so a web
  page open in your browser cannot drive the light.
- **LAN access is opt-in.** Setting `bind` to the Mac's LAN address exposes the
  API to everyone on the network with no authentication. If you do that, also
  set a `secret` and only do it on a network you trust. The connection is plain
  HTTP, so the secret is visible to anyone who can observe that network.
- **The certificate is valid for 10 years.** If Control Center ever rotates its
  root or intermediate CA, re-mint: `./install.sh --remint` (or delete the
  `certs/` directory and re-run the installer).
- This software only talks to the lights listed in your own `config.json`.

## Development

```sh
VENV="$HOME/Library/Application Support/keylight-mk2/venv"
"$VENV/bin/python" -m unittest discover -s tests -t .   # tests run against a fake light
"$VENV/bin/python" -m keylight_mk2 --config /path/to/config.json -v   # run in the foreground
"$VENV/bin/python" -m keylight_mk2.discover                   # list MK.2 lights on the network
```

Layout: `keylight_mk2/` is the daemon (`light.py` holds the WebSocket and protocol,
`api.py` the HTTP routes), `cli/keylight-mk2` the command-line wrapper,
`launchd/` the LaunchAgent template, `extras/` the camera trigger, `tests/`
the test suite and its fake light.

## Acknowledgements

[ha-elgato-mk2](https://github.com/McMoobud/ha-elgato-mk2) by McMoobud, a Home
Assistant integration, published the first public teardown of the MK.2
protocol that we know of (JSON-RPC 2.0 over a mutual-TLS WebSocket on port
9123). This project is an independent implementation and shares no code with
it, but its findings were a useful reference and agree with what we measured.

## License

[MIT](LICENSE). Copyright (c) 2026 Lost Mine Interactive LLC.

Not affiliated with or endorsed by Elgato or Corsair. Elgato and Key Light are
trademarks of Corsair Memory, Inc.
