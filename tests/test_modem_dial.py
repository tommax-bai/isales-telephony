"""End-to-end test for IPC dial flow + device status state machine."""

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


@pytest_asyncio.fixture(loop_scope="session")
async def dial_server(
    monkeypatch_session: pytest.MonkeyPatch, clean_engine: AsyncEngine
) -> AsyncIterator[tuple[IPCServer, async_sessionmaker]]:
    monkeypatch_session.setenv("MOCK_DIAL_DELAY_MS", "50")
    monkeypatch_session.setenv("MOCK_CALL_DURATION_MS", "100")

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


@pytest.fixture(scope="session")
def monkeypatch_session() -> AsyncIterator[pytest.MonkeyPatch]:
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest.mark.asyncio(loop_scope="session")
async def test_dial_emits_connected_then_hangup_and_flips_status(
    dial_server: tuple[IPCServer, async_sessionmaker],
) -> None:
    server, sm = dial_server

    # Seed an idle device.
    async with sm() as session:
        dev = Device(name="d1", imei="dev-imei", status=DeviceStatus.IDLE)
        session.add(dev)
        await session.commit()
        device_id = dev.id

    reader, writer = await asyncio.open_unix_connection(server.socket_path)
    try:
        writer.write(
            json.dumps(
                {"cmd": "dial", "device_id": device_id, "number": "+8613800138000"}
            ).encode()
            + b"\n"
        )
        await writer.drain()

        # Read three responses: dial_ack, connected, remote_hangup
        async def _read_msg() -> dict[str, object]:
            line = await asyncio.wait_for(reader.readline(), timeout=2)
            return dict(json.loads(line))

        ack = await _read_msg()
        assert ack["event"] == "dial_ack"
        assert isinstance(ack["session_id"], str)
        # PR #2 of prep-stage8-cleanup removes ``call_id`` from wire frames —
        # session_id is the single correlation field per spec § engine ↔
        # modem-controller IPC 协议.
        assert "call_id" not in ack

        # Right after dial, device.status should be DIALING.
        async with sm() as session:
            d = await session.get(Device, device_id)
            assert d is not None
            assert d.status == DeviceStatus.DIALING

        connected = await _read_msg()
        assert connected["event"] == "connected"
        assert connected["session_id"] == ack["session_id"]
        assert connected["device_id"] == device_id
        assert "call_id" not in connected

        # status is now IN_CALL
        async with sm() as session:
            d = await session.get(Device, device_id)
            assert d is not None
            assert d.status == DeviceStatus.IN_CALL

        hangup = await _read_msg()
        assert hangup["event"] == "remote_hangup"
        assert hangup["session_id"] == ack["session_id"]
        assert hangup["cause"] == "normal_clearing"
        assert "call_id" not in hangup

        # status back to IDLE
        async with sm() as session:
            d = await session.get(Device, device_id)
            assert d is not None
            assert d.status == DeviceStatus.IDLE
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


@pytest.mark.asyncio(loop_scope="session")
async def test_hangup_command_short_circuits_call(
    dial_server: tuple[IPCServer, async_sessionmaker],
) -> None:
    server, sm = dial_server
    async with sm() as session:
        dev = Device(name="dh", imei="dev-imei-h", status=DeviceStatus.IDLE)
        session.add(dev)
        await session.commit()
        device_id = dev.id

    reader, writer = await asyncio.open_unix_connection(server.socket_path)
    try:
        writer.write(
            json.dumps(
                {"cmd": "dial", "device_id": device_id, "number": "+861380013800X"}
            ).encode()
            + b"\n"
        )
        await writer.drain()
        ack = json.loads(await asyncio.wait_for(reader.readline(), timeout=2))
        session_id = ack["session_id"]

        # immediately hang up — before MOCK_DIAL_DELAY_MS elapses
        writer.write(
            json.dumps({"cmd": "hangup", "session_id": session_id}).encode() + b"\n"
        )
        await writer.drain()
        # hangup_ack arrives synchronously and echoes the same session_id
        ack2 = json.loads(await asyncio.wait_for(reader.readline(), timeout=2))
        assert ack2 == {"event": "hangup_ack", "session_id": session_id}

        # The dial event stream should now emit remote_hangup with manual_hangup
        # (canonical HangupCause; impl-real-at renamed local_clearing → manual_hangup;
        # no connected event since we cancelled before the delay).
        evt = json.loads(await asyncio.wait_for(reader.readline(), timeout=2))
        assert evt["event"] == "remote_hangup"
        assert evt["cause"] == "manual_hangup"
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
