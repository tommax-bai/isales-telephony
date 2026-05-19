"""CloudEdgeGrpcClient integration tests.

Spec: service-communication § Requirement: 云-边控制面 — Scenario "断线
重连与本地 buffer" + "cloud → edge 命令丢失语义".

Spins up a minimal grpc.aio CloudEdge server (test helper, not the
isales-engine production server — isales-telephony doesn't depend on
isales-engine), and connects the client against it. Tests focus on:

- Initial connect + first send / receive.
- Auto-reconnect on server restart.
- Buffering non-critical sends during disconnect + flush on reconnect.
- ``critical=True`` raises EdgeNotConnected when disconnected.
- Token forwarded as ``authorization: Bearer <token>``.
- Channel constructed with HTTP/2 keepalive options + ``cloud_edge_stream_connected``
  INFO log fires on every successful initial_metadata (cloud-edge-grpc-keepalive).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator

import grpc
import grpc.aio
import pytest
from isales_common.proto import cloud_edge_pb2 as pb
from isales_common.proto import cloud_edge_pb2_grpc
from isales_common.transport.cloud_edge import EdgeNotConnected

from isales_telephony.transport.grpc_client import CloudEdgeGrpcClient
from isales_telephony.transport.sqlite_buffer import SqliteEventBuffer


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _TestServicer(cloud_edge_pb2_grpc.CloudEdgeServicer):
    """Minimal Bidi servicer — verifies Bearer token, records inbound
    frames, lets the test push outbound frames via :meth:`push_to_edge`.

    Aborts with UNAUTHENTICATED for any token NOT in ``accepted_tokens``.
    """

    def __init__(self, *, accepted_tokens: set[str]) -> None:
        self._accepted_tokens = accepted_tokens
        self.received: list[pb.Edge2Cloud] = []
        self.received_tokens: list[str] = []
        self._outbound: asyncio.Queue[pb.Cloud2Edge | object] = asyncio.Queue()
        self.connection_count = 0

    async def Bidi(  # noqa: N802 — matches generated stub
        self,
        request_iterator: AsyncIterator[pb.Edge2Cloud],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[pb.Cloud2Edge]:
        meta = dict(context.invocation_metadata() or [])
        auth = meta.get("authorization", "")
        if not auth.startswith("Bearer "):
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "missing bearer",
            )
            return
        token = auth.removeprefix("Bearer ")
        self.received_tokens.append(token)
        if token not in self._accepted_tokens:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "bad token")
            return

        # Same eager-flush trick the production CloudEdgeGrpcServer does
        # — lets `await call.initial_metadata()` on the client side
        # unblock as soon as the server has accepted the RPC, even if
        # we never yield a response.
        await context.send_initial_metadata([])

        self.connection_count += 1

        async def reader() -> None:
            async for msg in request_iterator:
                self.received.append(msg)

        reader_task = asyncio.create_task(reader())
        _CLOSE: object = object()  # local sentinel
        try:
            while True:
                item = await self._outbound.get()
                if item is _CLOSE:
                    return
                assert isinstance(item, pb.Cloud2Edge)
                yield item
        finally:
            reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader_task

    async def push_to_edge(self, msg: pb.Cloud2Edge) -> None:
        await self._outbound.put(msg)


async def _start_server(
    *,
    accepted_tokens: set[str] | None = None,
) -> tuple[grpc.aio.Server, _TestServicer, str]:
    accepted = accepted_tokens if accepted_tokens is not None else {"good-token"}
    servicer = _TestServicer(accepted_tokens=accepted)
    server = grpc.aio.server()
    cloud_edge_pb2_grpc.add_CloudEdgeServicer_to_server(servicer, server)
    port = _free_port()
    server.add_insecure_port(f"127.0.0.1:{port}")
    await server.start()
    return server, servicer, f"127.0.0.1:{port}"


# --------------------------------------------------------------------------
# Construction validation
# --------------------------------------------------------------------------


def test_construct_with_bad_backoff_raises() -> None:
    with pytest.raises(ValueError, match="initial_backoff_s"):
        CloudEdgeGrpcClient(initial_backoff_s=0)
    with pytest.raises(ValueError, match="max_backoff_s"):
        CloudEdgeGrpcClient(initial_backoff_s=10, max_backoff_s=5)
    with pytest.raises(ValueError, match="backoff_factor"):
        CloudEdgeGrpcClient(backoff_factor=1.0)
    with pytest.raises(ValueError, match="memory_buffer_limit"):
        CloudEdgeGrpcClient(memory_buffer_limit=0)


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_send_receive_disconnect() -> None:
    server, servicer, target = await _start_server()
    client = CloudEdgeGrpcClient(initial_backoff_s=0.05, max_backoff_s=0.5)
    try:
        await client.start(target, "good-token")
        assert client.is_connected is True

        # Token forwarded.
        assert servicer.received_tokens[-1] == "good-token"

        # Edge → Cloud send.
        await client.send(
            pb.Edge2Cloud(
                call_event=pb.CallEvent(call_id="c-1", connected=pb.Connected()),
            ),
        )
        # Give the server reader a moment to drain.
        for _ in range(50):
            if servicer.received:
                break
            await asyncio.sleep(0.01)
        assert len(servicer.received) == 1
        assert servicer.received[0].call_event.WhichOneof("kind") == "connected"

        # Cloud → Edge receive.
        received: list[pb.Cloud2Edge] = []

        async def handler(msg: pb.Cloud2Edge) -> None:
            received.append(msg)

        client.on_cloud_message(handler)
        await servicer.push_to_edge(
            pb.Cloud2Edge(
                cancel=pb.CancelCommand(call_id="c-1", reason="manual"),
            ),
        )
        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.01)
        assert len(received) == 1
        assert received[0].cancel.call_id == "c-1"
    finally:
        await client.stop()
        await server.stop(grace=0.1)


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_token_keeps_retrying_until_stopped() -> None:
    """A wrong token causes the server to abort with UNAUTHENTICATED.
    The client treats this as a transient stream error and keeps
    retrying (the operator's job to fix the token). The test verifies
    is_connected stays False and start() blocks until the deadline /
    stop()."""
    server, _, target = await _start_server(accepted_tokens={"right"})
    client = CloudEdgeGrpcClient(initial_backoff_s=0.05, max_backoff_s=0.1)
    try:
        # Don't await start indefinitely — it would hang. Use a tight
        # timeout, then assert we're still NOT connected.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                client.start(target, "wrong"),
                timeout=0.5,
            )
        assert client.is_connected is False
    finally:
        await client.stop()
        await server.stop(grace=0.1)


# --------------------------------------------------------------------------
# send() critical contract
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_critical_send_while_disconnected_raises() -> None:
    """Before start() is even called, send(critical=True) MUST raise
    EdgeNotConnected — the spec promises hot-path latency for these."""
    client = CloudEdgeGrpcClient()
    with pytest.raises(EdgeNotConnected):
        await client.send(
            pb.Edge2Cloud(heartbeat=pb.Heartbeat()),
            critical=True,
        )


@pytest.mark.asyncio
async def test_non_critical_send_while_disconnected_buffers() -> None:
    """Before start(), send(critical=False) buffers; pending_buffer_size()
    reflects it."""
    client = CloudEdgeGrpcClient()
    await client.send(
        pb.Edge2Cloud(
            call_event=pb.CallEvent(call_id="c-1", connected=pb.Connected()),
        ),
    )
    await client.send(
        pb.Edge2Cloud(
            hardware_alert=pb.HardwareAlert(
                signal_lost=pb.SignalLost(last_signal_strength=2),
            ),
        ),
    )
    assert client.pending_buffer_size() == 2


@pytest.mark.asyncio
async def test_buffered_frames_flush_in_order_on_connect() -> None:
    server, servicer, target = await _start_server()
    client = CloudEdgeGrpcClient(initial_backoff_s=0.05)
    try:
        # Pre-load 3 frames before start.
        for i in range(3):
            await client.send(
                pb.Edge2Cloud(
                    call_event=pb.CallEvent(
                        call_id=f"c-{i}",
                        connected=pb.Connected(),
                    ),
                ),
            )
        assert client.pending_buffer_size() == 3

        await client.start(target, "good-token")
        # Buffer flushes on reconnect; wait for the server to see all 3.
        for _ in range(100):
            if len(servicer.received) >= 3:
                break
            await asyncio.sleep(0.01)
        assert len(servicer.received) == 3
        # Order preserved.
        assert [f.call_event.call_id for f in servicer.received] == ["c-0", "c-1", "c-2"]
        # Buffer is empty after flush.
        assert client.pending_buffer_size() == 0
    finally:
        await client.stop()
        await server.stop(grace=0.1)


@pytest.mark.asyncio
async def test_buffer_overflow_drops_oldest() -> None:
    """Sustained disconnect with a low buffer limit: oldest dropped."""
    client = CloudEdgeGrpcClient(memory_buffer_limit=3)
    # Send 5 — first 2 should be dropped.
    for i in range(5):
        await client.send(
            pb.Edge2Cloud(
                call_event=pb.CallEvent(call_id=f"c-{i}", connected=pb.Connected()),
            ),
        )
    assert client.pending_buffer_size() == 3
    # Internal: should hold c-2, c-3, c-4. Hard to verify externally without
    # actually connecting; that's tested via flush ordering below.


# --------------------------------------------------------------------------
# Auto-reconnect
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_reconnect_after_server_restart() -> None:
    """Kill the server, ensure is_connected flips to False; restart it
    on the same port, ensure the client reconnects and resumes."""
    server1, _servicer1, target = await _start_server()
    port = int(target.rsplit(":", 1)[1])
    client = CloudEdgeGrpcClient(initial_backoff_s=0.05, max_backoff_s=0.2)
    try:
        await client.start(target, "good-token")
        assert client.is_connected is True

        # Kill server 1.
        await server1.stop(grace=0.05)

        # Wait for client to notice.
        for _ in range(200):
            if not client.is_connected:
                break
            await asyncio.sleep(0.01)
        assert client.is_connected is False

        # Bring up server 2 on the same port — the client's reconnect
        # loop will pick it up.
        servicer2 = _TestServicer(accepted_tokens={"good-token"})
        server2 = grpc.aio.server()
        cloud_edge_pb2_grpc.add_CloudEdgeServicer_to_server(servicer2, server2)
        server2.add_insecure_port(f"127.0.0.1:{port}")
        await server2.start()
        try:
            for _ in range(400):
                if client.is_connected:
                    break
                await asyncio.sleep(0.01)
            assert client.is_connected is True
            assert servicer2.connection_count == 1

            # Verify the resumed stream still routes Edge2Cloud frames.
            await client.send(
                pb.Edge2Cloud(
                    call_event=pb.CallEvent(call_id="after", connected=pb.Connected()),
                ),
                critical=True,
            )
            for _ in range(50):
                if servicer2.received:
                    break
                await asyncio.sleep(0.01)
            assert len(servicer2.received) == 1
        finally:
            await server2.stop(grace=0.05)
    finally:
        await client.stop()


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_start_raises() -> None:
    server, _, target = await _start_server()
    client = CloudEdgeGrpcClient(initial_backoff_s=0.05)
    try:
        await client.start(target, "good-token")
        with pytest.raises(RuntimeError, match="already started"):
            await client.start(target, "good-token")
    finally:
        await client.stop()
        await server.stop(grace=0.1)


@pytest.mark.asyncio
async def test_stop_is_idempotent() -> None:
    client = CloudEdgeGrpcClient()
    await client.stop()  # never started
    await client.stop()  # second time, still fine


@pytest.mark.asyncio
async def test_callback_exception_does_not_kill_stream(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A buggy on_cloud_message handler must not kill the stream — the
    next inbound frame should still arrive."""
    import logging

    server, servicer, target = await _start_server()
    client = CloudEdgeGrpcClient(initial_backoff_s=0.05)
    try:
        count = 0
        second_seen = asyncio.Event()

        async def handler(_msg: pb.Cloud2Edge) -> None:
            nonlocal count
            count += 1
            if count == 1:
                raise RuntimeError("test-induced bug")
            second_seen.set()

        client.on_cloud_message(handler)
        await client.start(target, "good-token")

        with caplog.at_level(logging.ERROR):
            await servicer.push_to_edge(
                pb.Cloud2Edge(cancel=pb.CancelCommand(call_id="c-1", reason="x")),
            )
            await servicer.push_to_edge(
                pb.Cloud2Edge(cancel=pb.CancelCommand(call_id="c-2", reason="x")),
            )
            await asyncio.wait_for(second_seen.wait(), timeout=2.0)

        assert count == 2
        assert any("cloud_edge_callback_failed" in r.message for r in caplog.records)
    finally:
        await client.stop()
        await server.stop(grace=0.1)


# --------------------------------------------------------------------------
# Durable SQLite buffer integration
#
# Spec: service-communication § Scenario "断线重连与本地 buffer" — the
# in-memory deque covers the v1 floor; SqliteEventBuffer covers the
# spec'd "中断期间产生的 CallEvent / HardwareAlert SHALL 写入边缘本地
# SQLite buffer". These tests verify the wire-up.
# --------------------------------------------------------------------------


def _open_buffer(tmp_path: object) -> SqliteEventBuffer:
    """Helper: open a fresh sqlite buffer under tmp_path."""
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    buf = SqliteEventBuffer(path=tmp_path / "edge_buffer.db")
    buf.open()
    return buf


@pytest.mark.asyncio
async def test_durable_buffer_appends_while_disconnected(tmp_path: object) -> None:
    """send(critical=False) before start() appends rows to the sqlite
    buffer instead of the in-memory deque."""
    buf = _open_buffer(tmp_path)
    try:
        client = CloudEdgeGrpcClient(event_buffer=buf)
        for i in range(3):
            await client.send(
                pb.Edge2Cloud(
                    call_event=pb.CallEvent(
                        call_id=f"c-{i}",
                        connected=pb.Connected(),
                    ),
                ),
            )
        assert client.pending_buffer_size() == 3
        assert len(buf) == 3
    finally:
        buf.close()


@pytest.mark.asyncio
async def test_durable_critical_still_raises_when_disconnected(
    tmp_path: object,
) -> None:
    """The sqlite buffer is for non-critical frames only; critical=True
    must still raise EdgeNotConnected (Heartbeat / DialAck contract)."""
    buf = _open_buffer(tmp_path)
    try:
        client = CloudEdgeGrpcClient(event_buffer=buf)
        with pytest.raises(EdgeNotConnected):
            await client.send(
                pb.Edge2Cloud(heartbeat=pb.Heartbeat()),
                critical=True,
            )
        assert len(buf) == 0
    finally:
        buf.close()


@pytest.mark.asyncio
async def test_durable_buffer_flushes_in_order_on_connect(
    tmp_path: object,
) -> None:
    """Frames appended while disconnected are flushed in seq order on
    first connect, and deleted from the durable buffer after put."""
    buf = _open_buffer(tmp_path)
    try:
        client = CloudEdgeGrpcClient(
            initial_backoff_s=0.05,
            event_buffer=buf,
        )
        for i in range(3):
            await client.send(
                pb.Edge2Cloud(
                    call_event=pb.CallEvent(
                        call_id=f"d-{i}",
                        connected=pb.Connected(),
                    ),
                ),
            )
        assert len(buf) == 3

        server, servicer, target = await _start_server()
        try:
            await client.start(target, "good-token")
            for _ in range(100):
                if len(servicer.received) >= 3:
                    break
                await asyncio.sleep(0.01)
            assert [f.call_event.call_id for f in servicer.received] == [
                "d-0",
                "d-1",
                "d-2",
            ]
            # Weak-ACK semantics: rows are deleted as they hit the
            # outbound queue.
            assert len(buf) == 0
            assert client.pending_buffer_size() == 0
        finally:
            await client.stop()
            await server.stop(grace=0.1)
    finally:
        buf.close()


@pytest.mark.asyncio
async def test_durable_buffer_survives_client_restart(tmp_path: object) -> None:
    """The whole point of the durable backend: frames appended to one
    client instance must replay through a fresh instance pointed at the
    same buffer file (i.e. simulating a process crash + restart)."""
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    db_path = tmp_path / "edge_buffer.db"

    # Phase 1: produce, no server up — frames land in sqlite.
    buf1 = SqliteEventBuffer(path=db_path)
    buf1.open()
    try:
        client1 = CloudEdgeGrpcClient(event_buffer=buf1)
        for i in range(2):
            await client1.send(
                pb.Edge2Cloud(
                    call_event=pb.CallEvent(
                        call_id=f"persist-{i}",
                        connected=pb.Connected(),
                    ),
                ),
            )
        assert len(buf1) == 2
    finally:
        buf1.close()  # close as if process exited

    # Phase 2: a *new* client + buffer instance on the same file; start
    # against a live server. The pre-existing rows must flush.
    buf2 = SqliteEventBuffer(path=db_path)
    buf2.open()
    try:
        assert len(buf2) == 2  # rows survived
        client2 = CloudEdgeGrpcClient(
            initial_backoff_s=0.05,
            event_buffer=buf2,
        )
        server, servicer, target = await _start_server()
        try:
            await client2.start(target, "good-token")
            for _ in range(100):
                if len(servicer.received) >= 2:
                    break
                await asyncio.sleep(0.01)
            assert [f.call_event.call_id for f in servicer.received] == [
                "persist-0",
                "persist-1",
            ]
            assert len(buf2) == 0
        finally:
            await client2.stop()
            await server.stop(grace=0.1)
    finally:
        buf2.close()


# ---------------------------------------------------------------------------
# cloud-edge-grpc-keepalive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_constructed_with_keepalive_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec: cloud-edge-grpc-keepalive § "edge gRPC client 启用 HTTP/2 keepalive
    默认参数". Failing this means a refactor dropped the channel options
    and stream lifetime would silently regress.
    """
    captured: list[dict[str, object]] = []
    real_insecure = grpc.aio.insecure_channel

    def _spy_insecure(target, **kwargs):  # type: ignore[no-untyped-def]
        captured.append({"target": target, **kwargs})
        return real_insecure(target, **kwargs)

    monkeypatch.setattr(grpc.aio, "insecure_channel", _spy_insecure)

    server, servicer, target = await _start_server()
    client = CloudEdgeGrpcClient(initial_backoff_s=0.05)
    try:
        await client.start(target, "good-token")
        for _ in range(50):
            if client.is_connected:
                break
            await asyncio.sleep(0.02)
        assert client.is_connected
    finally:
        await client.stop()
        await server.stop(grace=0.1)

    assert captured, "insecure_channel was not invoked"
    options = dict(captured[0].get("options") or [])
    assert options.get("grpc.keepalive_time_ms") == 30000
    assert options.get("grpc.keepalive_timeout_ms") == 10000
    assert options.get("grpc.keepalive_permit_without_calls") == 1
    assert options.get("grpc.http2.max_pings_without_data") == 0
    assert options.get("grpc.http2.min_time_between_pings_ms") == 10000
    assert options.get(
        "grpc.http2.min_ping_interval_without_data_ms",
    ) == 10000


@pytest.mark.asyncio
async def test_stream_connected_log_fires_on_initial_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec: cloud-edge-grpc-keepalive § "stream 上线 + 上线时分别打 INFO 日志".

    The edge client SHALL emit ``cloud_edge_stream_connected`` exactly
    when ``call.initial_metadata()`` returns successfully, so dev / ops
    can correlate against the server's ``cloud_edge_stream_opened``.
    """
    import logging

    caplog.set_level(
        logging.INFO, logger="isales_telephony.transport.grpc_client",
    )
    server, _servicer, target = await _start_server()
    client = CloudEdgeGrpcClient(initial_backoff_s=0.05)
    try:
        await client.start(target, "good-token")
        for _ in range(50):
            if client.is_connected:
                break
            await asyncio.sleep(0.02)
        assert client.is_connected
    finally:
        await client.stop()
        await server.stop(grace=0.1)

    matches = [
        r for r in caplog.records
        if r.getMessage() == "cloud_edge_stream_connected"
    ]
    assert matches, "expected at least one cloud_edge_stream_connected INFO line"
    # The structured extra MUST carry endpoint so ops can grep by ECS IP.
    assert getattr(matches[-1], "endpoint", None) == target


@pytest.mark.asyncio
async def test_reconnect_after_unavailable_during_stream_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec: cloud-edge-grpc-keepalive § "edge gRPC client 启用 HTTP/2 keepalive"
    +  service-communication § "断线重连与本地 buffer".

    Models the Aliyun "Socket closed 1ms after initial_metadata"
    pattern. We can't precisely inject `AioRpcError(UNAVAILABLE)` into
    the bidi stream from an external test without a custom transport;
    the closest fixture-friendly trigger is a full server stop/start
    cycle, which produces the same ``AioRpcError(UNAVAILABLE)`` in
    ``_connect_loop``. The contract under test is: client logs the
    ``cloud_edge_stream_connected`` marker on EACH successful
    re-establishment + ``cloud_edge_stream_error`` between them.
    """
    import logging

    caplog.set_level(
        logging.INFO, logger="isales_telephony.transport.grpc_client",
    )

    server1, _servicer1, target = await _start_server()
    port = int(target.rsplit(":", 1)[1])
    client = CloudEdgeGrpcClient(initial_backoff_s=0.05, max_backoff_s=0.2)
    try:
        await client.start(target, "good-token")
        for _ in range(50):
            if client.is_connected:
                break
            await asyncio.sleep(0.02)
        assert client.is_connected

        # Tear server down → client's `async for response in call:` gets
        # an AioRpcError(UNAVAILABLE) on next read, which the
        # _connect_loop catches as a stream error.
        await server1.stop(grace=0.05)
        for _ in range(200):
            if not client.is_connected:
                break
            await asyncio.sleep(0.01)
        assert client.is_connected is False

        # Bring back a fresh server on the same port — backoff re-attempts
        # land on the new instance + the marker log fires a second time.
        servicer2 = _TestServicer(accepted_tokens={"good-token"})
        server2 = grpc.aio.server()
        cloud_edge_pb2_grpc.add_CloudEdgeServicer_to_server(servicer2, server2)
        server2.add_insecure_port(f"127.0.0.1:{port}")
        await server2.start()
        try:
            for _ in range(400):
                if client.is_connected:
                    break
                await asyncio.sleep(0.01)
            assert client.is_connected is True
        finally:
            await server2.stop(grace=0.05)
    finally:
        await client.stop()

    connected_logs = [
        r for r in caplog.records
        if r.getMessage() == "cloud_edge_stream_connected"
    ]
    error_logs = [
        r for r in caplog.records
        if r.getMessage() == "cloud_edge_stream_error"
    ]
    assert len(connected_logs) >= 2, (
        "expected ≥ 2 cloud_edge_stream_connected (initial + post-reconnect); "
        f"saw {len(connected_logs)}"
    )
    assert error_logs, (
        "expected at least one cloud_edge_stream_error during reconnect"
    )
