"""Async PCM ring buffer for modem ↔ audio-bridge cross-task plumbing.

Spec: device-hardware § Requirement: IPC 帧格式 — "音频环形 buffer 容量"
(上行 / 下行各维护独立环形 buffer，至少容纳 200 ms 音频；溢出 SHALL
丢弃最早一帧并打告警日志，"音频丢帧而非整链路阻塞").

Implemented as a bounded :class:`collections.deque` guarded by an
asyncio condition. We deliberately do NOT use :class:`asyncio.Queue`:
its `put()` blocks the producer once full, but the spec calls for
*drop-oldest* on overflow — i.e. audio drops, modem-controller doesn't
stall, the modem keeps writing frames at real-time cadence.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Iterator

logger = logging.getLogger(__name__)


class PcmRingBuffer:
    """Bounded async ring buffer for PCM chunks.

    Args:
        capacity_bytes: total bytes the buffer may hold. v1 default
            16 KiB ≈ 1024 ms @ 8 kHz mono 16-bit or 512 ms @ 16 kHz
            mono 16-bit — well above the spec's 200 ms floor.
        name: human-readable tag used in overflow log messages
            (e.g. ``"modem_upstream"`` / ``"modem_downstream"``).

    Threading: single asyncio event loop only — methods are not
    safe to call from multiple threads.
    """

    def __init__(self, *, capacity_bytes: int = 16 * 1024, name: str = "pcm") -> None:
        if capacity_bytes <= 0:
            raise ValueError("capacity_bytes must be positive")
        self._capacity = capacity_bytes
        self._name = name
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._cond = asyncio.Condition()
        self._closed = False
        # Counters for ops visibility / D2 hardware-observability.
        self.dropped_chunks = 0
        self.dropped_bytes = 0

    async def put(self, chunk: bytes) -> None:
        """Enqueue ``chunk``. Never blocks the caller — on overflow,
        drops the OLDEST chunk(s) until ``chunk`` fits.

        Returns immediately after the chunk is queued (or after enough
        old chunks are evicted to make room).
        """
        if self._closed:
            raise RuntimeError(f"PcmRingBuffer({self._name}) closed")
        if len(chunk) > self._capacity:
            # A single chunk larger than the whole buffer means the
            # upstream producer is mis-sized. Log and drop the chunk
            # rather than clearing the buffer to make room.
            self.dropped_chunks += 1
            self.dropped_bytes += len(chunk)
            logger.warning(
                "ring_buffer_chunk_too_large",
                extra={
                    "buffer_name": self._name,
                    "chunk_bytes": len(chunk),
                    "capacity_bytes": self._capacity,
                },
            )
            return
        async with self._cond:
            # Drop oldest until the new chunk fits.
            while self._size + len(chunk) > self._capacity and self._chunks:
                old = self._chunks.popleft()
                self._size -= len(old)
                self.dropped_chunks += 1
                self.dropped_bytes += len(old)
                logger.debug(
                    "ring_buffer_overflow_drop_oldest",
                    extra={
                        "buffer_name": self._name,
                        "dropped_bytes": len(old),
                    },
                )
            self._chunks.append(chunk)
            self._size += len(chunk)
            self._cond.notify()

    async def get(self) -> bytes:
        """Pop the next chunk, blocking until one is available or the
        buffer is :meth:`close`-d.

        Raises :class:`StopAsyncIteration` if the buffer is closed and
        empty — lets callers terminate ``async for`` loops naturally.
        """
        async with self._cond:
            while not self._chunks and not self._closed:
                await self._cond.wait()
            if not self._chunks:
                # Closed + empty.
                raise StopAsyncIteration
            chunk = self._chunks.popleft()
            self._size -= len(chunk)
            return chunk

    def get_nowait(self) -> bytes | None:
        """Non-blocking peek-and-pop. Returns ``None`` when empty."""
        if not self._chunks:
            return None
        chunk = self._chunks.popleft()
        self._size -= len(chunk)
        return chunk

    def __len__(self) -> int:
        """Current byte count in the buffer (not chunk count)."""
        return self._size

    def chunks(self) -> Iterator[bytes]:
        """Iterate over currently-queued chunks without consuming.

        Test helper only. Not part of the production contract.
        """
        return iter(self._chunks)

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        """Mark the buffer closed; wake all :meth:`get` waiters so they
        see ``StopAsyncIteration``. Idempotent."""
        async with self._cond:
            self._closed = True
            self._cond.notify_all()
