"""Driver-level tests."""

from __future__ import annotations

import pytest

from isales_telephony.modem_controller.serial_protocol import AtClient
from isales_telephony.modem_controller.drivers import (
    A7670Driver,
    DialResult,
    HANGUP_CAUSE_MAP,
    _classify_dial_failure,
    _parse_csq,
    detect_driver,
    get_driver,
)
from tests.modem_serial._helpers import feed_lines, make_stream_pair


def test_get_driver_falls_back_to_a7670() -> None:
    fake_at = object()
    cls = get_driver("totally-unknown", fake_at).__class__  # type: ignore[arg-type]
    assert cls is A7670Driver


def test_parse_csq_returns_rssi() -> None:
    assert _parse_csq(["+CSQ: 22,99"]) == 22
    assert _parse_csq(["junk"]) == 99
    assert _parse_csq([]) == 99


def test_classify_dial_failure_maps_known_phrases() -> None:
    assert _classify_dial_failure("ATD failed: NO CARRIER") == HANGUP_CAUSE_MAP["NO CARRIER"]
    assert _classify_dial_failure("BUSY") == HANGUP_CAUSE_MAP["BUSY"]
    assert _classify_dial_failure("totally weird") == "device_error"


@pytest.mark.asyncio
async def test_a7670_init_sends_canonical_setup() -> None:
    cli_r, cli_w, modem_r, modem_w = await make_stream_pair()
    await feed_lines(modem_w, "OK", "OK", "OK")
    async with AtClient.open_streams(cli_r, cli_w, default_timeout=1.0) as client:
        driver = A7670Driver(client)
        await driver.init()
    sent = await modem_r.read(200)
    decoded = sent.decode("ascii")
    assert "ATE0" in decoded
    assert "AT+CMEE=1" in decoded
    assert "AT+CLIP=1" in decoded


@pytest.mark.asyncio
async def test_a7670_dial_returns_connected_on_ok() -> None:
    cli_r, cli_w, _, modem_w = await make_stream_pair()
    await feed_lines(modem_w, "OK")
    async with AtClient.open_streams(cli_r, cli_w, default_timeout=1.0) as client:
        driver = A7670Driver(client)
        result = await driver.dial("13800138000")
    assert result == DialResult(connected=True)


@pytest.mark.asyncio
async def test_a7670_dial_maps_failure_to_hangup_cause() -> None:
    cli_r, cli_w, _, _ = await make_stream_pair()
    async with AtClient.open_streams(cli_r, cli_w, default_timeout=0.05) as client:
        driver = A7670Driver(client)
        result = await driver.dial("13800138000", timeout=0.05)
    assert result.connected is False
    assert result.hangup_cause is not None


@pytest.mark.asyncio
async def test_detect_driver_identifies_simcom_a7670() -> None:
    cli_r, cli_w, _, modem_w = await make_stream_pair()
    await feed_lines(modem_w, "SIMCOM_Ltd", "OK", "SIMCOM_A7670C", "OK")
    async with AtClient.open_streams(cli_r, cli_w, default_timeout=1.0) as client:
        driver = await detect_driver(client)
    assert isinstance(driver, A7670Driver)
