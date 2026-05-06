"""udev event watcher (Linux-only at runtime; mockable for tests).

This module monitors USB serial / TTY device add/remove events. When a known
GSM modem (matched by USB vendor/product ID against ``GSM_MODEM_WHITELIST``)
is plugged in, we update ``device.last_seen_at`` so the modem's row reflects
the recent presence; on remove we flip ``status`` to OFFLINE so the
/devices/select query stops considering it.

v1 stage 2 deliberately does NOT issue AT commands here — full identification
(query ICCID, IMEI, signal) is stage 6 territory. The whitelist match is by
USB ID alone; mapping a freshly-plugged dongle to a particular ``device`` row
needs serial-number reading (also stage 6).

macOS / non-Linux hosts have no usable pyudev. ``ISALES_SKIP_UDEV=1`` (or
``platform.system() != "Linux"``) makes :func:`start_udev_watcher` a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from isales_common.enums import DeviceStatus
from isales_common.models import Device
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

# vendor_id, product_id — case-insensitive 4-char hex. Add real values when
# stage 6 enumerates the supported modems.
GSM_MODEM_WHITELIST: set[tuple[str, str]] = {
    ("2c7c", "0125"),  # Quectel EC25 (placeholder; verify in stage 6)
}


@dataclass(frozen=True)
class UdevEvent:
    action: str  # "add" | "remove" | "change"
    device_node: str | None
    vendor_id: str | None
    product_id: str | None


def _is_known_modem(ev: UdevEvent) -> bool:
    if not ev.vendor_id or not ev.product_id:
        return False
    return (ev.vendor_id.lower(), ev.product_id.lower()) in GSM_MODEM_WHITELIST


async def _on_event(
    sm: async_sessionmaker[AsyncSession], ev: UdevEvent
) -> None:
    if ev.action == "add":
        if not _is_known_modem(ev):
            logger.debug("udev add: ignoring unknown device %s", ev)
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
            logger.info("udev add: device_node %s not in DB yet", device_node)
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
    """Pump udev events into DB updates. Returns when the iterator ends."""
    async for ev in events:
        try:
            await _on_event(sm, ev)
        except Exception:
            logger.exception("udev: event handler failed for %s", ev)


def _platform_skip() -> bool:
    return os.environ.get("ISALES_SKIP_UDEV") == "1" or platform.system() != "Linux"


async def start_udev_watcher(
    sm: async_sessionmaker[AsyncSession],
) -> asyncio.Task[None] | None:
    """Start the udev watcher as a background task. No-op on non-Linux."""
    if _platform_skip():
        logger.info("udev_watcher: skipped (platform=%s)", platform.system())
        return None

    # Lazy import — pyudev is Linux-only.
    import pyudev  # noqa: PLC0415

    async def _events() -> AsyncIterator[UdevEvent]:
        ctx = pyudev.Context()
        monitor = pyudev.Monitor.from_netlink(ctx)
        monitor.filter_by(subsystem="tty")
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[UdevEvent] = asyncio.Queue()

        def _on_pyudev(device: pyudev.Device) -> None:
            ev = UdevEvent(
                action=str(getattr(device, "action", "")),
                device_node=getattr(device, "device_node", None),
                vendor_id=device.get("ID_VENDOR_ID"),
                product_id=device.get("ID_MODEL_ID"),
            )
            loop.call_soon_threadsafe(queue.put_nowait, ev)

        observer = pyudev.MonitorObserver(monitor, callback=_on_pyudev)
        observer.start()
        try:
            while True:
                yield await queue.get()
        finally:
            observer.stop()

    task = asyncio.create_task(consume_events(sm, _events()))
    return task


def fake_events(events: Iterable[UdevEvent]) -> AsyncIterator[UdevEvent]:
    """Convenience for tests — wrap an iterable as an async iterator."""

    async def _gen() -> AsyncIterator[UdevEvent]:
        for ev in events:
            yield ev

    return _gen()
