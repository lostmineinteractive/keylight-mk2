"""Locations on disk and the daemon's config file."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

APP_NAME = "keylight-mk2-bridge"
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 9124
CERT_FILES = ("client-chain.pem", "client.key", "root.crt.pem")


class ConfigError(Exception):
    pass


def default_home() -> Path:
    override = os.environ.get("KEYLIGHT_MK2_BRIDGE_HOME")
    if override:
        return Path(override)
    return Path.home() / "Library" / "Application Support" / APP_NAME


@dataclass
class LightConfig:
    name: str
    host: str
    serial: str = ""


@dataclass
class Config:
    home: Path
    certs: Path
    bind: str = DEFAULT_BIND
    port: int = DEFAULT_PORT
    secret: Optional[str] = None
    lights: List[LightConfig] = field(default_factory=list)


def load(path: Path, certs: Optional[Path] = None) -> Config:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path} (run install.sh)") from None
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a JSON object")

    port = raw.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError(f'"port" must be a number between 1 and 65535, got {port!r}')

    lights = []
    entries = raw.get("lights")
    for index, entry in enumerate(entries if isinstance(entries, list) else []):
        host = entry.get("host") if isinstance(entry, dict) else None
        if not host or not isinstance(host, str):
            raise ConfigError(f'lights[{index}] needs a "host"')
        name = str(entry.get("name") or f"light{index + 1}")
        lights.append(LightConfig(name=name, host=host, serial=str(entry.get("serial") or "")))
    if not lights:
        raise ConfigError(f'{path} lists no lights; add one under "lights" or re-run install.sh')
    names = [light.name.lower() for light in lights]
    if len(set(names)) != len(names):
        raise ConfigError("light names must be unique")

    home = path.parent
    return Config(
        home=home,
        certs=certs or home / "certs",
        bind=str(raw.get("bind") or DEFAULT_BIND),
        port=port,
        secret=str(raw["secret"]) if raw.get("secret") else None,
        lights=lights,
    )
