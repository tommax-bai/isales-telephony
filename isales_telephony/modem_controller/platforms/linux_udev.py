"""Linux pyudev-backed :class:`UsbDeviceWatcher`.

Mirrors the pre-refactor behavior of ``udev_watcher._events()``:
- subsystem filter: ``tty``
- queue events into asyncio.Queue from a pyudev observer thread via
  ``loop.call_soon_threadsafe``
- yield :class:`UdevEvent` from the queue forever until ``stop()``

Lazy-imports pyudev so that this module can be imported on any platform
(it's only instantiated on Linux per :func:`platforms.get_usb_watcher_class`).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from .base import UdevEvent, UsbDeviceWatcher, UsbDeviceWatcherError


class LinuxUdevWatcher(UsbDeviceWatcher):
    def __init__(self) -> None:
        self._queue: asyncio.Queue[UdevEvent] | None = None
        self._observer: object | None = None  # pyudev.MonitorObserver
        self._stopped = False

    async def start(self) -> None:
        if self._observer is not None:
            raise UsbDeviceWatcherError(
                platform="linux",
                message="LinuxUdevWatcher.start() called twice",
            )
        try:
            import pyudev  # noqa: PLC0415  (Linux-only dep)
        except ImportError as exc:
            raise UsbDeviceWatcherError(
                platform="linux",
                message="pyudev not installed; pip install -e '.[linux]'",
            ) from exc

        loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        ctx = pyudev.Context()
        monitor = pyudev.Monitor.from_netlink(ctx)
        monitor.filter_by(subsystem="tty")

        queue = self._queue

        def _on_pyudev(device: object) -> None:
            ev = UdevEvent(
                action=str(getattr(device, "action", "")),
                device_node=getattr(device, "device_node", None),
                vendor_id=device.get("ID_VENDOR_ID"),  # type: ignore[attr-defined]
                product_id=device.get("ID_MODEL_ID"),  # type: ignore[attr-defined]
            )
            loop.call_soon_threadsafe(queue.put_nowait, ev)

        observer = pyudev.MonitorObserver(monitor, callback=_on_pyudev)
        observer.start()
        self._observer = observer

    def events(self) -> AsyncIterator[UdevEvent]:
        if self._queue is None:
            raise UsbDeviceWatcherError(
                platform="linux",
                message="events() called before start()",
            )

        return self._drain()

    async def _drain(self) -> AsyncIterator[UdevEvent]:
        assert self._queue is not None
        while not self._stopped:
            ev = await self._queue.get()
            yield ev

    async def stop(self) -> None:
        self._stopped = True
        observer = self._observer
        if observer is not None:
            # Tear-down must never raise; pyudev observer.stop() can occasionally
            # throw if the netlink monitor was never opened (start() failed mid-way).
            with contextlib.suppress(Exception):
                observer.stop()  # type: ignore[attr-defined]
        self._observer = None
