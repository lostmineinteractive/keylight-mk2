"""A stand-in for an MK.2 light, with the firmware quirks the bridge must handle.

It speaks the same JSON-RPC over a mutual-TLS WebSocket, using a throwaway PKI
generated with the openssl CLI the same way install.sh mints the client cert.
"""

from __future__ import annotations

import json
import ssl
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from websockets.asyncio.server import ServerConnection, serve

CA_EXT = "basicConstraints=critical,CA:TRUE\nkeyUsage=keyCertSign,cRLSign\n"
SERVER_EXT = "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature\nextendedKeyUsage=serverAuth\n"
CLIENT_EXT = "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature\nextendedKeyUsage=clientAuth\n"

INFO = {
    "productName": "Elgato Key Light Air MK.2",
    "firmwareVersion": "1.1.11",
    "serialNumber": "FAKE00000001",
    "displayName": "",
    "wifi-info": {"ssid": "test", "frequencyMHz": 5000, "rssi": -30},
}


def _openssl(cwd: Path, *args: str) -> None:
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


def _issue(pki: Path, name: str, subject: str, ext: str, ca: str) -> None:
    (pki / f"{name}.cnf").write_text(ext)
    _openssl(pki, "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", f"{name}.key")
    _openssl(pki, "req", "-new", "-key", f"{name}.key", "-subj", subject, "-out", f"{name}.csr")
    _openssl(
        pki, "x509", "-req", "-in", f"{name}.csr", "-CA", f"{ca}.crt.pem", "-CAkey", f"{ca}.key",
        "-CAserial", f"{ca}.srl", "-CAcreateserial", "-days", "2", "-sha256",
        "-extfile", f"{name}.cnf", "-out", f"{name}.crt.pem",
    )  # fmt: skip


def make_pki(root: Path) -> Path:
    """Create root -> intermediate -> {server, client}; returns the client certs dir."""
    pki = root / "pki"
    certs = root / "certs"
    pki.mkdir()
    certs.mkdir()
    (pki / "root.cnf").write_text(CA_EXT)
    _openssl(pki, "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", "root.key")
    _openssl(pki, "req", "-new", "-key", "root.key", "-subj", "/CN=Test Root CA", "-out", "root.csr")
    _openssl(
        pki, "x509", "-req", "-in", "root.csr", "-signkey", "root.key", "-days", "2",
        "-sha256", "-extfile", "root.cnf", "-out", "root.crt.pem",
    )  # fmt: skip
    _issue(pki, "intermediate", "/CN=Test Intermediate CA", CA_EXT, "root")
    _issue(pki, "server", "/CN=ElgatoLightsServerLeaf", SERVER_EXT, "intermediate")
    _issue(pki, "client", "/C=US/O=Elgato/CN=ElgatoLightsClient", CLIENT_EXT, "intermediate")
    intermediate = (pki / "intermediate.crt.pem").read_text()
    (pki / "server-chain.pem").write_text((pki / "server.crt.pem").read_text() + intermediate)
    (certs / "client-chain.pem").write_text((pki / "client.crt.pem").read_text() + intermediate)
    (certs / "client.key").write_text((pki / "client.key").read_text())
    (certs / "root.crt.pem").write_text((pki / "root.crt.pem").read_text())
    return certs


class FakeLight:
    def __init__(self, root: Path):
        self.pki = root / "pki"
        self.state = {"on": 0, "brightness": 30, "temperature": 200}
        self.requests: List[Dict[str, Any]] = []
        self.rejected = 0  # how many requests were answered with an error
        self.mute = False  # stop answering on connections that are already open
        self.port = 0
        self._server = None
        self._connections: set = set()
        self._muted: set = set()

    async def start(self) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(self.pki / "server-chain.pem"), str(self.pki / "server.key"))
        ctx.load_verify_locations(cafile=str(self.pki / "root.crt.pem"))
        ctx.verify_mode = ssl.CERT_REQUIRED  # mutual TLS: no client cert, no connection
        ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        self._server = await serve(self._handle, "127.0.0.1", self.port, ssl=ctx)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def go_mute(self) -> None:
        """Simulate a socket that died silently: open connections stop answering."""
        self._muted |= self._connections

    async def push_state(self, **changes: int) -> None:
        """A change made by the Elgato app or the remote: update and notify."""
        self.state.update(changes)
        await self._notify("notification.elgato.lights", self._lights())

    async def heartbeat(self) -> None:
        await self._notify("notification.elgato.accessory-info", INFO)

    async def _notify(self, method: str, params: Any) -> None:
        for ws in list(self._connections):
            await ws.send(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}))

    def _lights(self) -> Dict[str, Any]:
        return {"numberOfLights": 1, "lights": [dict(self.state)]}

    async def _handle(self, ws: ServerConnection) -> None:
        if ws.request.path != "/":
            await ws.close()
            return
        self._connections.add(ws)
        try:
            async for raw in ws:
                request = json.loads(raw)
                self.requests.append(request)
                if ws in self._muted:
                    continue
                # Firmware quirk: a request with a non-string id gets no reply at all.
                if not isinstance(request.get("id"), str):
                    continue
                reply = {"jsonrpc": "2.0", "id": request["id"]}
                reply.update(self._answer(request.get("method"), request.get("params")))
                if "error" in reply:
                    self.rejected += 1
                await ws.send(json.dumps(reply))
        finally:
            self._connections.discard(ws)
            self._muted.discard(ws)

    def _answer(self, method: Any, params: Any) -> Dict[str, Any]:
        if method == "get.elgato.accessory-info":
            return {"result": INFO}
        if method == "get.elgato.lights":
            return {"result": self._lights()}
        if method == "put.elgato.lights":
            new = params["lights"][0]
            in_range = 143 <= new["temperature"] <= 344 and 0 <= new["brightness"] <= 100
            if not in_range:
                # Firmware quirk: range violations come back as "Parse error".
                return {"error": {"code": -32700, "message": "Parse error"}}
            self.state = {key: new[key] for key in ("on", "brightness", "temperature")}
            return {"result": self._lights()}
        # Firmware quirk: unknown methods are "Invalid Request", not -32601.
        return {"error": {"code": -32600, "message": "Invalid Request"}}
