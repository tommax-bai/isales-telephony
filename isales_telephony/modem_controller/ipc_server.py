"""Unix-socket IPC server for engine ↔ modem-controller.

Wire format (per spec ``device-hardware`` § IPC 帧格式 — also surfaced in this
change's specs/device-hardware/spec.md):

- Newline-delimited JSON: each line is one complete JSON message.
- Single message ≤ 1 MiB. Larger frames close the connection.
- Incomplete frame at EOF (no trailing ``\\n``) closes the connection.
- Bidirectional independent streams — request/response not strictly paired.

Handlers receive the parsed dict and return either a dict (sent back to the
peer as a single line) or ``None`` (no immediate reply). Async events from
the modem (e.g. ``connected``, ``remote_hangup``) are pushed to the connection
via :meth:`Connection.send` from the dial handler's spawned task.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

MAX_FRAME_BYTES = 1 * 1024 * 1024

Handler = Callable[["Connection", dict[str, Any]], Awaitable[dict[str, Any] | None]]


class FrameError(Exception):
    """Wire-format violation — caller closes the connection."""


class Connection:
    """One peer connection. Owns its writer; safe to call ``send`` from anywhere."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self._send_lock = asyncio.Lock()

    async def send(self, msg: dict[str, Any]) -> None:
        line = json.dumps(msg, ensure_ascii=False).encode() + b"\n"
        if len(line) > MAX_FRAME_BYTES:
            raise FrameError(f"outbound frame {len(line)} bytes > {MAX_FRAME_BYTES}")
        async with self._send_lock:
            self.writer.write(line)
            await self.writer.drain()

    @property
    def peer(self) -> str:
        info = self.writer.get_extra_info("peername") or self.writer.get_extra_info("sockname")
        return str(info)


class IPCServer:
    def __init__(self, socket_path: str, handlers: Mapping[str, Handler]) -> None:
        self.socket_path = socket_path
        self.handlers = handlers
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(
            self._on_connect, path=self.socket_path
        )
        logger.info("ipc_server listening on %s", self.socket_path)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _on_connect(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        conn = Connection(reader, writer)
        logger.info("ipc_server: client connected (%s)", conn.peer)
        try:
            await self._read_loop(conn)
        except FrameError as exc:
            logger.warning("ipc_server: protocol error from %s: %s", conn.peer, exc)
        except Exception:
            logger.exception("ipc_server: handler error")
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            logger.info("ipc_server: client disconnected (%s)", conn.peer)

    async def _read_loop(self, conn: Connection) -> None:
        while True:
            try:
                line = await conn.reader.readuntil(b"\n")
            except asyncio.IncompleteReadError as exc:
                if exc.partial:
                    raise FrameError(
                        f"incomplete frame at EOF ({len(exc.partial)} bytes, no terminator)"
                    ) from exc
                return  # clean EOF
            except asyncio.LimitOverrunError as exc:
                raise FrameError(f"frame exceeds reader buffer: {exc}") from exc

            if len(line) > MAX_FRAME_BYTES:
                raise FrameError(f"inbound frame {len(line)} bytes > {MAX_FRAME_BYTES}")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                raise FrameError(f"invalid JSON: {exc}") from exc
            if not isinstance(msg, dict):
                raise FrameError(f"frame must be JSON object, got {type(msg).__name__}")
            cmd = msg.get("cmd")
            if not isinstance(cmd, str):
                raise FrameError("frame missing 'cmd' string")

            handler = self.handlers.get(cmd)
            if handler is None:
                await conn.send({"error": "unknown_cmd", "cmd": cmd})
                continue
            reply = await handler(conn, msg)
            if reply is not None:
                await conn.send(reply)
