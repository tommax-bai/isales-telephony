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
    _cap_count = 0
    _cap_bytes = 0
    try:
        while True:
            chunk = await capture.read_chunk()
            if not chunk:
                # EOF from the backend — exit cleanly.
                logger.info("capture_pump_eof n=%d bytes=%d", _cap_count, _cap_bytes)
                return
            _cap_count += 1
            _cap_bytes += len(chunk)
            if _cap_count <= 3 or _cap_count % 50 == 0:
                # max abs int16 sample — distinguishes true silence (~0) from
                # captured far-end voice (thousands). If this stays ~0 while
                # the caller speaks, the modem isn't routing GSM-RX audio into
                # the USB PCM downlink (a modem audio-path config issue).
                try:
                    import audioop  # noqa: PLC0415
                    amp = audioop.max(chunk, 2)
                except Exception:  # noqa: BLE001
                    amp = -1
                logger.info(
                    "capture_pump_read n=%d bytes=%d last=%d amp=%d",
                    _cap_count, _cap_bytes, len(chunk), amp,
                )
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


async def run_duplex_pump(
    capture: CaptureBackend,
    playback: PlaybackBackend,
    upstream: PcmRingBuffer,
    downstream: PcmRingBuffer,
) -> None:
    """Single-task FULL-DUPLEX pump for a shared serial handle (Windows).

    Windows cannot open the SIM7600 audio COM port twice, so capture (read)
    and playback (write) share ONE pyserial handle. Running them as two
    independent concurrent pumps (separate threads) corrupts the read — the
    write collides with it and the captured far-end audio comes back as
    silence/garbage (2026-05-31: pure-read amp ~4000 while the caller speaks,
    but concurrent read+write amp ~0). So on Windows both directions are driven
    from ONE task in lockstep: read one frame (the blocking read paces the loop
    at the modem's 8 kHz clock), then write one frame — never both at once.

    macOS/Linux keep the separate capture+playback pumps (independent in/out
    devices, no shared handle).
    """
    FRAME = 320  # 20 ms @ 8 kHz int16 mono
    SILENCE = b"\x00" * FRAME
    pending = bytearray()  # downstream (engine/AI voice) accumulator
    _n = 0
    try:
        while True:
            # 1. READ one uplink frame from the modem (blocks ~20 ms → paces).
            chunk = await capture.read_chunk()
            if not chunk:
                logger.info("duplex_pump_eof n=%d", _n)
                return
            await upstream.put(chunk)
            # 2. Drain whatever downstream (AI voice) is ready — non-blocking.
            while True:
                out = downstream.get_nowait()
                if out is None:
                    break
                pending.extend(out)
            # 3. WRITE one frame (AI voice, or silence to keep the PCM clock fed
            #    so the modem keeps streaming the downlink we read in step 1).
            if len(pending) >= FRAME:
                frame = bytes(pending[:FRAME])
                del pending[:FRAME]
            else:
                frame = SILENCE
            await playback.write_chunk(frame)
            _n += 1
            if _n <= 3 or _n % 100 == 0:
                logger.info(
                    "duplex_pump n=%d up_last=%d down_pending=%d wrote=%d",
                    _n, len(chunk), len(pending), len(frame),
                )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("duplex_pump_unexpected_error")
        raise


__all__ = ["run_capture_pump", "run_duplex_pump", "run_playback_pump"]
