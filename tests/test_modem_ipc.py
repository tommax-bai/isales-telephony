"""IPC server tests — newline-delimited JSON framing + dispatch + edge cases."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from isales_telephony.modem_controller.handlers import DEFAULT_HANDLERS
from isales_telephony.modem_controller.ipc_server import (
    MAX_FRAME_BYTES,
    IPCServer,
)


@pytest_asyncio.fixture(loop_scope="session")
async def ipc_server() -> AsyncIterator[IPCServer]:
    # macOS AF_UNIX path is ~104 chars; pytest tmp_path is too deep, so put
    # the socket in /tmp directly with a short name.
    import tempfile
    import uuid

    sock_path = Path(tempfile.gettempdir()) / f"is-{uuid.uuid4().hex[:8]}.sock"
    server = IPCServer(str(sock_path), DEFAULT_HANDLERS)
    await server.start()
    task = asyncio.create_task(_serve_in_background(server))
    import contextlib

    try:
        yield server
    finally:
        await server.stop()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        if sock_path.exists():
            sock_path.unlink()


async def _serve_in_background(server: IPCServer) -> None:
    # serve() will block; we want to keep accepting new connections in the
    # background while tests run.
    if server._server is None:  # type: ignore[attr-defined]
        return
    async with server._server:  # type: ignore[attr-defined]
        await server._server.serve_forever()  # type: ignore[attr-defined]


async def _connect(server: IPCServer) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_unix_connection(server.socket_path)


@pytest.mark.asyncio(loop_scope="session")
async def test_status_round_trip(ipc_server: IPCServer) -> None:
    reader, writer = await _connect(ipc_server)
    writer.write(b'{"cmd":"status"}\n')
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=2)
    msg = json.loads(line)
    assert msg == {"event": "status", "status": "ok"}
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_cmd_returns_error(ipc_server: IPCServer) -> None:
    reader, writer = await _connect(ipc_server)
    writer.write(b'{"cmd":"nope"}\n')
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=2)
    msg = json.loads(line)
    assert msg == {"error": "unknown_cmd", "cmd": "nope"}
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio(loop_scope="session")
async def test_incomplete_frame_closes_connection(ipc_server: IPCServer) -> None:
    reader, writer = await _connect(ipc_server)
    writer.write(b'{"cmd":"status"')  # no trailing newline, no closing brace either
    await writer.drain()
    writer.write_eof()
    # Server detects incomplete frame and closes; readline returns b"".
    rest = await asyncio.wait_for(reader.read(), timeout=2)
    assert rest == b""
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio(loop_scope="session")
async def test_invalid_json_closes(ipc_server: IPCServer) -> None:
    reader, writer = await _connect(ipc_server)
    writer.write(b"not-json\n")
    await writer.drain()
    rest = await asyncio.wait_for(reader.read(), timeout=2)
    assert rest == b""
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio(loop_scope="session")
async def test_frame_size_limit_constant() -> None:
    # 1 MiB exactly per spec.
    assert MAX_FRAME_BYTES == 1 * 1024 * 1024


@pytest.mark.asyncio(loop_scope="session")
async def test_socket_path_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ISALES_MODEM_SOCKET", str(tmp_path / "x.sock"))
    from isales_telephony.modem_controller.main import _socket_path

    assert _socket_path() == str(tmp_path / "x.sock")
    monkeypatch.delenv("ISALES_MODEM_SOCKET")
    assert _socket_path() == "/var/run/isales/modem.sock"
    # silence ruff about unused
    assert os.path.basename(_socket_path()) == "modem.sock"
