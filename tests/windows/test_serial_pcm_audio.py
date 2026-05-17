"""Tests for the SerialPcm-over-COM Windows audio backend.

Spec: windows-client-core / device-hardware § "SerialPcm-over-COM 音频
backend" + "音频帧格式" + "PCM 通道按 SIMCom AT 协议启停".

These tests inject a fake ``Serial`` via ``_set_serial_for_test`` so we
don't need pyserial or a real COM port on CI hosts. Real-hardware
coverage is in tasks.md §9 PoC week 1.
"""

from __future__ import annotations

import sys

import pytest

from isales_telephony.modem_controller.audio import (
    get_capture_class,
    get_playback_class,
)
from isales_telephony.modem_controller.audio.windows_serial_pcm import (
    DEFAULT_CHUNK_BYTES,
    WindowsSerialPcmCapture,
    WindowsSerialPcmPlayback,
)
from isales_telephony.modem_controller.audio_pipe import (
    CaptureBackend,
    PlaybackBackend,
)


# ---------- Fakes -----------------------------------------------------------


class _FakeSerial:
    """Records ``read`` / ``write`` calls; programmable response buffer.

    Mirrors the surface ``pyserial.Serial`` exposes: ``read(n)`` returns
    up to ``n`` bytes from a FIFO that the test fills; ``write(buf)``
    appends to ``written`` and returns the number of bytes accepted.
    """

    def __init__(self, *, read_response: bytes = b"") -> None:
        self._read_buf = bytearray(read_response)
        self.written: bytearray = bytearray()
        self.closed = False
        self.read_calls: list[int] = []

    def read(self, n: int) -> bytes:
        self.read_calls.append(n)
        if not self._read_buf:
            # pyserial returns whatever accumulated within ``timeout``;
            # for CPCMREG=0 windows that may be 0 bytes (partial).
            return b""
        chunk = bytes(self._read_buf[:n])
        del self._read_buf[:n]
        return chunk

    def write(self, buf: bytes) -> int:
        self.written.extend(buf)
        return len(buf)

    def close(self) -> None:
        self.closed = True

    # Pre-seed convenience: tests can refill the read buffer mid-test
    # to simulate CPCMREG=1 starting the byte stream.
    def feed(self, data: bytes) -> None:
        self._read_buf.extend(data)


# ---------- (a) read_chunk in timeout window returns partial bytes ---------


@pytest.mark.asyncio
async def test_read_chunk_returns_partial_during_cpcmreg_disabled() -> None:
    """Between calls (CPCMREG=0), the modem stops emitting PCM. pyserial's
    timeout-bounded read MUST return what accumulated (here: nothing)
    without raising — the orchestrator's pump loop just calls again."""
    cap = WindowsSerialPcmCapture("COM11")
    fake = _FakeSerial(read_response=b"")
    cap._set_serial_for_test(fake)

    out = await cap.read_chunk()
    assert out == b""
    assert fake.read_calls == [DEFAULT_CHUNK_BYTES]


# ---------- (b) read_chunk returns full 320 bytes after PCM starts ----------


@pytest.mark.asyncio
async def test_read_chunk_returns_full_frame_after_cpcmreg_enabled() -> None:
    """Once AT+CPCMREG=1 succeeds the modem emits one 20 ms frame
    (160 samples × 2 bytes int16) per read window. pyserial returns
    exactly ``chunk_bytes``."""
    cap = WindowsSerialPcmCapture("COM11")
    payload = bytes(range(256)) * (DEFAULT_CHUNK_BYTES // 256 + 1)
    payload = payload[:DEFAULT_CHUNK_BYTES]
    fake = _FakeSerial(read_response=payload)
    cap._set_serial_for_test(fake)

    out = await cap.read_chunk()
    assert out == payload
    assert len(out) == DEFAULT_CHUNK_BYTES  # 320 bytes = 8 kHz × 20 ms × int16


# ---------- (c) write_chunk delivers bytes ---------------------------------


@pytest.mark.asyncio
async def test_write_chunk_writes_pcm_bytes_to_serial() -> None:
    pb = WindowsSerialPcmPlayback("COM11")
    fake = _FakeSerial()
    pb._set_serial_for_test(fake)

    payload = b"\xaa\xbb" * 160  # 320-byte 20 ms frame
    await pb.write_chunk(payload)
    assert bytes(fake.written) == payload


# ---------- (d) close idempotent -------------------------------------------


@pytest.mark.asyncio
async def test_close_is_idempotent_on_both_directions() -> None:
    cap = WindowsSerialPcmCapture("COM11")
    fake = _FakeSerial()
    cap._set_serial_for_test(fake)

    await cap.close()
    assert fake.closed is True

    # Second close must not raise + must not re-call the fake.
    fake.closed = False
    await cap.close()
    assert fake.closed is False  # not re-closed


# ---------- (e) dispatch picks WindowsSerialPcm on win32 -------------------


@pytest.mark.skipif(sys.platform != "win32", reason="windows-only dispatch")
def test_get_capture_class_returns_windows_serial_pcm() -> None:
    assert get_capture_class() is WindowsSerialPcmCapture


@pytest.mark.skipif(sys.platform != "win32", reason="windows-only dispatch")
def test_get_playback_class_returns_windows_serial_pcm() -> None:
    assert get_playback_class() is WindowsSerialPcmPlayback


# ---------- (f) Protocol shape ---------------------------------------------


def test_capture_satisfies_capture_backend_protocol() -> None:
    """``WindowsSerialPcmCapture`` MUST be a structural ``CaptureBackend``
    (read_chunk + close). The Protocol is runtime-checkable indirectly
    via attribute presence — see audio_pipe.CaptureBackend."""
    cap: CaptureBackend = WindowsSerialPcmCapture("COM11")
    assert hasattr(cap, "read_chunk")
    assert hasattr(cap, "close")


def test_playback_satisfies_playback_backend_protocol() -> None:
    pb: PlaybackBackend = WindowsSerialPcmPlayback("COM11")
    assert hasattr(pb, "write_chunk")
    assert hasattr(pb, "close")
