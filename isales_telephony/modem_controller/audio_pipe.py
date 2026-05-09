"""ALSA PCM bidirectional pipe with 8 ↔ 16 kHz resampling.

Spec: device-hardware § PCM 音频管道 — 8 kHz mono 16-bit linear PCM on the
modem side, 16 kHz on the engine side.

Design (design.md § Decisions §4 + §5):
- Resampling: scipy.signal.resample_poly (multi-phase polyphase). Up=2,
  Down=1 for upstream; reversed for downstream. Integer 1:2 ratios make
  this fast enough for 8-channel concurrent calls on a modest CPU.
- Jitter buffer: fixed 200 ms ring; under-runs play silence, over-runs
  drop the oldest chunks.

The ALSA backend is loaded lazily so unit tests can run on macOS / inside
CI containers without libasound. Tests inject `Capture` / `Playback`
implementations via the public `AudioPipe` constructor.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from scipy.signal import resample_poly

# Modem-side sample rate (GSM line is fixed at 8 kHz mono 16-bit LE).
GSM_SAMPLE_RATE = 8_000
# Engine / ASR / TTS-side sample rate.
ENGINE_SAMPLE_RATE = 16_000
# 50 ms is the chunk size we exchange with engine — matches stage 5
# real ASR/TTS Provider chunk size.
CHUNK_MS = 50
ENGINE_CHUNK_BYTES = int(ENGINE_SAMPLE_RATE * CHUNK_MS / 1000) * 2  # int16 → 2 bytes
GSM_CHUNK_BYTES = int(GSM_SAMPLE_RATE * CHUNK_MS / 1000) * 2

# Spec: jitter buffer ≤ 200 ms.
JITTER_BUFFER_MS = 200
JITTER_BUFFER_CHUNKS = JITTER_BUFFER_MS // CHUNK_MS  # = 4 chunks


class CaptureBackend(Protocol):
    """Pulls 8 kHz mono int16 PCM from the modem side (ALSA capture)."""

    async def read_chunk(self) -> bytes:
        """Return one chunk of GSM-rate PCM (size GSM_CHUNK_BYTES)."""

    async def close(self) -> None: ...


class PlaybackBackend(Protocol):
    """Pushes 8 kHz mono int16 PCM to the modem side (ALSA playback)."""

    async def write_chunk(self, pcm: bytes) -> None:
        """Submit one GSM-rate PCM chunk for playback."""

    async def close(self) -> None: ...


@dataclass(slots=True)
class AudioPipeStats:
    upstream_chunks: int = 0
    downstream_chunks: int = 0
    underruns: int = 0
    overruns: int = 0


def upsample_8k_to_16k(pcm_8k: bytes) -> bytes:
    """8 kHz int16 LE mono → 16 kHz int16 LE mono."""

    samples = np.frombuffer(pcm_8k, dtype="<i2")
    if samples.size == 0:
        return b""
    upsampled = resample_poly(samples, up=2, down=1).astype("<i2", copy=False)
    return upsampled.tobytes()


def downsample_16k_to_8k(pcm_16k: bytes) -> bytes:
    """16 kHz int16 LE mono → 8 kHz int16 LE mono."""

    samples = np.frombuffer(pcm_16k, dtype="<i2")
    if samples.size == 0:
        return b""
    downsampled = resample_poly(samples, up=1, down=2).astype("<i2", copy=False)
    return downsampled.tobytes()


class JitterBuffer:
    """Fixed-size ring of PCM chunks; underrun → silence, overrun → drop oldest."""

    def __init__(self, *, max_chunks: int = JITTER_BUFFER_CHUNKS) -> None:
        self._buf: deque[bytes] = deque(maxlen=max_chunks)
        self._silence_chunk = b"\x00" * GSM_CHUNK_BYTES
        self.underruns = 0
        self.overruns = 0

    def push(self, chunk: bytes) -> None:
        if len(self._buf) == self._buf.maxlen:
            # deque.append silently drops the oldest entry when at capacity.
            self.overruns += 1
        self._buf.append(chunk)

    def pop(self) -> bytes:
        if not self._buf:
            self.underruns += 1
            return self._silence_chunk
        return self._buf.popleft()

    def __len__(self) -> int:
        return len(self._buf)


class AudioPipe:
    """Glue between modem ALSA streams and the engine-facing PCM hooks.

    Engine-side hooks (``on_inbound_chunk`` / ``send_outbound_chunk``)
    operate on 16 kHz PCM; the pipe handles re-sampling at the boundary.
    """

    def __init__(
        self,
        capture: CaptureBackend,
        playback: PlaybackBackend,
        *,
        on_inbound_chunk: Callable[[bytes], Awaitable[None]],
    ) -> None:
        self._capture = capture
        self._playback = playback
        self._on_inbound = on_inbound_chunk
        self._jitter = JitterBuffer()
        self.stats = AudioPipeStats()

    async def run_capture(self) -> None:
        """Read modem capture → resample to 16 kHz → push to engine."""

        try:
            while True:
                pcm_8k = await self._capture.read_chunk()
                if not pcm_8k:
                    return
                pcm_16k = upsample_8k_to_16k(pcm_8k)
                self.stats.upstream_chunks += 1
                await self._on_inbound(pcm_16k)
        finally:
            await self._capture.close()

    async def run_playback(self) -> None:
        """Pop 8 kHz chunks from the jitter buffer → ALSA playback at 50 ms cadence."""

        import asyncio

        period = CHUNK_MS / 1000
        try:
            while True:
                chunk = self._jitter.pop()
                await self._playback.write_chunk(chunk)
                self.stats.downstream_chunks += 1
                await asyncio.sleep(period)
        finally:
            await self._playback.close()
            self.stats.underruns = self._jitter.underruns
            self.stats.overruns = self._jitter.overruns

    def feed_outbound(self, pcm_16k: bytes) -> None:
        """Engine → modem: down-sample and enqueue for playback."""

        pcm_8k = downsample_16k_to_8k(pcm_16k)
        # Split into 50 ms chunks if upstream sent a larger blob.
        for offset in range(0, len(pcm_8k), GSM_CHUNK_BYTES):
            self._jitter.push(pcm_8k[offset : offset + GSM_CHUNK_BYTES])


__all__ = [
    "AudioPipe",
    "AudioPipeStats",
    "CHUNK_MS",
    "CaptureBackend",
    "ENGINE_CHUNK_BYTES",
    "ENGINE_SAMPLE_RATE",
    "GSM_CHUNK_BYTES",
    "GSM_SAMPLE_RATE",
    "JitterBuffer",
    "PlaybackBackend",
    "downsample_16k_to_8k",
    "upsample_8k_to_16k",
]


# Provide an AsyncIterator alias only when consumers want one — keeps
# import surface clean.
async def chunk_iter(pipe: AudioPipe) -> AsyncIterator[bytes]:  # pragma: no cover
    """Compatibility helper: yield inbound 16 kHz chunks from a pipe."""

    raise NotImplementedError("chunk_iter is for future use; use run_capture instead")
