"""modem-controller asyncio daemon entrypoint.

Phase 2 wiring:
- IPC server (Unix socket, newline-delimited JSON) — PR #6
- AT client / dial mock                            — PR #7
- udev watcher                                     — PR #8
"""

from __future__ import annotations

import asyncio
import logging
import os

from isales_common.utils.redis import get_redis  # noqa: F401  (future use)

from isales_telephony.common.db import get_engine, get_sessionmaker
from isales_telephony.modem_controller.at_client import MockATClient
from isales_telephony.modem_controller.handlers import build_handlers
from isales_telephony.modem_controller.ipc_server import IPCServer

logger = logging.getLogger("isales.modem_controller")

DEFAULT_SOCKET_PATH = "/var/run/isales/modem.sock"


def _socket_path() -> str:
    return os.environ.get("ISALES_MODEM_SOCKET", DEFAULT_SOCKET_PATH)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    sm = None
    engine = None
    if os.environ.get("ISALES_DATABASE_URL"):
        engine = get_engine()
        sm = get_sessionmaker(engine)
    handlers = build_handlers(at_client=MockATClient(), sm=sm)
    server = IPCServer(_socket_path(), handlers)
    await server.start()
    logger.info("modem-controller IPC server listening on %s", server.socket_path)
    try:
        await server.serve_forever()
    finally:
        await server.stop()
        if engine is not None:
            await engine.dispose()


def run() -> None:
    """Console-script entry point: ``modem-controller``."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("modem-controller stopped")
