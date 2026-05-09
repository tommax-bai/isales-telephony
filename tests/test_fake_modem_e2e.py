"""End-to-end smoke: real IPCServer + (mock) AT client + heartbeat path.

Spec: device-hardware § engine ↔ modem-controller IPC 协议 +
      § modem-controller 心跳与失联探测.

We deliberately don't use real ALSA / pyserial here — the goal is to
exercise the *control plane* end-to-end so a hardware regression on the
serial side doesn't sneak in via API drift. PR #11 (real hardware
acceptance) is what actually validates the audio path.
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


@pytest_asyncio.fixture(loop_scope="session")
async def fake_modem_stack(
    monkeypatch_fast_dial: pytest.MonkeyPatch, clean_engine: AsyncEngine
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


@pytest.fixture(scope="session")
def monkeypatch_fast_dial() -> AsyncIterator[pytest.MonkeyPatch]:
    mp = pytest.MonkeyPatch()
    mp.setenv("MOCK_DIAL_DELAY_MS", "20")
    mp.setenv("MOCK_CALL_DURATION_MS", "40")
    yield mp
    mp.undo()


@pytest.mark.asyncio(loop_scope="session")
async def test_session_id_round_trip_through_ipc(
    fake_modem_stack: tuple[IPCServer, async_sessionmaker],
) -> None:
    """Engine-style flow: send {cmd:dial, session_id}, observe events keyed by it."""

    server, sm = fake_modem_stack
    async with sm() as session:
        dev = Device(name="fm1", imei="fm-1", status=DeviceStatus.IDLE)
        session.add(dev)
        await session.commit()
        device_id = dev.id

    reader, writer = await asyncio.open_unix_connection(server.socket_path)
    try:
        writer.write(
            json.dumps(
                {
                    "cmd": "dial",
                    "session_id": "engine-call-7",
                    "device_id": device_id,
                    "number": "+8613800138000",
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()

        async def _read() -> dict[str, object]:
            line = await asyncio.wait_for(reader.readline(), timeout=2)
            return dict(json.loads(line))

        ack = await _read()
        assert ack["event"] == "dial_ack"
        assert ack["session_id"] == "engine-call-7"

        connected = await _read()
        assert connected["event"] == "connected"
        assert connected["session_id"] == "engine-call-7"

        hangup = await _read()
        assert hangup["event"] == "remote_hangup"
        assert hangup["session_id"] == "engine-call-7"

        async with sm() as session:
            d = await session.get(Device, device_id)
            assert d is not None
            assert d.status == DeviceStatus.IDLE
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


@pytest.mark.asyncio(loop_scope="session")
async def test_legacy_clients_without_session_id_still_work(
    fake_modem_stack: tuple[IPCServer, async_sessionmaker],
) -> None:
    """Stage-2 callers (no session_id field) MUST keep working — backward compat."""

    server, sm = fake_modem_stack
    async with sm() as session:
        dev = Device(name="fm2", imei="fm-2", status=DeviceStatus.IDLE)
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
        assert ack["event"] == "dial_ack"
        # No session_id in ack since none was supplied.
        assert "session_id" not in ack
        # Drain connected + remote_hangup so the handler's pump task ends
        # before the fixture tears down — otherwise the pump survives this
        # test and races with downstream tests' device.status writes via
        # the shared sessionmaker.
        for _ in range(2):
            await asyncio.wait_for(reader.readline(), timeout=2)
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
