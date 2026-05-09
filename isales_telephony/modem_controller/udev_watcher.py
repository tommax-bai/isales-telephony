"""USB device watcher — domain logic + Linux/macOS dispatch.

This module is intentionally light after impl-deploy-macos PR #1: the
platform-specific event source moved to
:mod:`isales_telephony.modem_controller.platforms`, and this file now
holds only the cross-platform domain logic (modem-whitelist match, DB
write-back, dispatch helper).

Public API (kept stable for legacy callers and the 115 stage-2 tests):

- :class:`UdevEvent` — re-exported from ``platforms.base``
- :data:`GSM_MODEM_WHITELIST` — vendor/product allowlist
- :func:`consume_events` — pump events into DB updates
- :func:`fake_events` — wrap an iterable as AsyncIterator (test helper)
- :func:`start_udev_watcher` — spawn background task; no-op on non-Linux
  with ``ISALES_SKIP_UDEV=1`` (legacy macOS dev mode); on macOS without
  the env var, dispatches to MacOSIokitWatcher (raises NotImplementedError
  until impl-deploy-macos PR #3).

v1 stage 2 deliberately does NOT issue AT commands here — full
identification (query ICCID, IMEI, signal) is stage 6 territory.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import sys
from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime

from isales_common.enums import DeviceStatus
from isales_common.models import Device
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .platforms import UdevEvent, UsbDeviceWatcherError, get_usb_watcher_class

logger = logging.getLogger(__name__)

# vendor_id, product_id — case-insensitive 4-char hex. Add real values when
# stage 6 enumerates the supported modems.
GSM_MODEM_WHITELIST: set[tuple[str, str]] = {
    ("2c7c", "0125"),  # Quectel EC25 (placeholder; verify in stage 6)
}


def _is_known_modem(ev: UdevEvent) -> bool:
    if not ev.vendor_id or not ev.product_id:
        return False
    return (ev.vendor_id.lower(), ev.product_id.lower()) in GSM_MODEM_WHITELIST


async def _on_event(
    sm: async_sessionmaker[AsyncSession], ev: UdevEvent
) -> None:
    if ev.action == "add":
        if not _is_known_modem(ev):
            logger.debug("usb add: ignoring unknown device %s", ev)
            return
        await _touch_last_seen(sm, ev.device_node)
    elif ev.action == "remove":
        await _mark_offline(sm, ev.device_node)


async def _touch_last_seen(
    sm: async_sessionmaker[AsyncSession], device_node: str | None
) -> None:
    if device_node is None:
        return
    async with sm() as session:
        dev = (
            await session.execute(
                select(Device).where(Device.usb_port == device_node)
            )
        ).scalar_one_or_none()
        if dev is None:
            logger.info("usb add: device_node %s not in DB yet", device_node)
            return
        dev.last_seen_at = datetime.now(tz=UTC)
        await session.commit()


async def _mark_offline(
    sm: async_sessionmaker[AsyncSession], device_node: str | None
) -> None:
    if device_node is None:
        return
    async with sm() as session:
        dev = (
            await session.execute(
                select(Device).where(Device.usb_port == device_node)
            )
        ).scalar_one_or_none()
        if dev is None:
            return
        dev.status = DeviceStatus.OFFLINE
        await session.commit()


async def consume_events(
    sm: async_sessionmaker[AsyncSession], events: AsyncIterator[UdevEvent]
) -> None:
    """Pump USB events into DB updates. Returns when the iterator ends."""
    async for ev in events:
        try:
            await _on_event(sm, ev)
        except Exception:
            logger.exception("usb watcher: event handler failed for %s", ev)


def _platform_skip() -> bool:
    """Legacy escape hatch: ``ISALES_SKIP_UDEV=1`` disables the watcher
    entirely. Used by non-Linux dev environments before macOS support
    landed; kept for forward compatibility.
    """
    return os.environ.get("ISALES_SKIP_UDEV") == "1"


async def start_udev_watcher(
    sm: async_sessionmaker[AsyncSession],
) -> asyncio.Task[None] | None:
    """Start the USB watcher as a background task.

    Returns ``None`` when ``ISALES_SKIP_UDEV=1`` is set (legacy dev escape
    hatch). On unsupported platforms, propagates :class:`UsbDeviceWatcherError`
    from :func:`platforms.get_usb_watcher_class` so the caller fails fast
    rather than silently no-op.
    """
    if _platform_skip():
        logger.info("usb watcher: skipped via ISALES_SKIP_UDEV=1 (platform=%s)", platform.system())
        return None

    watcher_cls = get_usb_watcher_class()
    watcher = watcher_cls()
    await watcher.start()
    logger.info(
        "usb watcher: started (platform=%s, impl=%s)",
        sys.platform,
        watcher_cls.__name__,
    )

    async def _consume() -> None:
        try:
            await consume_events(sm, watcher.events())
        finally:
            await watcher.stop()

    return asyncio.create_task(_consume())


def fake_events(events: Iterable[UdevEvent]) -> AsyncIterator[UdevEvent]:
    """Convenience for tests — wrap an iterable as an async iterator."""

    async def _gen() -> AsyncIterator[UdevEvent]:
        for ev in events:
            yield ev

    return _gen()


__all__ = [
    "GSM_MODEM_WHITELIST",
    "UdevEvent",
    "UsbDeviceWatcherError",
    "consume_events",
    "fake_events",
    "start_udev_watcher",
]
