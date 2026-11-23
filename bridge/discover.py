"""Find Elgato lights on the LAN via mDNS (`python -m bridge.discover`)."""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict, List, Optional

from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

SERVICE = "_elg._tcp.local."
# TXT marker for lights that speak the mutual-TLS protocol. Lights without it
# use the older plain-HTTP API and are out of scope.
TLS_MARKER = "CERT_1_VER_EN"


class _Collector(ServiceListener):
    def __init__(self) -> None:
        self.names: set = set()

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.names.add(name)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.names.add(name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass


def discover(timeout: float = 4.0) -> List[Dict[str, Any]]:
    """Every `_elg._tcp` device that answers within `timeout` seconds."""
    zc = Zeroconf()
    try:
        collector = _Collector()
        ServiceBrowser(zc, SERVICE, collector)
        time.sleep(timeout)
        found = []
        for name in sorted(collector.names):
            info = zc.get_service_info(SERVICE, name, timeout=3000)
            if info is None:
                continue
            txt = {
                key.decode("utf-8", "replace"): (value or b"").decode("utf-8", "replace")
                for key, value in info.properties.items()
            }
            found.append(
                {
                    "instance": name[: -len(SERVICE) - 1],
                    "host": (info.server or "").rstrip("."),
                    "addresses": info.parsed_addresses(),
                    "model": txt.get("md", ""),
                    "id": txt.get("id", ""),
                    "tls": txt.get("tls", ""),
                }
            )
        return found
    finally:
        zc.close()


def is_mk2(device: Dict[str, Any]) -> bool:
    return device["tls"] == TLS_MARKER


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="bridge.discover", description=__doc__)
    parser.add_argument("--timeout", type=float, default=4.0, help="seconds to listen (default 4)")
    parser.add_argument("--all", action="store_true", help="include lights without the TLS marker")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    devices = [d for d in discover(args.timeout) if args.all or is_mk2(d)]
    if args.json:
        print(json.dumps(devices, indent=2))
    else:
        for device in devices:
            print(f"{device['host']}\t{', '.join(device['addresses'])}\t{device['instance']}")
        if not devices:
            print("no MK.2 lights found", file=sys.stderr)
    return 0 if devices else 1


if __name__ == "__main__":
    sys.exit(main())
