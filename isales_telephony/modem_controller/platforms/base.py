"""Platform-neutral ABC + types for USB device watching.

The :class:`UdevEvent` dataclass keeps the original name (legacy callers
import it) but its semantics are platform-neutral — Linux implementations
fill it from pyudev events, macOS implementations from IOKit notifications,
future Windows implementations from WMI / SetupAPI device events.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(frozen=True)
class UdevEvent:
    """USB device event (add / remove / change). Platform-neutral.

    ``device_node`` is a tty / serial device path on the host:
    ``/dev/ttyUSB0`` on Linux, ``/dev/cu.usbmodem*`` on macOS. ``None`` when
    the platform reports a device-level event without a tty child node.
    """

    action: str  # "add" | "remove" | "change"
    device_node: str | None
    vendor_id: str | None  # 4-char hex, lowercase
    product_id: str | None  # 4-char hex, lowercase


class UsbDeviceWatcherError(RuntimeError):
    """Raised when a USB watcher cannot start or runs into an unrecoverable
    platform error.

    ``platform`` field carries ``sys.platform`` so callers can render a
    useful message ("no implementation for platform=win32") without leaking
    OS-specific details into business code.
    """

    def __init__(self, *, platform: str, message: str) -> None:
        super().__init__(f"[{platform}] {message}")
        self.platform = platform


class UsbDeviceWatcher(abc.ABC):
    """Async USB device add/remove watcher. Lifecycle:

    1. Construct (cheap, no I/O).
    2. ``await start()`` to kick off platform-specific subscription.
    3. ``async for ev in watcher.events()`` to consume events.
    4. ``await stop()`` to tear down — must be safe even if ``start`` failed.

    Implementations are expected to be single-shot: starting twice MAY raise
    :class:`UsbDeviceWatcherError`.
    """

    @abc.abstractmethod
    async def start(self) -> None:
        """Set up platform subscriptions. Idempotent NOT required."""

    @abc.abstractmethod
    def events(self) -> AsyncIterator[UdevEvent]:
        """Yield events until ``stop()`` is called."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Tear down. Must not raise even when start() failed."""


__all__ = ["UdevEvent", "UsbDeviceWatcher", "UsbDeviceWatcherError"]
