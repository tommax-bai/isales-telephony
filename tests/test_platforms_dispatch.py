"""Platform dispatch tests for modem_controller.platforms.

Task 1.7 of impl-deploy-macos: verify ``get_usb_watcher_class()`` returns
the correct concrete impl per ``sys.platform``, and surfaces a clear
``UsbDeviceWatcherError`` for unsupported platforms.

These are unit tests against the dispatcher only; they do NOT exercise
real pyudev or IOKit.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator

import pytest

from isales_telephony.modem_controller.platforms import (
    UdevEvent,
    UsbDeviceWatcher,
    UsbDeviceWatcherError,
    get_usb_watcher_class,
)
from isales_telephony.modem_controller.platforms.linux_udev import LinuxUdevWatcher


def test_dispatch_linux_returns_linux_class(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    cls = get_usb_watcher_class()
    assert cls is LinuxUdevWatcher
    assert issubclass(cls, UsbDeviceWatcher)


def test_dispatch_darwin_returns_macos_class(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    cls = get_usb_watcher_class()
    # MacOSIokitWatcher is a placeholder until impl-deploy-macos PR #3 lands;
    # we only verify dispatch picks the right class, not that __init__ works.
    assert cls.__name__ == "MacOSIokitWatcher"
    assert issubclass(cls, UsbDeviceWatcher)


def test_dispatch_unsupported_platform_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(UsbDeviceWatcherError) as exc_info:
        get_usb_watcher_class()
    assert exc_info.value.platform == "win32"
    assert "win32" in str(exc_info.value)


def test_macos_iokit_instantiation() -> None:
    """MacOSIokitWatcher is now instantiable (PR #3 landed the polling
    impl). Detailed lifecycle / diff tests live in
    ``tests/macos/test_iokit_watcher.py``.
    """
    from isales_telephony.modem_controller.platforms.macos_iokit import (
        MacOSIokitWatcher,
    )

    w = MacOSIokitWatcher(poll_interval=10.0)  # not started — no I/O
    assert w._task is None  # type: ignore[attr-defined]


def test_udev_event_is_platform_neutral() -> None:
    """UdevEvent should hold no platform-specific handles. Construction
    works without any pyudev / pyobjc dependency.
    """
    ev = UdevEvent(
        action="add", device_node="/dev/cu.usbmodem001", vendor_id="2c7c", product_id="0125"
    )
    assert ev.action == "add"
    assert ev.device_node == "/dev/cu.usbmodem001"


class _StubWatcher(UsbDeviceWatcher):
    """Trivial impl proves the ABC contract is implementable cleanly."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    def events(self) -> AsyncIterator[UdevEvent]:
        async def _gen() -> AsyncIterator[UdevEvent]:
            return
            yield  # unreachable but keeps generator type

        return _gen()

    async def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio(loop_scope="session")
async def test_abc_contract_lifecycle() -> None:
    w = _StubWatcher()
    await w.start()
    assert w.started
    async for _ in w.events():
        pass  # generator is empty
    await w.stop()
    assert w.stopped
