"""Unit tests for capture/playback pumps (isales_telephony.edge.audio_io).

Spec: arch-cloud-edge-split task 7.3 — modem PCM moves between modem
backends and audio-bridge via in-process ring buffers, NOT Unix socket.
"""

from __future__ import annotations

import asyncio

import pytest

from isales_telephony.audio_bridge.ring_buffer import PcmRingBuffer
from isales_telephony.edge.audio_io import run_capture_pump, run_playback_pump


class _FakeCapture:
    """Yields a fixed list of chunks then EOF (empty bytes)."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.closed = False

    async def read_chunk(self) -> bytes:
        if not self._chunks:
            # Mirror real backends: empty bytes == EOF.
            return b""
        return self._chunks.pop(0)

    async def close(self) -> None:
        self.closed = True


class _FakePlayback:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False

    async def write_chunk(self, pcm: bytes) -> None:
        self.written.append(pcm)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_capture_pump_forwards_until_eof() -> None:
    cap = _FakeCapture([b"a" * 320, b"b" * 320, b"c" * 320])
    ring = PcmRingBuffer(capacity_bytes=4096, name="up")
    await run_capture_pump(cap, ring)
    # Pump exits cleanly on EOF without closing capture (HW-scoped backend).
    assert cap.closed is False
    assert len(ring) == 320 * 3


@pytest.mark.asyncio
async def test_capture_pump_overflow_drops_oldest() -> None:
    # Capacity 800 bytes ≈ 2.5 chunks of 320 bytes. Producer pushes 5
    # chunks; PcmRingBuffer drops the oldest two as new ones arrive.
    cap = _FakeCapture([b"a" * 320, b"b" * 320, b"c" * 320, b"d" * 320, b"e" * 320])
    ring = PcmRingBuffer(capacity_bytes=800, name="up")
    await run_capture_pump(cap, ring)
    # Latest chunks survive: c, d, e (the 3 newest that fit; oldest pushed out).
    assert len(ring) <= 800
    assert ring.dropped_chunks > 0


@pytest.mark.asyncio
async def test_playback_pump_drains_until_ring_closed() -> None:
    ring = PcmRingBuffer(capacity_bytes=4096, name="down")
    play = _FakePlayback()

    await ring.put(b"x" * 160)
    await ring.put(b"y" * 160)

    pump = asyncio.create_task(run_playback_pump(ring, play))
    # The pump coalesces to one full 320-byte modem frame (20ms @ 8kHz)
    # before writing — partial writes (<320B) block the SIM7600 USB CDC
    # endpoint — so two 160-byte chunks become a single 320-byte write.
    for _ in range(50):
        if len(play.written) == 1:
            break
        await asyncio.sleep(0.01)
    assert len(play.written) == 1
    assert play.written[0] == b"x" * 160 + b"y" * 160

    # Close the ring → pump exits, playback NOT closed (HW-scoped).
    await ring.close()
    await asyncio.wait_for(pump, timeout=1.0)
    assert play.closed is False


@pytest.mark.asyncio
async def test_playback_pump_cancellation() -> None:
    ring = PcmRingBuffer(capacity_bytes=4096, name="down")
    play = _FakePlayback()
    pump = asyncio.create_task(run_playback_pump(ring, play))
    await asyncio.sleep(0.01)
    pump.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pump
