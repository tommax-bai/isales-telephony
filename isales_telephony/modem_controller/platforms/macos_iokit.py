"""macOS USB device watcher (polling-based).

Despite the file name, this module does NOT use IOKit notifications
directly: ``pyobjc-framework-IOKit`` is not published as a separate
package on PyPI, and writing ctypes against the IOKit C ABI is heavier
than the use case warrants. Instead, the macOS impl polls
``pyserial.tools.list_ports.comports()`` at 1 Hz and emits
:class:`UdevEvent` based on the diff between successive scans.

Latency analysis: GSM modems take 3–5 s to become usable after
insertion (USB enumeration → kernel modem driver attach → modem
firmware ready), so a 1 Hz polling cadence adds at most ~1 s on top —
negligible for a modem-controller daemon. The file name stays
``macos_iokit.py`` for symmetry with ``linux_udev.py`` and to leave
room for a future ctypes-IOKit notification port upgrade without
renaming.

Diff semantics (per :class:`UdevEvent`):

- A device that appears in scan N+1 but not in scan N → ``action="add"``
- A device that appears in scan N but not in scan N+1 → ``action="remove"``
- A device whose VID/PID changed (rare; mostly composite-device modes)
  is reported as remove-then-add of the new identity

Vendor / product IDs come from ``pyserial.tools.list_ports.ListPortInfo``:
``vid`` / ``pid`` are integers; we render them as 4-char lowercase hex
to match the Linux pyudev shape (``ID_VENDOR_ID`` / ``ID_MODEL_ID``).

Devices without a usable VID (USB-CDC ACM exposing no idVendor) are
skipped — the iSales whitelist is VID/PID based by spec, so unfiltered
add events would only generate downstream "ignoring unknown device"
log noise.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .base import UdevEvent, UsbDeviceWatcher, UsbDeviceWatcherError

if TYPE_CHECKING:  # pragma: no cover  - typing only
    from serial.tools.list_ports_common import ListPortInfo


# 1 Hz scan cadence — see latency analysis in module docstring.
DEFAULT_POLL_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class _PortIdentity:
    """Identity tuple used for diffing scan results.

    ``device_node`` alone is unstable (macOS may reassign /dev/cu.usbmodem*
    suffixes across reconnects); we additionally key on (vid, pid, serial)
    so a device replug emits remove+add even if the path repeats.
    """

    device_node: str
    vid: str | None
    pid: str | None
    serial: str | None


def _vid_pid_hex(value: int | None) -> str | None:
    if value is None:
        return None
    return f"{value:04x}"


def _identity_from_port(port: ListPortInfo) -> _PortIdentity:
    return _PortIdentity(
        device_node=port.device,
        vid=_vid_pid_hex(port.vid),
        pid=_vid_pid_hex(port.pid),
        serial=port.serial_number,
    )


def _scan_ports() -> dict[str, _PortIdentity]:
    """Return current set of serial ports keyed by device_node.

    Lazy-imports pyserial so this module is importable on hosts without
    pyserial; the actual call only happens when a watcher is started.
    """
    from serial.tools.list_ports import comports  # noqa: PLC0415

    out: dict[str, _PortIdentity] = {}
    for p in comports():
        # Skip ports without a USB VID — those aren't candidate GSM modems
        # and would only generate unknown-device log noise downstream.
        if p.vid is None:
            continue
        ident = _identity_from_port(p)
        out[p.device] = ident
    return out


def _diff_scans(
    prev: dict[str, _PortIdentity],
    curr: dict[str, _PortIdentity],
) -> list[UdevEvent]:
    """Compute add/remove events from two consecutive scans.

    Order: removes first (so downstream sees a clean disconnect before any
    new identity appears on the same device_node), then adds.
    """
    events: list[UdevEvent] = []

    # Removes: in prev, not in curr (identity-equal); identity changed counts as remove.
    for node, prev_ident in prev.items():
        curr_ident = curr.get(node)
        if curr_ident is None or curr_ident != prev_ident:
            events.append(
                UdevEvent(
                    action="remove",
                    device_node=node,
                    vendor_id=prev_ident.vid,
                    product_id=prev_ident.pid,
                )
            )

    # Adds: in curr, not in prev (identity-equal); identity changed counts as add.
    for node, curr_ident in curr.items():
        prev_ident_for_node = prev.get(node)
        if prev_ident_for_node is None or prev_ident_for_node != curr_ident:
            events.append(
                UdevEvent(
                    action="add",
                    device_node=node,
                    vendor_id=curr_ident.vid,
                    product_id=curr_ident.pid,
                )
            )

    return events


class MacOSIokitWatcher(UsbDeviceWatcher):
    """USB watcher for macOS via pyserial.list_ports polling.

    Lifecycle matches the ABC contract — instantiate, ``start()``, drain
    ``events()``, ``stop()``. Re-using ``start()`` is rejected with a
    clear error.

    Designed so callers can substitute a fake clock / scanner in tests:
    pass a custom ``poll_interval`` and a ``scanner`` callable.
    """

    def __init__(
        self,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        scanner: object | None = None,
    ) -> None:
        # ``scanner`` is typed as ``object`` to avoid leaking implementation
        # details; in tests it's a Callable[[], dict[str, _PortIdentity]].
        self._poll_interval = poll_interval
        self._scanner = scanner if scanner is not None else _scan_ports
        self._queue: asyncio.Queue[UdevEvent] | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    async def start(self) -> None:
        if self._task is not None:
            raise UsbDeviceWatcherError(
                platform="darwin",
                message="MacOSIokitWatcher.start() called twice",
            )
        try:
            # Import-test: fail fast if pyserial unavailable so callers
            # don't get a confusing crash later mid-poll.
            from serial.tools.list_ports import comports as _probe  # noqa: F401, PLC0415
        except ImportError as exc:  # pragma: no cover  - macOS extras guarantee install
            raise UsbDeviceWatcherError(
                platform="darwin",
                message="pyserial not installed; pip install -e '.[macos]'",
            ) from exc

        self._queue = asyncio.Queue()
        self._task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        assert self._queue is not None
        prev: dict[str, _PortIdentity] = {}
        # Seed the watcher with the *current* set silently so callers don't
        # see synthetic "add" events for devices already present at startup
        # — those will be reconciled by modem-controller's reconcile-on-boot
        # path, not by USB events.
        try:
            prev = self._scanner()  # type: ignore[operator]
        except Exception:  # noqa: BLE001  - tolerate flaky pyserial calls
            prev = {}

        while not self._stopped:
            try:
                await asyncio.sleep(self._poll_interval)
                if self._stopped:
                    break
                curr = self._scanner()  # type: ignore[operator]
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001  - logged + retried next tick
                # Silent retry — pyserial occasionally throws OSError when a
                # USB device is mid-disconnect. The next tick is fine.
                continue
            for ev in _diff_scans(prev, curr):
                await self._queue.put(ev)
            prev = curr

    def events(self) -> AsyncIterator[UdevEvent]:
        if self._queue is None:
            raise UsbDeviceWatcherError(
                platform="darwin",
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
        task = self._task
        if task is not None:
            task.cancel()
            # Tear-down must never raise — swallow CancelledError from the
            # poll loop and any final exception from the scanner.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._task = None
