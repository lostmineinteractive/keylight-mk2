"""The bridge daemon: one asyncio process holding the lights and serving HTTP."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
import signal
import sys
from pathlib import Path
from typing import List, Optional

from aiohttp import web

from . import __version__
from .api import SECRET_HEADER, Api
from .config import CERT_FILES, Config, ConfigError, default_home, load
from .light import IPCache, Light

log = logging.getLogger("bridge")


def _is_loopback(bind: str) -> bool:
    try:
        return ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return bind == "localhost"


async def run(cfg: Config) -> None:
    ip_cache = IPCache(cfg.home / "state.json")
    lights = [
        Light(entry.name, entry.host, entry.serial, certs=cfg.certs, ip_cache=ip_cache)
        for entry in cfg.lights
    ]
    runner = web.AppRunner(Api(lights, cfg.secret).app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, cfg.bind, cfg.port).start()
    log.info("keylight-mk2-bridge %s listening on http://%s:%d", __version__, cfg.bind, cfg.port)
    if not _is_loopback(cfg.bind) and not cfg.secret:
        log.warning(
            'bound to %s with no "secret" in the config: anyone on this network can '
            "control the lights (set a secret to require the %s header)",
            cfg.bind,
            SECRET_HEADER,
        )

    for light in lights:
        light.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    await stop.wait()

    log.info("shutting down")
    await asyncio.gather(*(light.stop() for light in lights))
    await runner.cleanup()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="keylight-mk2-bridge", description=__doc__)
    parser.add_argument("--config", type=Path, default=default_home() / "config.json")
    parser.add_argument("--certs", type=Path, help="default: certs/ next to the config file")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)

    try:
        cfg = load(args.config, args.certs)
    except ConfigError as exc:
        log.error("%s", exc)
        return 78  # EX_CONFIG
    missing = [name for name in CERT_FILES if not (cfg.certs / name).is_file()]
    if missing:
        log.error("missing %s in %s (run install.sh to mint them)", ", ".join(missing), cfg.certs)
        return 78

    try:
        asyncio.run(run(cfg))
    except OSError as exc:
        log.error("cannot serve on %s:%d: %s", cfg.bind, cfg.port, exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
