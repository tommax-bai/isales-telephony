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
import logging
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .base import UdevEvent, UsbDeviceWatcher, UsbDeviceWatcherError

if TYPE_CHECKING:  # pragma: no cover — typing only
    from serial.tools.list_ports_common import ListPortInfo


logger = logging.getLogger(__name__)


# Substring (case-insensitive) used to recognise the sibling audio COM
# port on a SIMCom-class composite USB modem. The SIM7600G-H exposes
# its MI_04 PCM interface with a description like ``"Simcom HS-USB Audio
# 9001 (COM11)"`` — the bare ``"Audio"`` token discriminates from the
# AT / NMEA / Diagnostics / Modem siblings in the same composite.
AUDIO_DESCRIPTION_TOKEN = "audio"


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


def find_audio_serial_path(
    *,
    usb_serial: str | None,
    vid: str | None,
    pid: str | None,
    scanner: Callable[[], Iterable["ListPortInfo"]] | None = None,
) -> str | None:
    """Locate the sibling audio COM port for a USB composite GSM modem.

    Walks the current ``serial.tools.list_ports.comports()`` enumeration
    and returns the device path (e.g. ``"COM11"``) of the sibling port
    that:

    1. has the same USB ``vid`` / ``pid`` as the AT channel,
    2. has the same USB composite ``serial_number`` (i.e. is part of
       the same physical device — Windows assigns one ``iSerialNumber``
       across all interfaces of a composite USB device), AND
    3. has the substring :data:`AUDIO_DESCRIPTION_TOKEN` (case-insensitive)
       in its pyserial ``description``.

    Returns ``None`` if no sibling matches, or if the scan itself raises.
    The caller (audio backend constructor) treats ``None`` as "audio
    backend not constructible for this modem — surface via the usual
    init-failure HardwareAlert path".

    Args:
        usb_serial: USB composite ``serial_number``. ``None`` short-
            circuits to ``None`` (without a serial we cannot disambiguate
            siblings on a host with multiple modems plugged in).
        vid: Lowercase 4-hex VID of the AT channel.
        pid: Lowercase 4-hex PID of the AT channel.
        scanner: Test hook — supply an iterable of ``ListPortInfo``-shaped
            objects (with ``device`` / ``vid`` / ``pid`` / ``serial_number``
            / ``description`` attributes). Defaults to
            ``serial.tools.list_ports.comports``.
    """
    if not usb_serial:
        return None

    if scanner is None:
        try:
            from serial.tools.list_ports import comports  # noqa: PLC0415

            scanner = comports
        except ImportError:  # pragma: no cover — pyserial in main deps
            return None

    try:
        ports = list(scanner())
    except Exception:  # noqa: BLE001 — see docstring
        logger.exception("find_audio_serial_path: pyserial scan failed")
        return None

    token = AUDIO_DESCRIPTION_TOKEN.lower()
    for port in ports:
        port_vid = _vid_pid_hex(getattr(port, "vid", None))
        port_pid = _vid_pid_hex(getattr(port, "pid", None))
        port_serial = getattr(port, "serial_number", None)
        port_desc = getattr(port, "description", None) or ""
        if port_vid != vid or port_pid != pid:
            continue
        if port_serial != usb_serial:
            continue
        if token not in port_desc.lower():
            continue
        return str(port.device)
    return None


# SIMCom HS-USB composite exposes its primary command channel with a
# description like "Simcom HS-USB AT PORT 9001 (COM16)". The bare "at port"
# token discriminates it from the legacy "Modem" RAS-compat channel (which
# also answers AT but does not carry the URC stream we rely on for dialing).
AT_DESCRIPTION_TOKEN = "at port"


class ModemDiscoveryError(RuntimeError):
    """No usable AT / Audio COM port could be auto-discovered.

    Raised (fail-loud) instead of falling back to an env-configured COM
    number: COM numbers re-enumerate on every USB re-plug, so any hardcoded
    value is guaranteed stale and a silent fallback would mask a real
    "modem not attached / driver missing / modem wedged" condition.
    """


