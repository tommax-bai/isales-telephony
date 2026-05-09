"""Test helpers: socket-pair backed StreamReader/Writer.

Using a real socket lets asyncio set up its standard StreamProtocol +
Transport pair (with proper _drain_helper etc.), so we exercise the same
code path that pyserial-asyncio uses in production. The remote end of the
pair is exposed for tests to script modem responses on.
"""

from __future__ import annotations

import asyncio
import socket


async def make_stream_pair() -> tuple[
    asyncio.StreamReader,
    asyncio.StreamWriter,
    asyncio.StreamReader,
    asyncio.StreamWriter,
]:
    """Build two connected stream endpoints, a la `(client_r/w, server_r/w)`."""

    sock_a, sock_b = socket.socketpair()
    a_reader, a_writer = await asyncio.open_connection(sock=sock_a)
    b_reader, b_writer = await asyncio.open_connection(sock=sock_b)
    return a_reader, a_writer, b_reader, b_writer


async def feed_lines(writer: asyncio.StreamWriter, *lines: str) -> None:
    """Push CRLF-terminated lines into ``writer`` (modem side)."""

    for line in lines:
        writer.write((line + "\r\n").encode("ascii"))
    await writer.drain()
