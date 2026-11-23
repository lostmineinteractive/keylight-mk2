"""Write config.json for the installer (`python -m keylight_mk2.configure`).

Discovers MK.2 lights over mDNS (or takes --host), asks each one for its
serial number, and writes the config. Settings and light names already in the
file are kept, so re-running the installer never clobbers hand edits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DEFAULT_BIND, DEFAULT_PORT, default_home
from .light import Light, LightError

EXIT_NOT_FOUND = 3


async def _probe(host: str, certs: Path) -> Dict[str, Any]:
    """accessory-info from the light at `host`, or {} if it does not answer."""
    light = Light("probe", host, certs=certs)
    light.start()
    try:
        await light.ensure_connected(timeout=12)
        return light.info
    except LightError as exc:
        print(f"  warning: {exc}", file=sys.stderr)
        return {}
    finally:
        await light.stop()


async def _probe_all(hosts: List[str], certs: Path) -> List[Dict[str, Any]]:
    return list(await asyncio.gather(*(_probe(host, certs) for host in hosts)))


def _name_for(info: Dict[str, Any], index: int, total: int, taken: set) -> str:
    base = "key" if total == 1 else ""
    if not base:
        base = re.sub(r"[^a-z0-9]+", "-", str(info.get("displayName") or "").lower()).strip("-")
    base = base or f"light{index + 1}"
    name, suffix = base, 2
    while name in taken:
        name, suffix = f"{base}{suffix}", suffix + 1
    return name


def _write(path: Path, config: Dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    # 0600: the file may carry the optional shared secret.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(config, indent=2) + "\n")
    os.replace(tmp, path)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="keylight_mk2.configure", description=__doc__)
    parser.add_argument("--config", type=Path, default=default_home() / "config.json")
    parser.add_argument("--certs", type=Path, help="default: certs/ next to the config file")
    parser.add_argument("--host", action="append", default=[], help="skip discovery (repeatable)")
    parser.add_argument("--port", type=int, help="HTTP port to write into the config")
    parser.add_argument("--rediscover", action="store_true", help="replace the configured lights")
    parser.add_argument("--timeout", type=float, default=4.0, help="mDNS listen time in seconds")
    args = parser.parse_args(argv)
    certs = args.certs or args.config.parent / "certs"

    config: Dict[str, Any] = {}
    if args.config.exists():
        try:
            config = json.loads(args.config.read_text())
        except ValueError as exc:
            print(f"error: {args.config} is not valid JSON ({exc}); fix or delete it", file=sys.stderr)
            return 1
    existing = [e for e in config.get("lights") or [] if isinstance(e, dict) and e.get("host")]
    config.setdefault("bind", DEFAULT_BIND)
    config.setdefault("port", DEFAULT_PORT)
    if args.port:
        config["port"] = args.port

    if existing and not args.host and not args.rediscover:
        print(f"Keeping the {len(existing)} light(s) already in {args.config.name}:")
        for entry in existing:
            print(f"  {entry.get('name', '?')}: {entry['host']}")
        _write(args.config, config)
        return 0

    if args.host:
        hosts = args.host
    else:
        from .discover import discover, is_mk2  # zeroconf is only needed here

        print(f"Searching the network for MK.2 lights ({args.timeout:g}s)...")
        devices = discover(args.timeout)
        for device in devices:
            if not is_mk2(device):
                print(f"  skipping {device['instance']}: not an MK.2 (no TLS marker)")
        hosts = [device["host"] for device in devices if is_mk2(device) and device["host"]]
        if not hosts:
            print("error: no MK.2 lights found via mDNS", file=sys.stderr)
            return EXIT_NOT_FOUND

    infos = asyncio.run(_probe_all(hosts, certs))
    lights: List[Dict[str, str]] = []
    taken: set = set()
    for index, (host, info) in enumerate(zip(hosts, infos)):
        serial = str(info.get("serialNumber") or "")
        previous = next(
            (
                e
                for e in existing
                if e["host"].lower() == host.lower() or (serial and e.get("serial") == serial)
            ),
            {},
        )
        name = previous.get("name")
        if not name or name in taken:
            name = _name_for(info, index, len(hosts), taken)
        taken.add(name)
        lights.append({"name": name, "host": host, "serial": serial or previous.get("serial", "")})
        details = (
            f"{info.get('productName', 'light')}, firmware {info.get('firmwareVersion', '?')}"
            if info
            else "did not answer; check it is powered and on this network"
        )
        print(f"  {name}: {host} ({details})")

    config["lights"] = lights
    _write(args.config, config)
    print(f"Wrote {args.config}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