def _probe_at_ok(device: str, *, timeout: float = 0.4) -> bool:
    """Open ``device`` and return True iff it answers ``AT`` with ``OK``.

    Synchronous (one-shot startup probe, ~250 ms); vendor-neutral confirmation
    that a description-matched candidate is really a live AT channel.
    """
    import time  # noqa: PLC0415

    import serial  # noqa: PLC0415

    try:
        ser = serial.Serial(device, 115200, timeout=timeout)
    except Exception:  # noqa: BLE001 — port busy / gone → not usable
        return False
    try:
        ser.reset_input_buffer()
        ser.write(b"AT\r\n")
        time.sleep(0.25)
        resp = ser.read(ser.in_waiting or 1).decode("ascii", "replace")
        return "OK" in resp
    except Exception:  # noqa: BLE001
        return False
    finally:
        with contextlib.suppress(Exception):
            ser.close()


def discover_modem_serial_paths(
    *,
    scanner: Callable[[], Iterable["ListPortInfo"]] | None = None,
    at_prober: Callable[[str], bool] | None = None,
) -> tuple[str, str]:
    """Auto-discover ``(at_path, audio_path)`` for the attached USB GSM modem.

    Keys on the **stable USB descriptor** (description token + vid/pid/serial),
    confirmed by an AT probe — NOT on the COM number, which re-enumerates on
    every re-plug. There is deliberately **no env COM-number fallback**: a
    hardcoded COM value is guaranteed stale, so falling back to one would be a
    pointless middle-state that masks real failures. Raises
    :class:`ModemDiscoveryError` (fail-loud) when discovery cannot complete.

    Args:
        scanner: Test hook — iterable of ``ListPortInfo``-shaped objects.
            Defaults to ``serial.tools.list_ports.comports``.
        at_prober: Test hook — ``device -> bool`` AT-OK probe. Defaults to
            :func:`_probe_at_ok`.

    Returns:
        ``(at_path, audio_path)`` device strings, e.g. ``("COM16", "COM17")``.
    """
    if scanner is None:
        try:
            from serial.tools.list_ports import comports  # noqa: PLC0415

            scanner = comports
        except ImportError:  # pragma: no cover — pyserial in main deps
            raise ModemDiscoveryError("pyserial not available for COM scan") from None
    if at_prober is None:
        at_prober = _probe_at_ok

    try:
        ports = list(scanner())
    except Exception as exc:  # noqa: BLE001
        raise ModemDiscoveryError(f"serial port scan failed: {exc}") from exc

    at_token = AT_DESCRIPTION_TOKEN
    at_candidates = [
        p for p in ports
        if at_token in (getattr(p, "description", None) or "").lower()
    ]
    if not at_candidates:
        raise ModemDiscoveryError(
            "no AT-command port found (no COM description contains "
            f"{AT_DESCRIPTION_TOKEN!r}); is the USB GSM modem plugged in and "
            "its driver installed?"
        )

    at_port = next((p for p in at_candidates if at_prober(p.device)), None)
    if at_port is None:
        raise ModemDiscoveryError(
            f"AT-port candidate(s) {[p.device for p in at_candidates]} found but "
            "none answered 'AT' with 'OK' — modem may be wedged (try re-plugging "
            "the USB cable)"
        )

    vid = _vid_pid_hex(getattr(at_port, "vid", None))
    pid = _vid_pid_hex(getattr(at_port, "pid", None))
    usb_serial = getattr(at_port, "serial_number", None)
    audio_path = find_audio_serial_path(
        usb_serial=usb_serial, vid=vid, pid=pid, scanner=lambda: ports,
    )
    if not audio_path:
        raise ModemDiscoveryError(
            f"AT port {at_port.device} found but no sibling Audio port "
            f"(same vid/pid/serial + description containing "
            f"{AUDIO_DESCRIPTION_TOKEN!r}); check the SIMCom driver install"
        )
    return str(at_port.device), audio_path


__all__ = [
    "AT_DESCRIPTION_TOKEN",
    "AUDIO_DESCRIPTION_TOKEN",
    "ModemDiscoveryError",
    "WindowsSerialWatcher",
    "discover_modem_serial_paths",
    "find_audio_serial_path",
]
