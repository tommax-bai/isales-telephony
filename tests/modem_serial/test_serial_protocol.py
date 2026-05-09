"""AT client unit tests via socket-pair backed asyncio streams.

A `socket.socketpair()` gives us two real asyncio streams; tests script
modem responses by writing to the "modem side" and assert what the AT
client wrote back. This avoids hand-rolling Transport/Protocol mocks.
"""

from __future__ import annotations

import asyncio

import pytest

from isales_telephony.modem_controller.serial_protocol import (
    AtClient,
    AtCommandError,
    AtTimeoutError,
    Urc,
    UrcType,
    _classify_urc,
)
from tests.modem_serial._helpers import feed_lines, make_stream_pair


@pytest.mark.asyncio
async def test_send_returns_response_lines_then_ok() -> None:
    cli_r, cli_w, modem_r, modem_w = await make_stream_pair()
    await feed_lines(modem_w, "+CSQ: 18,99", "OK")
    async with AtClient.open_streams(cli_r, cli_w, default_timeout=1.0) as client:
        response = await client.send("AT+CSQ")
    assert response.ok is True
    assert response.lines == ["+CSQ: 18,99"]
    sent = await modem_r.readuntil(b"\r")
    assert sent == b"AT+CSQ\r"


@pytest.mark.asyncio
async def test_send_raises_on_error_terminal() -> None:
    cli_r, cli_w, _, modem_w = await make_stream_pair()
    await feed_lines(modem_w, "ERROR")
    async with AtClient.open_streams(cli_r, cli_w, default_timeout=1.0) as client:
        with pytest.raises(AtCommandError) as ei:
            await client.send("ATD123;")
    assert ei.value.code == "ERROR"


@pytest.mark.asyncio
async def test_send_raises_on_cme_error() -> None:
    cli_r, cli_w, _, modem_w = await make_stream_pair()
    await feed_lines(modem_w, "+CME ERROR: 30")
    async with AtClient.open_streams(cli_r, cli_w, default_timeout=1.0) as client:
        with pytest.raises(AtCommandError) as ei:
            await client.send("AT+CGSN")
    assert ei.value.code == "+CME ERROR"
    assert ei.value.detail == "30"


@pytest.mark.asyncio
async def test_send_times_out_when_no_response_arrives() -> None:
    cli_r, cli_w, _, _ = await make_stream_pair()
    async with AtClient.open_streams(cli_r, cli_w, default_timeout=0.1) as client:
        with pytest.raises(AtTimeoutError):
            await client.send("AT")


@pytest.mark.asyncio
async def test_urc_handler_fires_for_ring_and_no_carrier() -> None:
    cli_r, cli_w, _, modem_w = await make_stream_pair()
    seen: list[Urc] = []

    async with AtClient.open_streams(cli_r, cli_w, default_timeout=1.0) as client:
        async def handler(urc: Urc) -> None:
            seen.append(urc)

        client.on_urc(handler)
        # Push URCs first, then a real OK to terminate a no-op AT command.
        await feed_lines(modem_w, "RING", "NO CARRIER", "OK")
        await client.send("AT")
        # Yield once so URC dispatch tasks complete.
        await asyncio.sleep(0)

    types = {urc.type for urc in seen}
    assert UrcType.RING in types
    assert UrcType.NO_CARRIER in types


def test_classify_urc_recognises_known_prefixes() -> None:
    assert _classify_urc("RING") == UrcType.RING
    assert _classify_urc("BUSY") == UrcType.BUSY
    assert _classify_urc('+CLIP: "13800138000",129') == UrcType.CLIP
    assert _classify_urc("+CREG: 0,1") == UrcType.CREG
    assert _classify_urc("OK") is None
    assert _classify_urc("garbage") is None
