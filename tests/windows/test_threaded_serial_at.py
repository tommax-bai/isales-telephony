"""Thread-backed AT serial transport (Windows).

Verifies the duck-typed StreamReader/StreamWriter pair feeds AtClient correctly:
complete-line reassembly across reads, writes reach the port, and close emits
the EOF sentinel + tears the thread/port down. Uses a fake Serial so it runs on
any host (no real COM port / Windows required).
"""

from __future__ import annotations

import time

import pytest
import serial

from isales_telephony.modem_controller.platforms import windows_serial_at


class _FakeSerial:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.written = bytearray()
        self.closed = False

    def read(self, _n: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        time.sleep(0.02)  # mimic pyserial read timeout (no data)
        return b""

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_reader_reassembles_lines_across_reads(monkeypatch) -> None:
    # "+CME " and "ERROR: 1\r\n" arrive in separate reads → one line.
    fake = _FakeSerial([b"OK\r\n", b"+CME ", b"ERROR: 1\r\n"])
    monkeypatch.setattr(serial, "Serial", lambda **kw: fake)

    reader, writer = await windows_serial_at.open_threaded_serial("COM_FAKE")
    try:
        assert await reader.readline() == b"OK\r\n"
        assert await reader.readline() == b"+CME ERROR: 1\r\n"
    finally:
        writer.close()
        await writer.wait_closed()
    assert fake.closed is True


@pytest.mark.asyncio
async def test_write_reaches_port(monkeypatch) -> None:
    fake = _FakeSerial([])
    monkeypatch.setattr(serial, "Serial", lambda **kw: fake)

    reader, writer = await windows_serial_at.open_threaded_serial("COM_FAKE")
    try:
        writer.write(b"AT\r")
        await writer.drain()
        assert bytes(fake.written) == b"AT\r"
        # transport.serial exposed for the fd-based exclusive-lock probe.
        assert writer.transport.serial is fake
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_close_emits_eof_sentinel(monkeypatch) -> None:
    fake = _FakeSerial([])
    monkeypatch.setattr(serial, "Serial", lambda **kw: fake)

    reader, writer = await windows_serial_at.open_threaded_serial("COM_FAKE")
    writer.close()
    await writer.wait_closed()
    # AtClient._read_loop relies on b"" to detect EOF after close.
    assert await reader.readline() == b""
