"""Platform abstraction for modem-controller's OS-coupled subsystems.

Two ABCs live here:

- :class:`UsbDeviceWatcher` — emits :class:`UdevEvent` (platform-neutral) on
  USB add/remove. Linux impl wraps pyudev; macOS impl wraps IOKit
  ``IOServiceMatching("IOUSBHostDevice")`` notifications.
- :class:`AudioBackends` (struct of capture + playback) — see
  :mod:`isales_telephony.modem_controller.audio` for the actual Protocol-based
  abstraction. The audio side stays in its own package because the existing
  ``AudioPipe`` orchestrator (resampling + jitter buffer) is cross-platform
  and already factored cleanly.

Selection happens here via :func:`get_usb_watcher_class`. Callers MUST go
through this module — direct ``import pyudev`` / ``import pyobjc`` from
business code is forbidden by spec.

Adding a third platform (e.g. Windows) means: add ``platforms/windows_X.py``
with a class implementing the ABC, then extend :func:`get_usb_watcher_class`
to dispatch — no caller-side changes.
"""

from __future__ import annotations

import sys

from .base import (
    UdevEvent,
    UsbDeviceWatcher,
    UsbDeviceWatcherError,
)


def get_usb_watcher_class() -> type[UsbDeviceWatcher]:
    """Return the platform-appropriate :class:`UsbDeviceWatcher` impl class.

    The class is *not* instantiated here so callers control async lifetime
    (e.g. wiring it into asyncio.create_task in main.py).
    """
    if sys.platform == "linux":
        from .linux_udev import LinuxUdevWatcher  # noqa: PLC0415  (lazy import)

        return LinuxUdevWatcher
    if sys.platform == "darwin":
        from .macos_iokit import MacOSIokitWatcher  # noqa: PLC0415

        return MacOSIokitWatcher
    raise UsbDeviceWatcherError(
        platform=sys.platform,
        message=(
            f"no UsbDeviceWatcher implementation for platform={sys.platform!r}; "
            "supported: linux, darwin"
        ),
    )


__all__ = [
    "UdevEvent",
    "UsbDeviceWatcher",
    "UsbDeviceWatcherError",
    "get_usb_watcher_class",
]
