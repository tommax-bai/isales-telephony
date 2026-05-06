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

from isales_telephony.modem_controller.handlers import DEFAULT_HANDLERS
from isales_telephony.modem_controller.ipc_server import IPCServer

logger = logging.getLogger("isales.modem_controller")

DEFAULT_SOCKET_PATH = "/var/run/isales/modem.sock"


def _socket_path() -> str:
    return os.environ.get("ISALES_MODEM_SOCKET", DEFAULT_SOCKET_PATH)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    server = IPCServer(_socket_path(), DEFAULT_HANDLERS)
    await server.start()
    logger.info("modem-controller IPC server listening on %s", server.socket_path)
    try:
        await server.serve_forever()
    finally:
        await server.stop()


def run() -> None:
    """Console-script entry point: ``modem-controller``."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("modem-controller stopped")
