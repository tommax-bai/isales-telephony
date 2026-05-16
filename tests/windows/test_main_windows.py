"""Cross-platform unit coverage for ``main_windows.py`` pieces.

We skip the ``run()`` wrapper (it depends on PySide6 + qasync at
import-call time); D1 PoC week 1 exercises that on a real Windows
host. The pieces tested here are pure-Python: log ring, gRPC
restarter, connection watcher.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from isales_telephony.main_windows import (
    _GrpcClientRestarter,
    _RingBufferLogHandler,
    _watch_grpc_connection,
)
from isales_telephony.ui.state import CloudLinkState, StateBus


def test_ring_log_handler_keeps_only_last_n() -> None:
    bus = StateBus()
    handler = _RingBufferLogHandler(state_bus=bus, capacity=3)
    handler.setFormatter(logging.Formatter("%(message)s"))

    for i in range(7):
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=0,
            msg=f"line-{i}",
            args=None,
            exc_info=None,
        )
        handler.emit(record)

    lines = bus.status.recent_log_lines
    assert len(lines) == 3
    assert lines == ("line-4", "line-5", "line-6")


class _FakeGrpcClient:
    def __init__(self, *, connect_raises: Exception | None = None) -> None:
        self.stopped = 0
        self.started: list[tuple[str, str]] = []
        self._connect_raises = connect_raises
        self.is_connected = False

    async def stop(self) -> None:
        self.stopped += 1
        self.is_connected = False

    async def start(self, *, endpoint: str, token: str) -> None:
        if self._connect_raises is not None:
            raise self._connect_raises
        self.started.append((endpoint, token))
        self.is_connected = True


@pytest.mark.asyncio(loop_scope="session")
async def test_grpc_restarter_stops_then_starts_then_flips_state() -> None:
    client = _FakeGrpcClient()
    bus = StateBus()
    bus.update(cloud_link=CloudLinkState.AWAITING_ACTIVATION)
    restarter = _GrpcClientRestarter(client=client, bus=bus)

    await restarter.restart(endpoint="isales.example.com:443", token="abcdef1234567890")

    assert client.stopped == 1
    assert client.started == [("isales.example.com:443", "abcdef1234567890")]
    assert bus.status.cloud_link is CloudLinkState.CONNECTED


@pytest.mark.asyncio(loop_scope="session")
async def test_grpc_restarter_propagates_start_failures() -> None:
    client = _FakeGrpcClient(connect_raises=RuntimeError("UNAUTHENTICATED"))
    bus = StateBus()
    restarter = _GrpcClientRestarter(client=client, bus=bus)

    with pytest.raises(RuntimeError, match="UNAUTHENTICATED"):
        await restarter.restart(
            endpoint="isales.example.com:443", token="abcdef1234567890"
        )
    # State bus untouched by restarter (activation controller manages it).
    assert bus.status.cloud_link is CloudLinkState.AWAITING_ACTIVATION


@pytest.mark.asyncio(loop_scope="session")
async def test_watch_grpc_connection_flips_to_connected() -> None:
    client = _FakeGrpcClient()
    bus = StateBus()
    bus.update(cloud_link=CloudLinkState.DISCONNECTED)

    task = asyncio.create_task(
        _watch_grpc_connection(client=client, bus=bus, poll_interval_s=0.01)
    )
    # initially disconnected — watcher sees DISCONNECTED.
    await asyncio.sleep(0.02)
    assert bus.status.cloud_link is CloudLinkState.DISCONNECTED

    client.is_connected = True
    await asyncio.sleep(0.05)
    assert bus.status.cloud_link is CloudLinkState.CONNECTED

    client.is_connected = False
    await asyncio.sleep(0.05)
    assert bus.status.cloud_link is CloudLinkState.DISCONNECTED

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio(loop_scope="session")
async def test_watch_grpc_connection_respects_awaiting_activation() -> None:
    """Sticky states (AWAITING_ACTIVATION / AUTH_REJECTED) MUST NOT be
    overwritten by the polling watcher — those are managed by the
    activation flow.
    """
    client = _FakeGrpcClient()  # not connected
    bus = StateBus()
    bus.update(cloud_link=CloudLinkState.AWAITING_ACTIVATION)

    task = asyncio.create_task(
        _watch_grpc_connection(client=client, bus=bus, poll_interval_s=0.01)
    )
    await asyncio.sleep(0.03)
    assert bus.status.cloud_link is CloudLinkState.AWAITING_ACTIVATION

    bus.update(cloud_link=CloudLinkState.AUTH_REJECTED)
    await asyncio.sleep(0.03)
    assert bus.status.cloud_link is CloudLinkState.AUTH_REJECTED

    # When the client actually connects, the watcher should flip to CONNECTED
    # regardless of the sticky-disconnected handling.
    client.is_connected = True
    await asyncio.sleep(0.05)
    assert bus.status.cloud_link is CloudLinkState.CONNECTED

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
