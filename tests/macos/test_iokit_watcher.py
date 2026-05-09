"""macOS USB watcher tests — diff-from-polling semantics.

These tests inject a fake scanner so they run on Linux too (no pyserial
or USB hardware required). The ABC contract is platform-neutral; the
``MacOSIokitWatcher`` class can be exercised anywhere it can be imported.

Real macOS hot-plug behavior (insert/remove a USB device, observe events
within ~1 s) is verified manually on a Mac mini during PR #12 hardware
acceptance — this module covers the polling diff logic itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from isales_telephony.modem_controller.platforms.base import UdevEvent
from isales_telephony.modem_controller.platforms.macos_iokit import (
    MacOSIokitWatcher,
    _diff_scans,
    _PortIdentity,
)


def _ident(node: str, vid: str = "2c7c", pid: str = "0125", serial: str = "S1") -> _PortIdentity:
    return _PortIdentity(device_node=node, vid=vid, pid=pid, serial=serial)


# ---------------------------------------------------------------------- diff


def test_diff_emits_add_for_newly_appeared_port() -> None:
    prev: dict[str, _PortIdentity] = {}
    curr = {"/dev/cu.usbmodem001": _ident("/dev/cu.usbmodem001")}
    events = _diff_scans(prev, curr)
    assert events == [
        UdevEvent(
            action="add",
            device_node="/dev/cu.usbmodem001",
            vendor_id="2c7c",
            product_id="0125",
        )
    ]


def test_diff_emits_remove_for_disappeared_port() -> None:
    prev = {"/dev/cu.usbmodem002": _ident("/dev/cu.usbmodem002")}
    events = _diff_scans(prev, {})
    assert events == [
        UdevEvent(
            action="remove",
            device_node="/dev/cu.usbmodem002",
            vendor_id="2c7c",
            product_id="0125",
        )
    ]


def test_diff_no_change_emits_nothing() -> None:
    state = {"/dev/cu.usbmodem001": _ident("/dev/cu.usbmodem001")}
    assert _diff_scans(state, state.copy()) == []


def test_diff_identity_change_on_same_path_emits_remove_then_add() -> None:
    """Replug with a different VID/PID on the same device_node — emit
    remove of old identity, then add of new identity. Order matters so
    downstream sees a clean disconnect before the new device.
    """
    prev = {"/dev/cu.usbmodem001": _ident("/dev/cu.usbmodem001", vid="aaaa", pid="bbbb")}
    curr = {"/dev/cu.usbmodem001": _ident("/dev/cu.usbmodem001", vid="2c7c", pid="0125")}
    events = _diff_scans(prev, curr)
    assert [e.action for e in events] == ["remove", "add"]
    assert events[0].vendor_id == "aaaa"
    assert events[1].vendor_id == "2c7c"


def test_diff_three_devices_one_swapped() -> None:
    prev = {
        "/dev/cu.usbmodem001": _ident("/dev/cu.usbmodem001", serial="A"),
        "/dev/cu.usbmodem002": _ident("/dev/cu.usbmodem002", serial="B"),
        "/dev/cu.usbmodem003": _ident("/dev/cu.usbmodem003", serial="C"),
    }
    curr = {
        "/dev/cu.usbmodem001": _ident("/dev/cu.usbmodem001", serial="A"),  # unchanged
        "/dev/cu.usbmodem002": _ident("/dev/cu.usbmodem002", serial="X"),  # serial flipped
        "/dev/cu.usbmodem004": _ident("/dev/cu.usbmodem004", serial="D"),  # new device
    }
    events = _diff_scans(prev, curr)
    actions_by_node = sorted((e.device_node, e.action) for e in events)
    assert actions_by_node == [
        ("/dev/cu.usbmodem002", "add"),
        ("/dev/cu.usbmodem002", "remove"),
        ("/dev/cu.usbmodem003", "remove"),
        ("/dev/cu.usbmodem004", "add"),
    ]


# ---------------------------------------------------------- watcher lifecycle


class _FakeScanner:
    """Programmable scanner — drives the watcher's poll loop."""

    def __init__(self, scans: list[dict[str, _PortIdentity]]) -> None:
        self._scans = scans
        self._idx = 0

    def __call__(self) -> dict[str, _PortIdentity]:
        # Hold the last scan once exhausted; the watcher's loop runs forever.
        if self._idx >= len(self._scans):
            return self._scans[-1] if self._scans else {}
        out = self._scans[self._idx]
        self._idx += 1
        return out


