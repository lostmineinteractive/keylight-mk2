"""Persistent mutual-TLS WebSocket connection to one Elgato MK.2 light.

Protocol facts (reverse-engineered against firmware 1.1.11, see README):
  * wss://<host>:9123/ with TLS 1.2 and a client certificate.
  * JSON-RPC 2.0, one object per text frame. The request id MUST be a string;
    the light silently drops requests with an integer id.
  * The light pushes notifications (no id) at any time: a heartbeat roughly
    every 30s and a state update whenever anything changes the light.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import random
import socket
import ssl
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from .convert import clamp_brightness, clamp_mired, mired_to_kelvin

log = logging.getLogger(__name__)

PORT = 9123


class LightError(Exception):
    """Base class for anything that goes wrong talking to a light."""


class LightUnreachable(LightError):
    """No usable connection to the light."""


class LightRangeError(LightError):
    """The light rejected a value as out of range."""


class LightRPCError(LightError):
    """The light answered a request with a JSON-RPC error."""


def build_ssl_context(certs: Path, legacy_ciphers: bool = False) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_cert_chain(certfile=str(certs / "client-chain.pem"), keyfile=str(certs / "client.key"))
    ctx.load_verify_locations(cafile=str(certs / "root.crt.pem"))
    # The root CA authenticates the light, but its leaf CN is the fixed string
    # "ElgatoLightsServerLeaf", never a hostname.
    ctx.check_hostname = False
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    if legacy_ciphers:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    return ctx


async def _connect(
    host: str, ip: Optional[str], port: int, ctx: ssl.SSLContext, timeout: float
) -> ClientConnection:
    uri_host = f"[{host}]" if ":" in host else host
    return await connect(
        f"wss://{uri_host}:{port}/",  # the path must be exactly "/"
        ssl=ctx,
        host=ip or host,
        port=port,
        open_timeout=timeout,
        close_timeout=1,
        # Liveness comes from the light's own heartbeat, not WebSocket pings.
        ping_interval=None,
        compression=None,
        proxy=None,
        max_size=1 << 20,
    )


async def open_light(
    host: str,
    certs: Path,
    *,
    ip: Optional[str] = None,
    port: int = PORT,
    legacy: bool = False,
    timeout: float = 8.0,
) -> Tuple[ClientConnection, bool]:
    """Open the WebSocket. Returns it with whether legacy ciphers were needed."""
    try:
        return await _connect(host, ip, port, build_ssl_context(certs, legacy), timeout), legacy
    except ssl.SSLCertVerificationError:
        raise
    except ssl.SSLError as exc:
        if legacy:
            raise
        # The light only offers old TLS 1.2 suites, which some OpenSSL builds
        # refuse at their default security level.
        try:
            ctx = build_ssl_context(certs, legacy_ciphers=True)
        except ssl.SSLError:
            raise exc from None
        log.info("TLS handshake with %s failed (%s); retrying with legacy ciphers", host, exc)
        return await _connect(host, ip, port, ctx, timeout), True


class IPCache:
    """Last-known IP per hostname, used when .local name resolution fails."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path
        self._ips: Dict[str, str] = {}
        if path is not None:
            try:
                saved = json.loads(path.read_text()).get("last_ip", {})
                self._ips = {str(k): str(v) for k, v in saved.items()}
            except (OSError, ValueError, AttributeError):
                pass

    def get(self, host: str) -> Optional[str]:
        return self._ips.get(host)

    def put(self, host: str, ip: str) -> None:
        if self._ips.get(host) == ip:
            return
        self._ips[host] = ip
        if self.path is None:
            return
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"last_ip": self._ips}, indent=2) + "\n")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.debug("could not save %s: %s", self.path, exc)


