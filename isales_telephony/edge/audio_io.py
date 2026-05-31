"""Capture / playback pump coroutines between modem audio backends and
the per-call PCM ring buffers consumed / produced by :class:`AudioBridge`.

Spec: arch-cloud-edge-split / device-hardware § Requirement: 音频环形
buffer (上行 / 下行) — 上行 PCM 不再走 Unix socket, 直接进同进程
环形 buffer; 下行 PCM 反向。

These are tiny — the heavy lifting (resampling, RTC SDK plumbing, drop-
oldest semantics) lives in :mod:`isales_telephony.audio_bridge`. The
job of this module is to keep the capture / playback hardware threads
"unbothered": one task reads 20 ms chunks from the modem mic and posts
them to the upstream ring; another pulls chunks off the downstream ring
and writes them to the modem speaker. Both tasks exit when their
backend yields EOF (empty bytes) or the ring is closed.

Failure modes:

- ``CaptureBackend.read_chunk`` returning ``b""`` is treated as end-of-
  stream (matches the existing ALSA / Core Audio backends' contract).
- Ring buffer overflow on the upstream side is handled inside
  :class:`PcmRingBuffer.put` (drop-oldest); the pump just keeps writing.
- Ring buffer EOF on the downstream side raises
  :class:`StopAsyncIteration` from :meth:`PcmRingBuffer.get`; the pump
  exits cleanly so the per-call task group can shut down.
"""

from __future__ import annotations

import asyncio
import logging

from isales_telephony.audio_bridge.ring_buffer import PcmRingBuffer
from isales_telephony.modem_controller.audio_pipe import (
    CaptureBackend,
    PlaybackBackend,
)

logger = logging.getLogger(__name__)


async def run_capture_pump(
    capture: CaptureBackend,
    upstream: PcmRingBuffer,
) -> None:
    """Drain ``capture`` into ``upstream`` until EOF or cancellation.

    The ring buffer accepts every chunk; oversize chunks and overflow
    are logged inside :meth:`PcmRingBuffer.put`. We deliberately do NOT
    close ``capture`` on exit — the orchestrator owns its lifecycle so
    one backend can survive across multiple calls.
    """
    try:
        while True:
            chunk = await capture.read_chunk()
            if not chunk:
                # EOF from the backend — exit cleanly.
                logger.info("capture_pump_eof")
                return
            await upstream.put(chunk)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — never let the pump kill the process
        logger.exception("capture_pump_unexpected_error")
        raise


async def run_playback_pump(
    downstream: PcmRingBuffer,
    playback: PlaybackBackend,
) -> None:
    """Pull from ``downstream`` and write to ``playback`` until close.

    Accumulates chunks to at least MIN_WRITE_BYTES before writing to the
    modem serial port. SIM7600 expects 20ms frames = 320 bytes (8kHz
    mono int16). Writing less than a full frame may block the USB CDC
    endpoint indefinitely.
    """
    # SIM7600 native frame: 160 samples × 2 bytes = 320 bytes (20ms @ 8kHz)
    MIN_WRITE_BYTES = 320
    _pb_count = 0
    _accum = bytearray()
    try:
        while True:
            try:
                chunk = await downstream.get()
            except StopAsyncIteration:
                logger.info("playback_pump_ring_closed")
                return
            if not chunk:
                continue
            _accum.extend(chunk)
            # Flush when we have at least one full modem frame
            while len(_accum) >= MIN_WRITE_BYTES:
                frame = bytes(_accum[:MIN_WRITE_BYTES])
                del _accum[:MIN_WRITE_BYTES]
                _pb_count += 1
                if _pb_count <= 3 or _pb_count % 200 == 0:
                    logger.info(
                        "playback_pump_write n=%d bytes=%d",
                        _pb_count, len(frame),
                    )
                await playback.write_chunk(frame)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("playback_pump_unexpected_error")
        raise


__all__ = ["run_capture_pump", "run_playback_pump"]
