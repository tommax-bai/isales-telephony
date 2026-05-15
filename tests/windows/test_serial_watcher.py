"""Windows USB watcher tests — diff-from-polling semantics.

Same pattern as ``tests/macos/test_iokit_watcher.py``: inject a fake
scanner so the tests run on any host (macOS dev / Linux CI), since
the polling diff logic is platform-neutral. Real Windows hot-plug
acceptance happens during the D1 PoC week 1 implementation step (see
windows-client-core / tasks.md § 2).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from isales_telephony.modem_controller.platforms.base import UdevEvent
from isales_telephony.modem_controller.platforms.windows_serial import (
    WindowsSerialWatcher,
    _diff_scans,
    _PortIdentity,
)


def _ident(node: str, vid: str = "12d1", pid: str = "14db", serial: str = "S1") -> _PortIdentity:
    return _PortIdentity(device_node=node, vid=vid, pid=pid, serial=serial)


# ---------------------------------------------------------------------- diff


def test_diff_emits_add_for_newly_appeared_port() -> None:
    prev: dict[str, _PortIdentity] = {}
    curr = {"COM3": _ident("COM3")}
    events = _diff_scans(prev, curr)
    assert events == [
        UdevEvent(
            action="add",
            device_node="COM3",
            vendor_id="12d1",
            product_id="14db",
        )
    ]


def test_diff_emits_remove_for_disappeared_port() -> None:
    prev = {"COM4": _ident("COM4")}
    events = _diff_scans(prev, {})
    assert events == [
        UdevEvent(
            action="remove",
            device_node="COM4",
            vendor_id="12d1",
            product_id="14db",
        )
    ]


def test_diff_no_change_emits_nothing() -> None:
    state = {"COM3": _ident("COM3")}
    assert _diff_scans(state, state.copy()) == []


def test_diff_identity_change_on_same_path_emits_remove_then_add() -> None:
    """Windows reassigns ``COMn`` numbers across replug. A different
    VID/PID showing up on the same COMn (rare, mostly happens when the
    user moves a different modem to the same USB hub port) → emit a
    clean remove of old identity, then add of new.
    """
    prev = {"COM3": _ident("COM3", vid="aaaa", pid="bbbb")}
    curr = {"COM3": _ident("COM3", vid="12d1", pid="14db")}
    events = _diff_scans(prev, curr)
    assert [e.action for e in events] == ["remove", "add"]
    assert events[0].vendor_id == "aaaa"
    assert events[1].vendor_id == "12d1"


def test_diff_multi_port_modem() -> None:
    """Huawei / Quectel multi-mode modems enumerate as 3-4 COM ports
    per device. Plugging one in MUST emit one add per COM port — the
    AT-channel selection layer above the watcher picks the right one.
    """
    prev: dict[str, _PortIdentity] = {}
    curr = {
        "COM3": _ident("COM3", serial="HUAWEI-X1"),
        "COM4": _ident("COM4", serial="HUAWEI-X1"),
        "COM5": _ident("COM5", serial="HUAWEI-X1"),
    }
    events = _diff_scans(prev, curr)
    assert len(events) == 3
    assert all(e.action == "add" for e in events)
    assert {e.device_node for e in events} == {"COM3", "COM4", "COM5"}


# ---------------------------------------------------------- watcher lifecycle


class _FakeScanner:
    """Programmable scanner — drives the watcher's poll loop."""

    def __init__(self, scans: list[dict[str, _PortIdentity]]) -> None:
        self._scans = scans
        self._idx = 0

    def __call__(self) -> dict[str, _PortIdentity]:
        if self._idx >= len(self._scans):
            return self._scans[-1] if self._scans else {}
        out = self._scans[self._idx]
        self._idx += 1
        return out


@pytest.mark.asyncio(loop_scope="session")
async def test_watcher_emits_add_then_remove_in_order() -> None:
    scans = [
        {},  # seed (silent)
        {"COM3": _ident("COM3")},  # add
        {},  # remove
    ]
    w = WindowsSerialWatcher(poll_interval=0.01, scanner=_FakeScanner(scans))
    await w.start()
    try:
        events_iter = w.events()

        async def _next() -> UdevEvent:
            return await asyncio.wait_for(events_iter.__anext__(), timeout=1.0)

        ev1 = await _next()
        ev2 = await _next()
    finally:
        await w.stop()

    assert ev1.action == "add"
    assert ev1.device_node == "COM3"
    assert ev2.action == "remove"
    assert ev2.device_node == "COM3"


@pytest.mark.asyncio(loop_scope="session")
async def test_watcher_seed_does_not_emit_for_already_present_devices() -> None:
    """Devices visible at startup MUST NOT generate synthetic add events."""
    scans = [
        {"COM3": _ident("COM3")},  # seed
        {"COM3": _ident("COM3")},  # unchanged
        {},  # remove — MUST emit
    ]
    w = WindowsSerialWatcher(poll_interval=0.01, scanner=_FakeScanner(scans))
    await w.start()
    try:
        ev = await asyncio.wait_for(w.events().__anext__(), timeout=1.0)
    finally:
        await w.stop()
    assert ev.action == "remove"


@pytest.mark.asyncio(loop_scope="session")
async def test_watcher_start_twice_raises() -> None:
    from isales_telephony.modem_controller.platforms.base import (
        UsbDeviceWatcherError,
    )

    w = WindowsSerialWatcher(poll_interval=0.01, scanner=_FakeScanner([{}]))
    await w.start()
    try:
        with pytest.raises(UsbDeviceWatcherError):
            await w.start()
    finally:
        await w.stop()


@pytest.mark.asyncio(loop_scope="session")
async def test_watcher_stop_is_safe_after_failed_start() -> None:
    w = WindowsSerialWatcher(poll_interval=0.01, scanner=_FakeScanner([{}]))
    await w.stop()  # no exception


@pytest.mark.asyncio(loop_scope="session")
async def test_watcher_tolerates_scanner_exception() -> None:
    """pyserial may throw OSError mid-disconnect; watcher MUST retry."""
    seq: list[Callable[[], dict[str, _PortIdentity]]] = [
        lambda: {},  # seed
        _raise_oserror,
        lambda: {"COM3": _ident("COM3")},
    ]
    state = {"i": 0}

    def scan() -> dict[str, _PortIdentity]:
        i = state["i"]
        state["i"] = min(i + 1, len(seq) - 1)
        return seq[i]()

    w = WindowsSerialWatcher(poll_interval=0.01, scanner=scan)
    await w.start()
    try:
        ev = await asyncio.wait_for(w.events().__anext__(), timeout=1.5)
    finally:
        await w.stop()
    assert ev.action == "add"


def _raise_oserror() -> dict[str, _PortIdentity]:
    raise OSError("transient pyserial error")


@pytest.mark.asyncio(loop_scope="session")
async def test_watcher_via_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: dispatch picks WindowsSerialWatcher on win32."""
    import sys

    from isales_telephony.modem_controller.platforms import get_usb_watcher_class

    monkeypatch.setattr(sys, "platform", "win32")
    cls = get_usb_watcher_class()
    assert cls is WindowsSerialWatcher

    w = cls(poll_interval=0.01, scanner=_FakeScanner([{}]))
    await w.start()
    await w.stop()