class Light:
    """One light: holds the socket open, tracks state, reconnects on its own."""

    port = PORT
    rpc_timeout = 3.0
    open_timeout = 8.0
    resolve_timeout = 5.0
    connect_wait = 3.0  # how long an API call waits for a reconnect before 503
    heartbeat_timeout = 45.0  # the light heartbeats roughly every 30s
    watchdog_interval = 10.0
    backoff_min = 1.0
    backoff_max = 30.0
    stable_after = 10.0  # a session this long resets the backoff

    def __init__(
        self,
        name: str,
        host: str,
        serial: str = "",
        *,
        certs: Path,
        ip_cache: Optional[IPCache] = None,
    ):
        self.name = name
        self.host = host
        self.serial = serial
        self.certs = certs
        self.info: Dict[str, Any] = {}
        self.lights: List[Dict[str, int]] = []
        self.last_seen: Optional[float] = None
        self.last_error: Optional[str] = "not connected yet"
        self._ip_cache = ip_cache or IPCache()
        self._legacy = False
        self._ws: Optional[ClientConnection] = None
        self._reader: Optional[asyncio.Task] = None
        self._task: Optional[asyncio.Task] = None
        self._pending: Dict[str, asyncio.Future] = {}
        self._connected = asyncio.Event()
        self._retry_now = asyncio.Event()
        self._write_lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"light-{self.name}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    @property
    def reachable(self) -> bool:
        return self._connected.is_set()

    async def _run(self) -> None:
        delay = 0.0
        failures = 0
        while True:
            started = time.monotonic()
            stable = False
            try:
                await self._session()
                stable = time.monotonic() - started >= self.stable_after
                self.last_error = "connection lost"
                log.warning("%s: connection lost, reconnecting", self.name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # any failure here just means "try again"
                failures += 1
                self.last_error = _describe(exc)
                # Only the first failure of an outage is worth a log line.
                log.log(
                    logging.WARNING if failures == 1 else logging.DEBUG,
                    "%s: cannot connect to %s (%s); retrying",
                    self.name,
                    self.host,
                    self.last_error,
                )
            if stable:
                delay, failures = 0.0, 0
            else:
                delay = min(self.backoff_max, max(self.backoff_min, delay * 2))
            await self._backoff(delay)

    async def _backoff(self, delay: float) -> None:
        if delay > 0:
            try:
                await asyncio.wait_for(self._retry_now.wait(), delay * random.uniform(0.8, 1.2))
            except asyncio.TimeoutError:
                pass
        self._retry_now.clear()

    async def _session(self) -> None:
        """One connection's lifetime. Returns when an established link ends."""
        ip = await self._resolve()
        ws, self._legacy = await open_light(
            self.host,
            self.certs,
            ip=ip,
            port=self.port,
            legacy=self._legacy,
            timeout=self.open_timeout,
        )
        reader = asyncio.create_task(self._read(ws))
        try:
            self._apply_info(await self._rpc(ws, "get.elgato.accessory-info"))
            self._apply_lights(await self._rpc(ws, "get.elgato.lights"))
            self._ws, self._reader = ws, reader
            self.last_error = None
            self._connected.set()
            log.info(
                "%s: connected to %s (%s, firmware %s)",
                self.name,
                self.host,
                self.info.get("productName", "unknown product"),
                self.info.get("firmwareVersion", "?"),
            )
            await self._supervise(ws, reader)
        finally:
            self._connected.clear()
            self._ws = self._reader = None
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(LightUnreachable(f"{self.name}: connection lost"))
            try:
                await asyncio.wait_for(ws.close(), 2)
            except Exception:
                pass

    async def _resolve(self) -> Optional[str]:
        """IPv4 address to dial, or None to let the socket layer resolve."""
        try:
            ipaddress.ip_address(self.host)
            return None
        except ValueError:
            pass
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(self.host, self.port, family=socket.AF_INET, type=socket.SOCK_STREAM),
                self.resolve_timeout,
            )
            ip = infos[0][4][0]
        except (OSError, asyncio.TimeoutError, IndexError):
            cached = self._ip_cache.get(self.host)
            if cached is None:
                raise LightUnreachable(f"cannot resolve {self.host}") from None
            log.debug("%s: cannot resolve %s, trying last-known IP %s", self.name, self.host, cached)
            return cached
        self._ip_cache.put(self.host, ip)
        return ip

    async def _supervise(self, ws: ClientConnection, reader: asyncio.Task) -> None:
        while True:
            done, _ = await asyncio.wait({reader}, timeout=self.watchdog_interval)
            if done:
                return
            if time.monotonic() - (self.last_seen or 0) > self.heartbeat_timeout:
                # Heartbeat is overdue: ask directly before declaring the socket dead.
                try:
                    self._apply_info(await self._rpc(ws, "get.elgato.accessory-info"))
                except LightError:
                    log.warning("%s: no heartbeat from the light", self.name)
                    return

    async def _read(self, ws: ClientConnection) -> None:
        try:
            async for raw in ws:
                self.last_seen = time.monotonic()
                try:
                    frame = json.loads(raw)
                except ValueError:
                    log.debug("%s: ignoring non-JSON frame", self.name)
                    continue
                if not isinstance(frame, dict):
                    continue
                frame_id = frame.get("id")
                future = self._pending.get(frame_id) if isinstance(frame_id, str) else None
                if future is not None:
                    if not future.done():
                        future.set_result(frame)
                elif "method" in frame:
                    self._notify(frame["method"], frame.get("params"))
        except ConnectionClosed:
            pass

    def _notify(self, method: str, params: Any) -> None:
        if method == "notification.elgato.accessory-info":
            self._apply_info(params)
        elif method == "notification.elgato.lights":
            self._apply_lights(params)
            log.debug("%s: state changed externally: %s", self.name, self.lights)
        else:
            log.debug("%s: ignoring notification %s", self.name, method)

    def _apply_info(self, payload: Any) -> None:
        if isinstance(payload, dict) and payload:
            self.info = payload
            self.serial = str(payload.get("serialNumber") or self.serial)

    def _apply_lights(self, payload: Any) -> bool:
        entries = payload.get("lights") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or not entries:
            log.debug("%s: unexpected lights payload: %r", self.name, payload)
            return False
        lights = []
        for index, entry in enumerate(entries):
            previous = self.lights[index] if index < len(self.lights) else {}
            merged = {**previous, **entry} if isinstance(entry, dict) else previous
            try:
                lights.append({key: int(merged[key]) for key in ("on", "brightness", "temperature")})
            except (KeyError, TypeError, ValueError):
                log.debug("%s: unexpected light entry: %r", self.name, entry)
                return False
        self.lights = lights
        return True

    # -- JSON-RPC ----------------------------------------------------------

    async def _rpc(self, ws: ClientConnection, method: str, params: Any = None) -> Any:
        # The id must be a string: the light silently drops integer ids.
        request_id = str(uuid.uuid4())
        message: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await ws.send(json.dumps(message))
            frame = await asyncio.wait_for(future, self.rpc_timeout)
        except asyncio.TimeoutError:
            raise LightUnreachable(
                f"{self.name}: no reply to {method} within {self.rpc_timeout:g}s"
            ) from None
        except ConnectionClosed:
            raise LightUnreachable(f"{self.name}: connection lost") from None
        finally:
            self._pending.pop(request_id, None)
        error = frame.get("error")
        if error is not None:
            raise self._rpc_error(method, error)
        return frame.get("result")

    def _rpc_error(self, method: str, error: Any) -> LightError:
        code = error.get("code") if isinstance(error, dict) else None
        log.warning("%s: %s failed: %s", self.name, method, error)
        if code == -32700:
            # The firmware answers a range violation with "Parse error".
            return LightRangeError("value out of range")
        if code == -32600:
            # ...and an unknown method with "Invalid Request", not -32601.
            return LightRPCError(f"the light rejected {method}: unknown method or malformed request")
        return LightRPCError(f"the light rejected {method}: {error}")

    async def ensure_connected(self, timeout: Optional[float] = None) -> ClientConnection:
        """Return the live socket, nudging a reconnect and waiting briefly if needed."""
        if not self._connected.is_set():
            self._retry_now.set()
            try:
                await asyncio.wait_for(
                    self._connected.wait(), self.connect_wait if timeout is None else timeout
                )
            except asyncio.TimeoutError:
                pass
        ws = self._ws
        if ws is None or not self._connected.is_set():
            reason = f" ({self.last_error})" if self.last_error else ""
            raise LightUnreachable(f"light {self.name!r} at {self.host} is unreachable{reason}")
        return ws

    async def call(self, method: str, params: Any = None) -> Any:
        """One request on the persistent socket, with one retry on a fresh socket."""
        ws = await self.ensure_connected()
        try:
            return await self._rpc(ws, method, params)
        except LightUnreachable:
            # Typically a socket that died silently (e.g. the Mac just woke up).
            self._drop(ws)
        ws = await self.ensure_connected()
        return await self._rpc(ws, method, params)

    def _drop(self, ws: ClientConnection) -> None:
        if ws is self._ws and self._reader is not None:
            self._connected.clear()
            self._reader.cancel()  # ends _supervise, which tears the session down

    # -- state -------------------------------------------------------------

    @property
    def is_on(self) -> bool:
        return any(entry["on"] for entry in self.lights)

    async def apply(
        self,
        on: Optional[int] = None,
        brightness: Optional[float] = None,
        temperature: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Change any of on/brightness/temperature(mired); returns the new status.

        The full state is always sent, so turning the light on restores the
        brightness and temperature it last had.
        """
        async with self._write_lock:
            await self.ensure_connected()
            lights = []
            for current in self.lights or [{"on": 0, "brightness": 50, "temperature": 200}]:
                lights.append(
                    {
                        "on": current["on"] if on is None else int(bool(on)),
                        "brightness": clamp_brightness(
                            current["brightness"] if brightness is None else brightness
                        ),
                        "temperature": clamp_mired(
                            current["temperature"] if temperature is None else temperature
                        ),
                    }
                )
            result = await self.call(
                "put.elgato.lights", {"numberOfLights": len(lights), "lights": lights}
            )
            if not self._apply_lights(result):
                self.lights = lights
            return self.status()

    def status(self) -> Dict[str, Any]:
        first = self.lights[0] if self.lights else {}
        mired = first.get("temperature")
        status: Dict[str, Any] = {
            "name": self.name,
            "serial": self.serial,
            "host": self.host,
            "reachable": self.reachable,
            "on": int(self.is_on) if self.lights else None,
            "brightness": first.get("brightness"),
            "kelvin": mired_to_kelvin(mired) if mired else None,
            "temperature": mired,
            "heartbeat_age": (
                round(time.monotonic() - self.last_seen, 1) if self.last_seen is not None else None
            ),
            "rssi": (self.info.get("wifi-info") or {}).get("rssi"),
        }
        if not self.reachable and self.last_error:
            status["error"] = self.last_error
        return status


def _describe(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "timed out"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f"certificate verification failed: {exc.verify_message}"
    return str(exc) or type(exc).__name__