@pytest.mark.asyncio(loop_scope="session")
async def test_watcher_emits_add_then_remove_in_order() -> None:
    scans = [
        {},  # seed (silent)
        {"/dev/cu.usbmodem001": _ident("/dev/cu.usbmodem001")},  # add
        {},  # remove
    ]
    w = MacOSIokitWatcher(poll_interval=0.01, scanner=_FakeScanner(scans))
    await w.start()
    try:
        events_iter = w.events()
        # Pull two events with a generous timeout (10× poll interval each).
        async def _next() -> UdevEvent:
            return await asyncio.wait_for(events_iter.__anext__(), timeout=1.0)

        ev1 = await _next()
        ev2 = await _next()
    finally:
        await w.stop()

    assert ev1.action == "add"
    assert ev1.device_node == "/dev/cu.usbmodem001"
    assert ev2.action == "remove"
    assert ev2.device_node == "/dev/cu.usbmodem001"


@pytest.mark.asyncio(loop_scope="session")
async def test_watcher_seed_does_not_emit_for_already_present_devices() -> None:
    """Devices visible at startup MUST NOT generate synthetic add events —
    those are reconciled by modem-controller's boot path, not USB watcher.
    """
    scans = [
        {"/dev/cu.usbmodem001": _ident("/dev/cu.usbmodem001")},  # seed
        {"/dev/cu.usbmodem001": _ident("/dev/cu.usbmodem001")},  # unchanged
        {},  # remove — this MUST emit
    ]
    w = MacOSIokitWatcher(poll_interval=0.01, scanner=_FakeScanner(scans))
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

    w = MacOSIokitWatcher(poll_interval=0.01, scanner=_FakeScanner([{}]))
    await w.start()
    try:
        with pytest.raises(UsbDeviceWatcherError):
            await w.start()
    finally:
        await w.stop()


@pytest.mark.asyncio(loop_scope="session")
async def test_watcher_stop_is_safe_after_failed_start() -> None:
    """stop() MUST be safe even if start() never ran — defensive teardown
    is part of the ABC contract.
    """
    w = MacOSIokitWatcher(poll_interval=0.01, scanner=_FakeScanner([{}]))
    await w.stop()  # no exception


@pytest.mark.asyncio(loop_scope="session")
async def test_watcher_tolerates_scanner_exception() -> None:
    """Pyserial occasionally throws OSError mid-disconnect; watcher MUST NOT
    crash, it MUST retry on next tick.
    """
    seq: list[Callable[[], dict[str, _PortIdentity]]] = [
        lambda: {},  # seed
        _raise_oserror,  # transient
        lambda: {"/dev/cu.usbmodem001": _ident("/dev/cu.usbmodem001")},
    ]
    state = {"i": 0}

    def scan() -> dict[str, _PortIdentity]:
        i = state["i"]
        state["i"] = min(i + 1, len(seq) - 1)
        result = seq[i]()
        return result

    w = MacOSIokitWatcher(poll_interval=0.01, scanner=scan)
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
    """End-to-end: dispatch picks MacOSIokitWatcher, instance is usable."""
    import sys

    from isales_telephony.modem_controller.platforms import get_usb_watcher_class

    monkeypatch.setattr(sys, "platform", "darwin")
    cls = get_usb_watcher_class()
    assert cls is MacOSIokitWatcher

    # Now instantiate via the class — confirms the constructor accepts
    # default args after PR #3 lifted the NotImplementedError stub.
    w = cls(poll_interval=0.01, scanner=_FakeScanner([{}]))
    await w.start()
    await w.stop()
