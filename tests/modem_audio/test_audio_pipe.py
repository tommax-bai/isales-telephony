"""Audio pipe + resampling tests.

The resampler is verified by checking that a 1 kHz sine remains the
dominant frequency after up/down conversion. The jitter buffer is
verified independently with synthetic chunks.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from isales_telephony.modem_controller.audio_pipe import (
    AudioPipe,
    GSM_CHUNK_BYTES,
    GSM_SAMPLE_RATE,
    ENGINE_SAMPLE_RATE,
    JitterBuffer,
    downsample_16k_to_8k,
    upsample_8k_to_16k,
)


def _sine(rate: int, freq: float, duration_s: float) -> bytes:
    n = int(rate * duration_s)
    t = np.arange(n) / rate
    samples = (np.sin(2 * np.pi * freq * t) * 0.5 * 32767).astype("<i2")
    return samples.tobytes()


def _dominant_freq(pcm: bytes, rate: int) -> float:
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    fft = np.abs(np.fft.rfft(samples))
    freq = np.fft.rfftfreq(samples.size, d=1 / rate)
    return float(freq[int(np.argmax(fft))])


def test_upsample_preserves_dominant_frequency() -> None:
    src = _sine(GSM_SAMPLE_RATE, 1000.0, 0.5)
    dst = upsample_8k_to_16k(src)
    assert len(dst) == len(src) * 2
    detected = _dominant_freq(dst, ENGINE_SAMPLE_RATE)
    assert abs(detected - 1000.0) < 5.0


def test_downsample_preserves_dominant_frequency() -> None:
    src = _sine(ENGINE_SAMPLE_RATE, 1500.0, 0.5)
    dst = downsample_16k_to_8k(src)
    assert len(dst) == len(src) // 2
    detected = _dominant_freq(dst, GSM_SAMPLE_RATE)
    assert abs(detected - 1500.0) < 5.0


def test_upsample_handles_empty_input() -> None:
    assert upsample_8k_to_16k(b"") == b""
    assert downsample_16k_to_8k(b"") == b""


def test_jitter_buffer_returns_silence_on_underrun() -> None:
    jb = JitterBuffer(max_chunks=4)
    chunk = jb.pop()
    assert chunk == b"\x00" * GSM_CHUNK_BYTES
    assert jb.underruns == 1


def test_jitter_buffer_drops_oldest_on_overrun() -> None:
    jb = JitterBuffer(max_chunks=2)
    jb.push(b"\x01" * GSM_CHUNK_BYTES)
    jb.push(b"\x02" * GSM_CHUNK_BYTES)
    jb.push(b"\x03" * GSM_CHUNK_BYTES)
    assert jb.overruns == 1
    # Oldest dropped, FIFO order preserved.
    assert jb.pop() == b"\x02" * GSM_CHUNK_BYTES
    assert jb.pop() == b"\x03" * GSM_CHUNK_BYTES


class _FakeCapture:
    """Yields a fixed list of GSM-rate chunks then EOF."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.closed = False

    async def read_chunk(self) -> bytes:
        if not self.chunks:
            return b""
        return self.chunks.pop(0)

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
async def test_run_capture_resamples_to_16k_and_invokes_inbound_hook() -> None:
    src = _sine(GSM_SAMPLE_RATE, 1000.0, 0.05)  # 50 ms
    cap = _FakeCapture([src])
    pb = _FakePlayback()
    seen: list[bytes] = []

    async def on_inbound(chunk: bytes) -> None:
        seen.append(chunk)

    pipe = AudioPipe(cap, pb, on_inbound_chunk=on_inbound)
    await pipe.run_capture()
    assert len(seen) == 1
    assert len(seen[0]) == len(src) * 2  # upsampled
    assert pipe.stats.upstream_chunks == 1
    assert cap.closed is True


@pytest.mark.asyncio
async def test_feed_outbound_downsamples_and_enqueues_chunks() -> None:
    cap = _FakeCapture([])
    pb = _FakePlayback()
    pipe = AudioPipe(cap, pb, on_inbound_chunk=lambda _b: asyncio.sleep(0))
    # 100 ms of 16 kHz audio → 50 ms of 8 kHz audio per spec; that's a
    # single GSM chunk (CHUNK_MS=50 ms).
    src = _sine(ENGINE_SAMPLE_RATE, 800.0, 0.1)
    pipe.feed_outbound(src)
    # The buffer should now hold exactly two GSM chunks.
    expected_8k_bytes = len(src) // 2
    assert expected_8k_bytes == 2 * GSM_CHUNK_BYTES
    assert len(pipe._jitter) == 2  # type: ignore[attr-defined]
