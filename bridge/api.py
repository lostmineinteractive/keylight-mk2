"""The localhost HTTP API: JSON in, JSON out."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl

from aiohttp import web

from .convert import clamp_brightness, clamp_mired, kelvin_to_mired
from .light import Light, LightError, LightRangeError, LightUnreachable

log = logging.getLogger(__name__)

SECRET_HEADER = "X-Bridge-Secret"
SET_FIELDS = ("on", "brightness", "kelvin", "temperature")
SET_EXAMPLE = '{"on":1,"brightness":40,"kelvin":4500}'
_TRUE = {"1", "true", "on", "yes"}
_FALSE = {"0", "false", "off", "no"}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _error(status: int, message: str, **extra: Any) -> web.Response:
    return web.json_response({"ok": False, "error": message, **extra}, status=status)


def _number(data: Dict[str, Any], key: str) -> float:
    value = data[key]
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            raise ApiError(400, f"{key} must be a number") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(400, f"{key} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ApiError(400, f"{key} must be a finite number")
    return value


def _on_off(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)) and value in (0, 1):
        return int(value)
    if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
        return int(value.strip().lower() in _TRUE)
    raise ApiError(400, "on must be true/false or 1/0")


def parse_settings(data: Dict[str, Any]) -> Dict[str, int]:
    """Validate a /set body into clamped Light.apply() arguments."""
    unknown = sorted(set(data) - set(SET_FIELDS))
    if unknown:
        raise ApiError(400, f"unknown field(s): {', '.join(unknown)}; use {', '.join(SET_FIELDS)}")
    if "kelvin" in data and "temperature" in data:
        raise ApiError(400, "send kelvin or temperature (mired), not both")
    settings: Dict[str, int] = {}
    if "on" in data:
        settings["on"] = _on_off(data["on"])
    if "brightness" in data:
        settings["brightness"] = clamp_brightness(_number(data, "brightness"))
    if "kelvin" in data:
        settings["temperature"] = kelvin_to_mired(_number(data, "kelvin"))
    if "temperature" in data:
        settings["temperature"] = clamp_mired(_number(data, "temperature"))
    if not settings:
        raise ApiError(400, f"nothing to set; send a JSON body such as {SET_EXAMPLE}")
    return settings


async def _read_body(request: web.Request) -> Dict[str, Any]:
    """The JSON object in the body, whatever Content-Type the client claimed.

    `curl -d '{...}'` labels JSON as a form post, so the header is not trusted.
    Query-string parameters fill in anything the body leaves out.
    """
    raw = (await request.read()).strip()
    data: Any = {}
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
            if request.content_type == "application/x-www-form-urlencoded":
                data = dict(parse_qsl(raw.decode("utf-8", "replace"))) or None
        if not isinstance(data, dict):
            raise ApiError(400, f"request body must be a JSON object such as {SET_EXAMPLE}")
    for key, value in request.query.items():
        data.setdefault(key, value)
    return data


def _summary(statuses: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Top-level fields so single-light setups never have to index into "lights"."""
    first = statuses[0]
    known = [status["on"] for status in statuses if status["on"] is not None]
    return {
        "on": int(any(known)) if known else None,
        "brightness": first["brightness"],
        "kelvin": first["kelvin"],
        "temperature": first["temperature"],
        "reachable": all(status["reachable"] for status in statuses),
    }


class Api:
    def __init__(self, lights: List[Light], secret: Optional[str] = None):
        self.lights = lights
        self.secret = secret

    def app(self) -> web.Application:
        app = web.Application(middlewares=[self._middleware])
        app.router.add_get("/status", self.status)
        app.router.add_get("/light/{target}/status", self.status)
        for action in ("on", "off", "toggle", "set"):
            app.router.add_post(f"/{action}", self.write)
            app.router.add_post(f"/light/{{target}}/{action}", self.write)
        return app

    @web.middleware
    async def _middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        try:
            # Browsers attach Origin to cross-site POSTs; nothing legitimate here
            # does, so this stops a web page from driving the light.
            if "Origin" in request.headers:
                raise ApiError(403, "requests from web pages are not allowed")
            if self.secret and not self._authorized(request):
                raise ApiError(401, f"missing or wrong {SECRET_HEADER} header")
            return await handler(request)
        except ApiError as exc:
            return _error(exc.status, str(exc))
        except web.HTTPException as exc:
            return _error(exc.status, exc.reason)
        except Exception:
            log.exception("unhandled error in %s %s", request.method, request.path)
            return _error(500, "internal error")

    def _authorized(self, request: web.Request) -> bool:
        offered = request.headers.get(SECRET_HEADER, "")
        if not offered and request.headers.get("Authorization", "").startswith("Bearer "):
            offered = request.headers["Authorization"][len("Bearer ") :]
        return hmac.compare_digest(offered.encode(), (self.secret or "").encode())

    def _targets(self, target: Optional[Any]) -> Tuple[List[Light], bool]:
        """The lights a request addresses, and whether it named a single one."""
        if target is None:
            return self.lights, False
        wanted = str(target).lower()
        for light in self.lights:
            if wanted in (light.name.lower(), light.serial.lower()):
                return [light], True
        known = ", ".join(light.name for light in self.lights)
        raise ApiError(404, f"no light named {target!r} (configured: {known})")

    async def status(self, request: web.Request) -> web.Response:
        lights, single = self._targets(request.match_info.get("target"))
        return self._respond([light.status() for light in lights], single)

    async def write(self, request: web.Request) -> web.Response:
        action = request.path.rsplit("/", 1)[-1]
        target = request.match_info.get("target")
        if action == "set":
            data = await _read_body(request)
            body_target = data.pop("light", None)
            lights, single = self._targets(body_target if target is None else target)
            settings = parse_settings(data)
        else:
            lights, single = self._targets(target)
            if action == "toggle":
                # State is only trustworthy once connected; unreachable lights
                # fail below with a clear error either way.
                await asyncio.gather(
                    *(light.ensure_connected() for light in lights), return_exceptions=True
                )
                settings = {"on": int(not any(light.is_on for light in lights))}
            else:
                settings = {"on": int(action == "on")}

        results = await asyncio.gather(
            *(light.apply(**settings) for light in lights), return_exceptions=True
        )
        statuses = []
        failures: List[BaseException] = []
        for light, result in zip(lights, results):
            if isinstance(result, LightError):
                failures.append(result)
                result = {**light.status(), "error": str(result)}
            elif isinstance(result, BaseException):
                raise result
            statuses.append(result)
        return self._respond(statuses, single, failures)

    def _respond(
        self,
        statuses: List[Dict[str, Any]],
        single: bool,
        failures: Sequence[BaseException] = (),
    ) -> web.Response:
        body: Dict[str, Any] = {"ok": not failures}
        if failures:
            body["error"] = "; ".join(dict.fromkeys(str(failure) for failure in failures))
        if single:
            body.update(statuses[0])
        else:
            body.update(_summary(statuses))
            body["lights"] = statuses
        if not failures:
            status = 200
        elif any(isinstance(failure, LightUnreachable) for failure in failures):
            status = 503
        elif any(isinstance(failure, LightRangeError) for failure in failures):
            status = 400
        else:
            status = 502
        return web.json_response(body, status=status)
