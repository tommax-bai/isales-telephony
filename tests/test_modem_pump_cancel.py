"""Pump-task cancellation: dropping the IPC connection MUST stop the pump.

Spec: device-hardware § engine ↔ modem-controller IPC 协议 — implementation
detail covered by impl-modem-controller change. This test guards against
the regression that prep-stage8-cleanup PR #1 fixed: a stage-2 ``handle_dial``
spawned a fire-and-forget ``_pump`` coroutine that survived the IPC client
disconnect, then later wrote ``device.status`` (via the shared
sessionmaker) at an unpredictable time, polluting downstream tests.

By honouring ``Connection.cancel_event`` inside the pump, the side-effects
stop when the writer is closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from isales_common.enums import DeviceStatus
from isales_common.models import Device
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from isales_telephony.modem_controller.at_client import MockATClient
from isales_telephony.modem_controller.handlers import build_handlers
from isales_telephony.modem_controller.ipc_server import IPCServer


@pytest.fixture(scope="session")
def slow_dial_env() -> AsyncIterator[pytest.MonkeyPatch]:
    mp = pytest.MonkeyPatch()
    # Make the connect path slow enough that the test can disconnect
    # *before* the pump emits ``connected``. Without cancellation, the
    # late ``connected`` event would fire ``_set_device_status(IN_CALL)``.
    mp.setenv("MOCK_DIAL_DELAY_MS", "200")
    mp.setenv("MOCK_CALL_DURATION_MS", "100")
    yield mp
    mp.undo()


@pytest_asyncio.fixture(loop_scope="session")
async def cancel_server(
    slow_dial_env: pytest.MonkeyPatch, clean_engine: AsyncEngine
) -> AsyncIterator[tuple[IPCServer, async_sessionmaker]]:
    sm = async_sessionmaker(clean_engine, expire_on_commit=False)
    handlers = build_handlers(at_client=MockATClient(), sm=sm)
    sock = Path(tempfile.gettempdir()) / f"is-{uuid.uuid4().hex[:8]}.sock"
    server = IPCServer(str(sock), handlers)
    await server.start()
    task = asyncio.create_task(_serve(server))
    try:
        yield server, sm
    finally:
        await server.stop()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        if sock.exists():
            sock.unlink()


async def _serve(server: IPCServer) -> None:
    if server._server is None:  # type: ignore[attr-defined]
        return
    async with server._server:  # type: ignore[attr-defined]
        await server._server.serve_forever()  # type: ignore[attr-defined]


@pytest.mark.asyncio(loop_scope="session")
async def test_pump_does_not_leak_after_disconnect(
    cancel_server: tuple[IPCServer, async_sessionmaker],
) -> None:
    server, sm = cancel_server
    async with sm() as session:
        dev = Device(name="cd1", imei="cd-1", status=DeviceStatus.IDLE)
        session.add(dev)
        await session.commit()
        device_id = dev.id

    reader, writer = await asyncio.open_unix_connection(server.socket_path)
    writer.write(
        json.dumps(
            {
                "cmd": "dial",
                "session_id": "engine-7",
                "device_id": device_id,
                "number": "+8613800138000",
            }
        ).encode()
        + b"\n"
    )
    await writer.drain()
    # Read the synchronous ack so we know dial has been accepted.
    ack = json.loads(await asyncio.wait_for(reader.readline(), timeout=2))
    assert ack["event"] == "dial_ack"

    # Now drop the connection BEFORE MOCK_DIAL_DELAY_MS expires. With
    # cancellation in place the pump bails at its next loop iteration; the
    # device row stays at DIALING (no IN_CALL write).
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()

    # Wait long enough that without cancellation the pump WOULD have
    # transitioned to IN_CALL (200ms dial delay) + a bit of padding.
    await asyncio.sleep(0.4)

    async with sm() as session:
        d = await session.get(Device, device_id)
        assert d is not None
        # Acceptable end states after cancellation:
        # - DIALING (pump killed before connected fired)
        # - IDLE    (pump processed remote_hangup before noticing cancel)
        # MUST NOT be IN_CALL (which would mean connected fired and the
        # pump kept writing past the disconnect).
        assert d.status in (DeviceStatus.DIALING, DeviceStatus.IDLE), (
            f"pump leaked: device.status={d.status}, expected DIALING or IDLE"
        )
