"""Edge-side :class:`CloudEdgeClient` over grpc.aio.

Spec: service-communication § Requirement: 云-边控制面 — "断线重连与本地
buffer".

One :class:`CloudEdgeGrpcClient` instance per edge process; it maintains
ONE bidi stream to the cloud. Auto-reconnect with exponential backoff
(initial 1s, max 30s, infinite retries). While disconnected:

- ``send(msg, critical=True)`` raises :class:`EdgeNotConnected`. Use this
  for ``Heartbeat`` (a stale heartbeat is meaningless) and ``DialAck``
  (the cloud has already moved on if the ACK didn't land).
- ``send(msg, critical=False)`` queues to a bounded in-memory buffer
  (FIFO, oldest dropped on overflow). Buffered frames flush in order on
  reconnect. Use this for ``CallEvent`` and ``HardwareAlert`` — they
  need durable delivery, but the spec accepts in-flight loss for
  catastrophic crashes (a persistent SQLite buffer is a follow-up PR).

The implementation deliberately keeps the buffer abstraction narrow:
the in-memory ``deque`` here is the v1 floor. A future PR swaps it for
a persistent SQLite-backed buffer (deps.txt change + same ``send``
contract).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from typing import TYPE_CHECKING

import grpc
import grpc.aio
from isales_common.proto import cloud_edge_pb2 as pb
from isales_common.proto import cloud_edge_pb2_grpc
from isales_common.transport.cloud_edge import (
    CloudEdgeClient,
    CloudMessageCallback,
    EdgeNotConnected,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

__all__ = ["CloudEdgeGrpcClient"]


# Sentinel pushed into the outbound queue to break the iterator on stream
# teardown / reconnect.
_CLOSE: object = object()


class CloudEdgeGrpcClient(CloudEdgeClient):
    """Production :class:`CloudEdgeClient`. Edge → cloud bidi stream
    with auto-reconnect and a small in-memory buffer for non-critical
    frames queued while disconnected.

    Construction is decoupled from connecting; pass ``endpoint`` + ``token``
    to :meth:`start` to actually open the channel.
    """

    def __init__(
        self,
        *,
        initial_backoff_s: float = 1.0,
        max_backoff_s: float = 30.0,
        backoff_factor: float = 2.0,
        memory_buffer_limit: int = 10_000,
    ) -> None:
        if initial_backoff_s <= 0:
            raise ValueError("initial_backoff_s must be positive")
        if max_backoff_s < initial_backoff_s:
            raise ValueError("max_backoff_s must be ≥ initial_backoff_s")
        if backoff_factor <= 1:
            raise ValueError("backoff_factor must be > 1")
        if memory_buffer_limit <= 0:
            raise ValueError("memory_buffer_limit must be positive")

        self._initial_backoff_s = initial_backoff_s
        self._max_backoff_s = max_backoff_s
        self._backoff_factor = backoff_factor

        self._cloud_callback: CloudMessageCallback | None = None
        self._endpoint: str | None = None
        self._token: str | None = None

        # Per-stream lifecycle objects — recreated on each reconnect.
        self._channel: grpc.aio.Channel | None = None
        self._outbound: asyncio.Queue[pb.Edge2Cloud | object] = asyncio.Queue()

        # Liveness signal — set when a stream is actively running.
        self._connected = asyncio.Event()
        # External stop signal.
        self._stop_event = asyncio.Event()
        # Pending buffer for non-critical frames queued while disconnected.
        self._buffer: deque[pb.Edge2Cloud] = deque(maxlen=memory_buffer_limit)

        self._connect_task: asyncio.Task[None] | None = None

    # ===== CloudEdgeClient surface =======================================

    async def start(self, endpoint: str, token: str) -> None:
        """Connect; return once the first stream is established.

        Background reconnect loop continues until :meth:`stop`. If the
        first connection attempt fails AND the channel is :meth:`stop`-ped
        before a retry succeeds, raises :class:`ConnectionError`.
        """
        if self._connect_task is not None:
            raise RuntimeError("client already started")
        self._endpoint = endpoint
        self._token = token
        self._stop_event.clear()
        self._connect_task = asyncio.create_task(
            self._connect_loop(),
            name="cloud_edge_client_connect_loop",
        )
        # Wait until either a stream is up or the caller stopped us.
        wait_connected = asyncio.create_task(self._connected.wait())
        wait_stop = asyncio.create_task(self._stop_event.wait())
        try:
            await asyncio.wait(
                {wait_connected, wait_stop},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for t in (wait_connected, wait_stop):
                if not t.done():
                    t.cancel()
        # Only raise ConnectionError when the caller actually called
        # :meth:`stop` before the first stream came up. If asyncio.wait
        # was cancelled externally (e.g. wait_for timeout in tests), the
        # CancelledError already propagated through the try/finally —
        # don't shadow it with a misleading ConnectionError. If neither
        # event fired and we're still here, the connect loop is in
        # backoff: surface that as "still trying" by returning normally;
        # the caller polls `is_connected` to know when it's actually up.
        if self._stop_event.is_set() and not self._connected.is_set():
            raise ConnectionError(
                f"cloud-edge client stopped before reaching {endpoint!r}",
            )

    async def stop(self) -> None:
        """Cancel reconnect loop, close stream + channel. Idempotent."""
        self._stop_event.set()
        # Sentinel breaks the outbound iterator if a stream is active.
        with contextlib.suppress(asyncio.QueueFull):
            self._outbound.put_nowait(_CLOSE)
        if self._connect_task is not None:
            self._connect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._connect_task
            self._connect_task = None
        if self._channel is not None:
            with contextlib.suppress(Exception):
                await self._channel.close()
            self._channel = None
        self._connected.clear()

    async def send(
        self,
        message: pb.Edge2Cloud,
        *,
        critical: bool = False,
    ) -> None:
        """Send ``message`` upstream.

        If currently connected, queues onto the active stream's outbound.
        If disconnected:
          - ``critical=True``: raises :class:`EdgeNotConnected`.
          - ``critical=False``: queues into the in-memory buffer (oldest
            dropped on overflow); flushes in order on reconnect.
        """
        if self._connected.is_set():
            await self._outbound.put(message)
            return
        if critical:
            raise EdgeNotConnected("cloud-edge stream not connected")
        # FIFO drop-oldest is what deque(maxlen=N) does by default;
        # callers observe the message *might* be lost on overflow but the
        # client doesn't raise (overflow under sustained disconnect is a
        # symptom upstream should already alert on via heartbeat gaps).
        self._buffer.append(message)

    def on_cloud_message(self, callback: CloudMessageCallback) -> None:
        self._cloud_callback = callback

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    # ===== Introspection / test hooks ===================================

    def pending_buffer_size(self) -> int:
        """Number of non-critical frames currently waiting for reconnect.

        Used by callers and tests to surface buffer health; not part of
        the ABC.
        """
        return len(self._buffer)

    # ===== Connect loop ==================================================

    async def _connect_loop(self) -> None:
        backoff = self._initial_backoff_s
        while not self._stop_event.is_set():
            try:
                await self._run_one_stream()
                # Stream ended cleanly (server closed). Reset backoff —
                # this wasn't a transport failure.
                backoff = self._initial_backoff_s
            except asyncio.CancelledError:
                return
            except grpc.aio.AioRpcError as exc:
                logger.warning(
                    "cloud_edge_stream_error",
                    extra={
                        "code": exc.code().name if exc.code() else None,
                        "details": exc.details(),
                    },
                )
            except Exception:  # noqa: BLE001 — defensive: never let connect loop die
                logger.exception("cloud_edge_stream_unexpected_error")
            finally:
                self._connected.clear()

            if self._stop_event.is_set():
                return
            await asyncio.sleep(min(backoff, self._max_backoff_s))
            backoff = min(backoff * self._backoff_factor, self._max_backoff_s)

    async def _run_one_stream(self) -> None:
        """Open one bidi stream and pump until it closes."""
        assert self._endpoint is not None
        assert self._token is not None

        self._channel = grpc.aio.insecure_channel(self._endpoint)
        stub = cloud_edge_pb2_grpc.CloudEdgeStub(  # type: ignore[no-untyped-call]
            self._channel,
        )
        # Fresh outbound queue per stream — leftover sentinels from a
        # previous stream's teardown must not leak in here.
        self._outbound = asyncio.Queue()
        metadata = (("authorization", f"Bearer {self._token}"),)
        call = stub.Bidi(self._outbound_iter(), metadata=metadata)
        # Wait for the server to actually accept the RPC (i.e. enter its
        # Bidi method body). Without this, ``stub.Bidi`` returns
        # immediately and ``_connected`` would flip True before grpc.aio
        # has even started the server-side coroutine — auth errors and
        # connection failures would slip past start() and surface as
        # silent dropped frames on the outbound queue. ``initial_metadata``
        # raises :class:`AioRpcError` for ``UNAUTHENTICATED`` / connection
        # refused, which the connect loop catches and retries.
        await call.initial_metadata()
        self._connected.set()
        await self._flush_buffer()
        try:
            async for response in call:
                if self._cloud_callback is None:
                    continue
                try:
                    await self._cloud_callback(response)
                except Exception:  # noqa: BLE001 — handler errors must not kill the stream
                    logger.exception(
                        "cloud_edge_callback_failed",
                        extra={
                            "kind": response.WhichOneof("payload"),
                        },
                    )
        finally:
            self._connected.clear()
            # The reader exiting tears down the stream; close the channel
            # so the next reconnect builds a fresh one.
            if self._channel is not None:
                with contextlib.suppress(Exception):
                    await self._channel.close()
                self._channel = None

    async def _outbound_iter(self) -> AsyncIterator[pb.Edge2Cloud]:
        while True:
            item = await self._outbound.get()
            if item is _CLOSE:
                return
            assert isinstance(item, pb.Edge2Cloud)
            yield item

    async def _flush_buffer(self) -> None:
        """Move any queued-while-disconnected frames onto the active
        stream's outbound, in original order."""
        while self._buffer:
            msg = self._buffer.popleft()
            await self._outbound.put(msg)
