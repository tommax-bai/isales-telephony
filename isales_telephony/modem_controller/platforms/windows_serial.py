"""Windows USB device watcher (pyserial polling).

Spec: windows-client-core / device-hardware § Windows backend — "D1
SHALL use pyserial polling for USB device add/remove on Windows".

Implementation mirrors :mod:`macos_iokit` because the pyserial library
is genuinely cross-platform: ``serial.tools.list_ports.comports()``
enumerates Windows ``COM*`` ports the same way it enumerates
``/dev/cu.usbmodem*`` on macOS. The two files stay separate (instead
of a shared "polling watcher" base class) to keep platform-specific
quirks isolated as they accumulate — D1 design Decision 2 leaves room
for a future ``WM_DEVICECHANGE`` triggered re-scan, which would be
Windows-only.

Windows-specific notes:

- ``port.device`` is e.g. ``"COM3"`` (no path prefix). Downstream code
  passes this string straight to pyserial / pyserial-asyncio, which
  knows how to translate ``COMn`` to the Win32 ``\\\\.\\COMn`` path.
- Multi-mode USB GSM modems (Huawei E-series, some Quectel models)
  enumerate as MULTIPLE COM ports per physical device — one AT
  channel + one PCM channel + one debug channel. The watcher emits an
  ``add`` event for *each* port; selecting the AT channel happens one
  layer up via ``ISALES_MODEM_SERIAL_PATH`` env or AT-probe (D1
  task 3.3).
- VID/PID are reported the same way as on macOS by pyserial — we
  render them as 4-char lowercase hex for whitelist comparison.

Latency analysis: identical to macOS — GSM modems take 3–5 s to be
usable after insertion, 1 Hz polling adds ≤ 1 s of detection lag.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .base import UdevEvent, UsbDeviceWatcher, UsbDeviceWatcherError

if TYPE_CHECKING:  # pragma: no cover — typing only
    from serial.tools.list_ports_common import ListPortInfo


# 1 Hz scan cadence — see latency analysis in module docstring.
DEFAULT_POLL_INTERVAL_SECONDS = 1.0


@dataclass(frozen=True)
class _PortIdentity:
    """Identity tuple used for diffing scan results.

    ``device_node`` alone is unstable (Windows reassigns ``COMn``
    numbers across reconnects, especially when a different USB hub
    port is used); we additionally key on (vid, pid, serial) so a
    device replug emits remove+add even if Windows happens to reuse
    the same ``COMn``.
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
        # Skip ports without a USB VID — those aren't candidate GSM
        # modems (built-in serial chips, Bluetooth virtual ports, etc.)
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

    Order: removes first (so downstream sees a clean disconnect before
    any new identity appears on the same device_node), then adds.
    """
    events: list[UdevEvent] = []

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


class WindowsSerialWatcher(UsbDeviceWatcher):
    """USB watcher for Windows via pyserial.list_ports polling.

    Lifecycle matches the ABC contract — instantiate, ``start()``,
    drain ``events()``, ``stop()``. Re-using ``start()`` is rejected
    with a clear error.

    Designed so callers can substitute a fake clock / scanner in
    tests: pass a custom ``poll_interval`` and a ``scanner`` callable.
    """

    def __init__(
        self,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        scanner: object | None = None,
    ) -> None:
        # ``scanner`` is typed as ``object`` to avoid leaking
        # implementation details; in tests it's
        # Callable[[], dict[str, _PortIdentity]].
        self._poll_interval = poll_interval
        self._scanner = scanner if scanner is not None else _scan_ports
        self._queue: asyncio.Queue[UdevEvent] | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    async def start(self) -> None:
        if self._task is not None:
            raise UsbDeviceWatcherError(
                platform="win32",
                message="WindowsSerialWatcher.start() called twice",
            )
        try:
            # Import-test: fail fast if pyserial unavailable so callers
            # don't get a confusing crash later mid-poll.
            from serial.tools.list_ports import comports as _probe  # noqa: F401, PLC0415
        except ImportError as exc:  # pragma: no cover — pyserial is in main deps
            raise UsbDeviceWatcherError(
                platform="win32",
                message="pyserial not installed",
            ) from exc

        self._queue = asyncio.Queue()
        self._task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        assert self._queue is not None
        prev: dict[str, _PortIdentity] = {}
        # Seed the watcher with the *current* set silently so callers
        # don't see synthetic "add" events for devices already present
        # at startup — those will be reconciled by modem-controller's
        # reconcile-on-boot path, not by USB events.
        try:
            prev = self._scanner()  # type: ignore[operator]
        except Exception:  # noqa: BLE001 — tolerate flaky pyserial calls
            prev = {}

        while not self._stopped:
            try:
                await asyncio.sleep(self._poll_interval)
                if self._stopped:
                    break
                curr = self._scanner()  # type: ignore[operator]
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — logged + retried next tick
                # Silent retry — pyserial occasionally throws OSError
                # when a USB device is mid-disconnect. The next tick is
                # fine.
                continue
            for ev in _diff_scans(prev, curr):
                await self._queue.put(ev)
            prev = curr

    def events(self) -> AsyncIterator[UdevEvent]:
        if self._queue is None:
            raise UsbDeviceWatcherError(
                platform="win32",
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
            # Tear-down must never raise — swallow CancelledError from
            # the poll loop and any final exception from the scanner.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._task = None


__all__ = ["WindowsSerialWatcher"]
