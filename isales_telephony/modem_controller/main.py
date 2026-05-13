"""modem-controller asyncio daemon entrypoint.

Phase 2 wiring:
- IPC server (Unix socket, newline-delimited JSON) — PR #6
- AT client / dial mock                            — PR #7
- udev watcher                                     — PR #8

impl-real-at: AT client is chosen at startup by environment, NOT defaulted to
MockATClient. See ``_make_at_client`` for the dispatch rules.
"""

from __future__ import annotations

import asyncio
import logging
import os

from isales_common.utils.redis import get_redis  # noqa: F401  (future use)

from isales_telephony.common.db import get_engine, get_sessionmaker
from isales_telephony.modem_controller.at_client import (
    ATClient,
    MockATClient,
    SerialATClient,
)
from isales_telephony.modem_controller.handlers import build_handlers
from isales_telephony.modem_controller.ipc_server import IPCServer
from isales_telephony.modem_controller.udev_watcher import start_udev_watcher

logger = logging.getLogger("isales.modem_controller")

DEFAULT_SOCKET_PATH = "/var/run/isales/modem.sock"


def _socket_path() -> str:
    return os.environ.get("ISALES_MODEM_SOCKET", DEFAULT_SOCKET_PATH)


async def _make_at_client() -> ATClient:
    """Choose an ATClient implementation from env vars (impl-real-at).

    Rules (device-hardware spec § "ATClient 实现策略"):
      1. ``ISALES_MODEM_SERIAL_PATH`` set → ``SerialATClient.create_from_tty(...)``;
         construction failures propagate (process exits non-zero).
      2. ``ISALES_ALLOW_MOCK_AT=1`` (and no TTY) → ``MockATClient`` with WARN.
      3. Otherwise → ``RuntimeError`` so systemd / launchd restart picks up the
         misconfiguration loudly.
    """
    tty = os.environ.get("ISALES_MODEM_SERIAL_PATH")
    if tty:
        driver_hint = os.environ.get("ISALES_MODEM_DRIVER", "")
        logger.info(
            "modem-controller: opening serial tty=%s driver_hint=%r",
            tty,
            driver_hint or "<auto-detect>",
        )
        return await SerialATClient.create_from_tty(tty, driver_hint=driver_hint)
    if os.environ.get("ISALES_ALLOW_MOCK_AT") == "1":
        logger.warning(
            "modem-controller: ISALES_MODEM_SERIAL_PATH unset; using MockATClient "
            "(dev/CI mode). MUST NOT happen in production."
        )
        return MockATClient()
    raise RuntimeError(
        "ISALES_MODEM_SERIAL_PATH is required to start modem-controller; "
        "set ISALES_ALLOW_MOCK_AT=1 only for dev/CI."
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    sm = None
    engine = None
    if os.environ.get("ISALES_DATABASE_URL"):
        engine = get_engine()
        sm = get_sessionmaker(engine)
    at_client = await _make_at_client()
    handlers = build_handlers(at_client=at_client, sm=sm)
    server = IPCServer(_socket_path(), handlers)
    await server.start()
    logger.info("modem-controller IPC server listening on %s", server.socket_path)
    udev_task = await start_udev_watcher(sm) if sm is not None else None
    try:
        await server.serve_forever()
    finally:
        await server.stop()
        if udev_task is not None:
            udev_task.cancel()
        if hasattr(at_client, "aclose"):
            try:
                await at_client.aclose()  # type: ignore[func-returns-value]
            except Exception:  # noqa: BLE001
                logger.exception("at_client.aclose failed")
        if engine is not None:
            await engine.dispose()


def run() -> None:
    """Console-script entry point: ``modem-controller``."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("modem-controller stopped")
