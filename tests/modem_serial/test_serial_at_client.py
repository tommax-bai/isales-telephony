"""Unit tests for SerialATClient (impl-real-at).

Spec: device-hardware § "ATClient 实现策略" + § "URC → ATEvent 翻译契约"
      + call-state-machine § "真硬件 URC 驱动状态转换".

These tests drive SerialATClient through a socket-pair "fake modem" rather
than a real serial port, so they run in CI without hardware. The fake-modem
script (lines fed via ``feed_lines``) covers every URC / failure path the
spec enumerates.

Real-hardware coverage is provided by ``scripts/at_smoke.py`` and the
impl-real-at acceptance log; not in this CI path.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from isales_telephony.modem_controller.at_client import (
    ATEvent,
    SerialATClient,
)
from isales_telephony.modem_controller.drivers import A7670Driver
from isales_telephony.modem_controller.serial_protocol import AtClient
from tests.modem_serial._helpers import feed_lines, make_stream_pair


async def _drain(
    stream: AsyncIterator[ATEvent], *, max_events: int = 5, timeout: float = 5.0
) -> list[ATEvent]:
    """Pull up to ``max_events`` from ``stream`` with an overall timeout."""

    out: list[ATEvent] = []

    async def _pull() -> None:
        async for ev in stream:
            out.append(ev)
            if len(out) >= max_events:
                return

    await asyncio.wait_for(_pull(), timeout=timeout)
    return out


async def _build_client(
    modem_w: asyncio.StreamWriter, cli_r: asyncio.StreamReader, cli_w: asyncio.StreamWriter
) -> tuple[SerialATClient, AtClient]:
    # A7670 init: feed three OKs for ATE0/CMEE/CLIP.
    await feed_lines(modem_w, "OK", "OK", "OK")
    at = AtClient(cli_r, cli_w, default_timeout=1.0)
    at.start()
    driver = A7670Driver(at)
    await driver.init()
    client = SerialATClient(at, driver, lock_fd=None)
    return client, at


@pytest.mark.asyncio
async def test_dial_ok_then_clcc_active_yields_connected() -> None:
    cli_r, cli_w, modem_r, modem_w = await make_stream_pair()
    client, at = await _build_client(modem_w, cli_r, cli_w)
    # Speed up the CLCC poller so the test finishes quickly.
    client._CLCC_POLL_INTERVAL_S = 0.05  # type: ignore[attr-defined]
    try:
        # ATD<n>; → OK; then AT+CLCC poll returns active call line.
        await feed_lines(modem_w, "OK")  # ATD ack
        # First CLCC poll returns active call (state=0).
        await feed_lines(modem_w, '+CLCC: 1,0,0,0,0,"13800138000",129', "OK")

        call_id, stream = await client.dial("13800138000")
        assert isinstance(call_id, str) and len(call_id) > 0

        # Drain until connected event arrives.
        events: list[ATEvent] = []

        async def _pull_one() -> None:
            async for ev in stream:
                events.append(ev)
                if ev.event == "connected":
                    return

        await asyncio.wait_for(_pull_one(), timeout=2.0)
        assert events[-1].event == "connected"
        assert events[-1].call_id == call_id
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_no_carrier_urc_yields_remote_hangup_user_hangup() -> None:
    cli_r, cli_w, modem_r, modem_w = await make_stream_pair()
    client, _ = await _build_client(modem_w, cli_r, cli_w)
    client._CLCC_POLL_INTERVAL_S = 5.0  # type: ignore[attr-defined] — keep poller idle
    try:
        await feed_lines(modem_w, "OK")  # ATD ack
        call_id, stream = await client.dial("13800138000")
        # Far side hangs up.
        await feed_lines(modem_w, "NO CARRIER")
        events = await _drain(stream, max_events=1, timeout=2.0)
        assert events == [ATEvent("remote_hangup", call_id, cause="user_hangup")]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_busy_urc_yields_user_busy() -> None:
    cli_r, cli_w, modem_r, modem_w = await make_stream_pair()
    client, _ = await _build_client(modem_w, cli_r, cli_w)
    client._CLCC_POLL_INTERVAL_S = 5.0  # type: ignore[attr-defined]
    try:
        await feed_lines(modem_w, "OK")
        call_id, stream = await client.dial("13800138000")
        await feed_lines(modem_w, "BUSY")
        events = await _drain(stream, max_events=1, timeout=2.0)
        assert events == [ATEvent("remote_hangup", call_id, cause="user_busy")]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_no_answer_urc_yields_no_answer() -> None:
    cli_r, cli_w, modem_r, modem_w = await make_stream_pair()
    client, _ = await _build_client(modem_w, cli_r, cli_w)
    client._CLCC_POLL_INTERVAL_S = 5.0  # type: ignore[attr-defined]
    try:
        await feed_lines(modem_w, "OK")
        call_id, stream = await client.dial("13800138000")
        await feed_lines(modem_w, "NO ANSWER")
        events = await _drain(stream, max_events=1, timeout=2.0)
        assert events == [ATEvent("remote_hangup", call_id, cause="no_answer")]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_no_dialtone_urc_yields_network_out_of_order() -> None:
    cli_r, cli_w, modem_r, modem_w = await make_stream_pair()
    client, _ = await _build_client(modem_w, cli_r, cli_w)
    client._CLCC_POLL_INTERVAL_S = 5.0  # type: ignore[attr-defined]
    try:
        await feed_lines(modem_w, "OK")
        call_id, stream = await client.dial("13800138000")
        await feed_lines(modem_w, "NO DIALTONE")
        events = await _drain(stream, max_events=1, timeout=2.0)
        assert events == [ATEvent("remote_hangup", call_id, cause="network_out_of_order")]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_atd_timeout_yields_immediate_remote_hangup() -> None:
    """ATD with no OK in deadline → DialResult.connected=False, immediate hangup event."""

    cli_r, cli_w, _modem_r, _modem_w = await make_stream_pair()
    # Build client manually with a short driver timeout so ATD ack times out.
    await feed_lines(_modem_w, "OK", "OK", "OK")  # init
    at = AtClient(cli_r, cli_w, default_timeout=0.05)
    at.start()
    driver = A7670Driver(at)
    await driver.init()
    client = SerialATClient(at, driver, lock_fd=None)
    client._ATD_ACK_TIMEOUT_S = 0.1  # type: ignore[attr-defined]
    try:
        # No OK for ATD → driver.dial returns connected=False with a cause.
        call_id, stream = await client.dial("13800138000")
        events = await _drain(stream, max_events=1, timeout=2.0)
        assert len(events) == 1
        assert events[0].event == "remote_hangup"
        assert events[0].call_id == call_id
        assert events[0].cause  # mapped to some hangup_cause
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_hangup_emits_manual_hangup() -> None:
    cli_r, cli_w, modem_r, modem_w = await make_stream_pair()
    client, _ = await _build_client(modem_w, cli_r, cli_w)
    client._CLCC_POLL_INTERVAL_S = 5.0  # type: ignore[attr-defined]
    try:
        await feed_lines(modem_w, "OK")  # ATD ack
        call_id, stream = await client.dial("13800138000")

        # Schedule hangup after a brief delay so we can iterate the stream.
        async def _delayed_hangup() -> None:
            await asyncio.sleep(0.05)
            # ATH response
            await feed_lines(modem_w, "OK")
            await client.hangup(call_id)

        asyncio.create_task(_delayed_hangup())
        events = await _drain(stream, max_events=1, timeout=2.0)
        assert events == [
            ATEvent("remote_hangup", call_id, cause="manual_hangup")
        ]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_hangup_is_idempotent_for_unknown_call_id() -> None:
    cli_r, cli_w, modem_r, modem_w = await make_stream_pair()
    client, _ = await _build_client(modem_w, cli_r, cli_w)
    try:
        # No active call → hangup with arbitrary id must be a no-op (no exception,
        # no AT command sent).
        await client.hangup("nonexistent-call-id")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_dial_refused() -> None:
    cli_r, cli_w, modem_r, modem_w = await make_stream_pair()
    client, _ = await _build_client(modem_w, cli_r, cli_w)
    client._CLCC_POLL_INTERVAL_S = 5.0  # type: ignore[attr-defined]
    try:
        await feed_lines(modem_w, "OK")  # first ATD ack
        call_id1, stream1 = await client.dial("13800138000")
        with pytest.raises(RuntimeError, match="another call is active"):
            await client.dial("13900139000")
        # Cleanup: emit hangup on first stream so aclose() doesn't block.
        await feed_lines(modem_w, "NO CARRIER")
        await _drain(stream1, max_events=1, timeout=2.0)
        _ = call_id1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_signal_iccid_imei_delegate_to_driver() -> None:
    cli_r, cli_w, modem_r, modem_w = await make_stream_pair()
    client, _ = await _build_client(modem_w, cli_r, cli_w)
    try:
        # AT+CSQ → +CSQ: 22,99 / OK
        await feed_lines(modem_w, "+CSQ: 22,99", "OK")
        assert await client.get_signal() == 22
        # AT+CCID → +CCID: "89860..." / OK
        await feed_lines(modem_w, '+CCID: "89860000000000000000"', "OK")
        iccid = await client.get_iccid()
        assert iccid == "89860000000000000000"
        # AT+CGSN → bare IMEI / OK
        await feed_lines(modem_w, "350000000000000", "OK")
        imei = await client.get_imei()
        assert imei == "350000000000000"
    finally:
        await client.aclose()


def test_clcc_parser_detects_active_call() -> None:
    from isales_telephony.modem_controller.at_client import _clcc_has_active_call

    assert _clcc_has_active_call(['+CLCC: 1,0,0,0,0,"138",129'])
    # state=2 (dialing) — not active yet
    assert not _clcc_has_active_call(['+CLCC: 1,0,2,0,0,"138",129'])
    # state=3 (alerting) — not active yet
    assert not _clcc_has_active_call(['+CLCC: 1,0,3,0,0,"138",129'])
    # Empty / non-CLCC noise
    assert not _clcc_has_active_call([])
    assert not _clcc_has_active_call(["OK", "ERROR"])
