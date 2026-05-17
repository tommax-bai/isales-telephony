"""AT-level tests for SerialATClient.cpcmreg_enable / cpcmreg_disable.

Spec: windows-client-core / device-hardware § "PCM 通道按 SIMCom AT
协议启停 (CPCMREG)" — call SHALL succeed with OK on connected modem,
SHALL raise :class:`PcmEnableError` on ERROR for CPCMREG=1, and SHALL
silently warn on ERROR for CPCMREG=0.

These tests drive ``SerialATClient`` through the same socket-pair
fake-modem fixture used by ``test_serial_at_client.py`` so they exercise
the real ``AtClient`` framing layer end-to-end without hardware.
"""

from __future__ import annotations

import pytest

from isales_telephony.modem_controller.at_client import (
    PcmEnableError,
    SerialATClient,
)
from isales_telephony.modem_controller.drivers import A7670Driver
from isales_telephony.modem_controller.serial_protocol import AtClient
from tests.modem_serial._helpers import feed_lines, make_stream_pair


async def _build_client() -> SerialATClient:
    cli_r, cli_w, _modem_r, modem_w = await make_stream_pair()
    # A7670 init: feed three OKs for ATE0/CMEE/CLIP (same as
    # tests/modem_serial/test_serial_at_client.py).
    await feed_lines(modem_w, "OK", "OK", "OK")
    at = AtClient(cli_r, cli_w, default_timeout=1.0)
    at.start()
    driver = A7670Driver(at)
    await driver.init()
    client = SerialATClient(at, driver, lock_fd=None)
    # Stash the modem side on the client for the test to drive.
    client._test_modem_w = modem_w  # type: ignore[attr-defined]
    return client


@pytest.mark.asyncio
async def test_cpcmreg_enable_ok_returns_silently() -> None:
    client = await _build_client()
    try:
        await feed_lines(client._test_modem_w, "OK")  # type: ignore[attr-defined]
        # Returning without raising IS the contract.
        await client.cpcmreg_enable()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_cpcmreg_enable_error_raises_pcm_enable_error() -> None:
    """Spec: 收到 ERROR MUST raise PcmEnableError so the orchestrator
    can convert to HardwareAlert + hang up. Detail string preserves
    the original AT error code for cloud-side classification."""
    client = await _build_client()
    try:
        await feed_lines(client._test_modem_w, "ERROR")  # type: ignore[attr-defined]
        with pytest.raises(PcmEnableError) as exc_info:
            await client.cpcmreg_enable()
        assert "ERROR" in str(exc_info.value) or exc_info.value.detail
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_cpcmreg_disable_ok_returns_silently() -> None:
    client = await _build_client()
    try:
        await feed_lines(client._test_modem_w, "OK")  # type: ignore[attr-defined]
        await client.cpcmreg_disable()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_cpcmreg_disable_error_logs_warning_no_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Spec: CPCMREG=0 失败 SHALL 记 warning log 但 MUST NOT 阻塞 teardown.

    Teardown is the only caller; reraising would orphan ring buffers /
    bridge resources.
    """
    client = await _build_client()
    try:
        await feed_lines(client._test_modem_w, "ERROR")  # type: ignore[attr-defined]
        with caplog.at_level("WARNING"):
            await client.cpcmreg_disable()  # MUST NOT raise
        assert any(
            "cpcmreg_disable failed" in rec.message for rec in caplog.records
        ), caplog.text
    finally:
        await client.aclose()
