# Controlling the light from Shortcuts

Shortcuts cannot speak the light's mutual-TLS WebSocket protocol, but it can
make HTTP requests, and the bridge accepts those on
`http://127.0.0.1:9124`. Every shortcut below is a single **Get Contents of
URL** action.

## The building block

1. Open **Shortcuts** and create a new shortcut.
2. Add the action **Get Contents of URL**.
3. Set the URL, then click **Show More** and set **Method** to **POST**.

| To do this | URL | Request Body |
| --- | --- | --- |
| Turn on | `http://127.0.0.1:9124/on` | none |
| Turn off | `http://127.0.0.1:9124/off` | none |
| Toggle | `http://127.0.0.1:9124/toggle` | none |
| Set brightness / colour | `http://127.0.0.1:9124/set` | JSON (below) |

For `/set`, choose **Request Body: JSON** and add the fields you want to change:

| Key | Type | Value |
| --- | --- | --- |
| `on` | Number | `1` or `0` |
| `brightness` | Number | 0 to 100 |
| `kelvin` | Number | 2900 to 7000 |

That sends, for example, `{"on":1,"brightness":40,"kelvin":4500}`. Values out
of range are clamped rather than rejected.

## Three ready-to-build shortcuts

**Light On**
- Get Contents of URL: `http://127.0.0.1:9124/on`, Method POST.

**Light Off**
- Get Contents of URL: `http://127.0.0.1:9124/off`, Method POST.

**Light: Call preset** (on, 40%, 4500 K)
- Get Contents of URL: `http://127.0.0.1:9124/set`, Method POST, Request Body
  JSON with `on` = 1, `brightness` = 40, `kelvin` = 4500 (all three as Number).

Give each a keyboard shortcut or pin it to the menu bar from the shortcut's
details panel (the ⓘ button).

## Reading the state

`GET http://127.0.0.1:9124/status` returns a dictionary, and so does every
POST. To branch on the current state:

1. **Get Contents of URL**: `http://127.0.0.1:9124/status` (Method GET).
2. **Get Dictionary Value**: Get **Value** for `on` in *Contents of URL*.
3. **If** *Dictionary Value* **is** `1` ... **Otherwise** ...

Useful keys at the top level: `on` (1 or 0), `brightness`, `kelvin`,
`temperature` (mireds) and `reachable`. With more than one light, each one's
state is under `lights`.

If the light is unplugged or off the network, the request fails with HTTP 503
and `{"ok": false, "error": "..."}` after about three seconds.

## More than one light

Address a single light by the name in `config.json` (or its serial number):

- `http://127.0.0.1:9124/light/key/on`
- `http://127.0.0.1:9124/light/key/set`

The plain `/on`, `/off`, `/toggle` and `/set` routes act on every light.

## Automations

Where your macOS version offers Shortcuts automations, they can run these when
an app opens or closes, for example *Light: Call preset* when Zoom opens. For
"on whenever the camera is in use, off when it stops", see
[../extras/README.md](../extras/README.md).

## Shortcuts on iPhone or iPad (opt-in)

`127.0.0.1` means "this device", so a shortcut on an iPhone cannot reach a
bridge that only listens on the Mac's localhost. To allow it, the bridge has to
listen on the Mac's LAN address:

1. Edit `~/Library/Application Support/keylight-mk2-bridge/config.json`:

   ```json
   {
     "bind": "0.0.0.0",
     "port": 9124,
     "secret": "choose-a-long-random-string",
     "lights": [ ... ]
   }
   ```

2. Restart the daemon:
   `launchctl kickstart -k gui/$(id -u)/com.lostmineinteractive.keylight-mk2-bridge`
3. In the iOS shortcut, use the Mac's address (for example
   `http://my-mac.local:9124/on`) and, under **Headers**, add
   `X-Bridge-Secret` with the same secret.

**Read this before you do it.** Bound to the LAN, the API is reachable by every
device on the network. Without a `secret` it has no authentication at all:
anyone on the Wi-Fi can switch your light. With a secret, requests without the
header are refused, but the traffic is plain HTTP, so the secret can be read by
anyone able to observe the network. Only enable this on a network you trust,
and never forward the port to the internet. macOS may also ask whether to allow
incoming connections for Python; allow it.

When a secret is set, Shortcuts on the Mac must send the `X-Bridge-Secret`
header too. The `keylight-mk2` command-line tool and the camera trigger read it from
the config automatically.
