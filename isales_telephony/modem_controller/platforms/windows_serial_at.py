"""Thread-backed serial transport for the AT control channel on Windows.

``pyserial-asyncio``'s reader is unreliable on Windows: the ProactorEventLoop
has no working ``add_reader`` for serial handles, so it intermittently drops
modem responses. Empirically (2026-05-31) ~80% of edge-daemon launches crashed
on AT init (``AtTimeoutError`` despite the modem replying in <20 ms to a plain
blocking ``pyserial`` read), and even successful starts dropped in-call URCs —
producing 1-second "failed" calls.

So on Windows we feed :class:`AtClient` a reader/writer pair backed by a
dedicated **blocking-pyserial reader thread** (the exact path that proved
reliable during hardware probing). POSIX keeps using ``pyserial-asyncio``.

The returned ``(reader, writer)`` are duck-typed to the subset of
``asyncio.StreamReader`` / ``StreamWriter`` that :class:`AtClient` uses:

- ``await reader.readline()`` → one line incl. trailing ``\\n`` (or ``b""`` EOF)
- ``writer.write(bytes)`` / ``await writer.drain()``
- ``writer.close()`` / ``await writer.wait_closed()``
- ``writer.transport.serial`` → the pyserial ``Serial`` so
  ``create_from_tty``'s fd-based exclusive-lock probe finds ``fd=None`` on
  Windows and no-ops (the OS already enforces exclusive open).
"""

from __future__ import annotations

import asyncio
import logging
import threading

logger = logging.getLogger(__name__)

# Reader-thread blocking read timeout. Short enough to notice the stop flag
# promptly on close; long enough to avoid busy-spinning.
_READ_TIMEOUT_S = 0.1
_READ_CHUNK = 256


class _ThreadedSerialReader:
    """``asyncio.StreamReader``-compatible (``readline`` only)."""

    def __init__(self, queue: "asyncio.Queue[bytes]") -> None:
        self._queue = queue

    async def readline(self) -> bytes:
        return await self._queue.get()


class _Transport:
    def __init__(self, serial_obj: object) -> None:
        self.serial = serial_obj


class _ThreadedSerialWriter:
    """``asyncio.StreamWriter``-compatible subset."""

    def __init__(
        self,
        serial_obj: object,
        stop: threading.Event,
        thread: threading.Thread,
    ) -> None:
        self._serial = serial_obj
        self._stop = stop
        self._thread = thread
        self.transport = _Transport(serial_obj)
        self._closed = False

    def write(self, data: bytes) -> None:
        # AT commands are short (<32 bytes); a synchronous pyserial write is
        # sub-millisecond and never blocks the event loop in practice.
        self._serial.write(data)  # type: ignore[attr-defined]

    async def drain(self) -> None:
        try:
            self._serial.flush()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — best-effort
            pass

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()

    async def wait_closed(self) -> None:
        await asyncio.to_thread(self._thread.join, 2.0)
        try:
            self._serial.close()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — tear-down must never raise
            pass


def _make_reader_thread(
    ser: object,
    queue: "asyncio.Queue[bytes]",
    stop: threading.Event,
    loop: asyncio.AbstractEventLoop,
) -> threading.Thread:
    def _run() -> None:
        buf = bytearray()
        while not stop.is_set():
            try:
                chunk = ser.read(_READ_CHUNK)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                logger.warning("threaded_serial_read_error: %s", exc)
                break
            if not chunk:
                continue
            buf.extend(chunk)
            # Emit complete newline-terminated lines (reassembling lines that
            # span multiple reads), matching StreamReader.readline semantics.
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(buf[: nl + 1])
                del buf[: nl + 1]
                loop.call_soon_threadsafe(queue.put_nowait, line)
        # EOF sentinel so AtClient._read_loop unblocks on close.
        loop.call_soon_threadsafe(queue.put_nowait, b"")

    return threading.Thread(target=_run, name="at_serial_reader", daemon=True)


async def open_threaded_serial(
    url: str, baudrate: int = 115200,
) -> tuple[_ThreadedSerialReader, _ThreadedSerialWriter]:
    """Open ``url`` with blocking pyserial + a reader thread.

    Returns a ``(reader, writer)`` pair compatible with :class:`AtClient`.
    Raises whatever ``serial.Serial(...)`` raises on open failure (the caller
    in ``create_from_tty`` translates it to ``BusyDeviceError`` etc.).
    """
    import serial  # noqa: PLC0415 — lazy, Windows-only path

    loop = asyncio.get_running_loop()
    ser = serial.Serial(port=url, baudrate=baudrate, timeout=_READ_TIMEOUT_S)
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    stop = threading.Event()
    thread = _make_reader_thread(ser, queue, stop, loop)
    thread.start()
    return _ThreadedSerialReader(queue), _ThreadedSerialWriter(ser, stop, thread)


__all__ = ["open_threaded_serial"]
