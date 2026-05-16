"""Unit tests for ``modem_controller.at_probe``.

Drives the probe / identity / coordinator from a socket-pair connector
so no real serial port is needed — the tests run identically on macOS
dev hosts, Linux CI, and the Windows D1 target.

Covers (windows-client-core tasks 3.3 / 3.4 / 3.5):

- ``probe_at_channel`` — OK / no-OK / IO error.
- ``read_modem_identity`` — full happy path, per-stage failure, +CME
  ERROR mapping, CCID → ICCID fallback.
- ``RateLimitedProber`` — allow/record, custom clock, interval=0 disabled.
- ``identify_modem_channel`` — VID:PID whitelist short-circuit,
  whitelist-miss + rate-limit path, multi-COM-port modem (only AT
  channel returns OK), init-failure folded into IdentifyResult so the
  caller can emit a HardwareAlert.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from isales_telephony.modem_controller.at_probe import (
    DEFAULT_PROBE_INTERVAL_S,
    IdentifyResult,
    ModemIdentity,
    ModemInitFailedError,
    RateLimitedProber,
    identify_modem_channel,
    probe_at_channel,
    read_modem_identity,
)

from tests.modem_serial._helpers import feed_lines, make_stream_pair


# ----------------------------------------------------------------- connectors


async def _make_scripted_connector(
    script: dict[str, Callable[[asyncio.StreamWriter], Awaitable[None]]],
):
    """Build a :class:`SerialConnector` whose modem side is scripted.

    ``script[device_node]`` is an async callable that receives the
    modem-side ``StreamWriter`` and writes whatever the modem should
    say. It is invoked **before** the client sends its command, so for
    AT probing the test can either:

    1. Pre-feed ``OK`` synchronously (modem buffered up the response in
       advance), or
    2. Wait for the command echo on the modem-side reader (advanced;
       not used here — async pre-feed is enough for these tests).
    """

    opened: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []

    @contextlib.asynccontextmanager
    async def connector(
        device_node: str, baudrate: int
    ) -> AsyncIterator[tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
        if device_node not in script:
            raise OSError(f"unscripted device_node={device_node!r}")
        cli_r, cli_w, modem_r, modem_w = await make_stream_pair()
        opened.append((modem_r, modem_w))
        await script[device_node](modem_w)
        try:
            yield cli_r, cli_w
        finally:
            cli_w.close()
            with contextlib.suppress(Exception):
                await cli_w.wait_closed()
            modem_w.close()
            with contextlib.suppress(Exception):
                await modem_w.wait_closed()

    connector.opened = opened  # type: ignore[attr-defined]
    return connector


def _script_ok() -> Callable[[asyncio.StreamWriter], Awaitable[None]]:
    async def _push(w: asyncio.StreamWriter) -> None:
        await feed_lines(w, "OK")
    return _push


def _script_silent() -> Callable[[asyncio.StreamWriter], Awaitable[None]]:
    async def _push(_: asyncio.StreamWriter) -> None:
        return None
    return _push


def _script_lines(*lines: str) -> Callable[[asyncio.StreamWriter], Awaitable[None]]:
    async def _push(w: asyncio.StreamWriter) -> None:
        await feed_lines(w, *lines)
    return _push


# A streamer that scripts a full identity sequence onto a single open
# connection. probe + identity_read both call ``connector(...)`` but
# ``identify_modem_channel`` calls them in **two separate** ``async with``
# blocks (when whitelist miss → probe → identity), so we need the
# connector to script the identity batch only on the second open.


def _script_full_identity(
    *, manufacturer: str, model: str, imei: str, iccid: str
) -> Callable[[asyncio.StreamWriter], Awaitable[None]]:
    async def _push(w: asyncio.StreamWriter) -> None:
        await feed_lines(
            w,
            manufacturer,
            "OK",
            model,
            "OK",
            imei,
            "OK",
            iccid,
            "OK",
        )
    return _push


def _staged_connector(
    *scripts_per_open: Callable[[asyncio.StreamWriter], Awaitable[None]],
):
    """Connector that pops one script per ``connector()`` invocation.

    Useful for ``identify_modem_channel`` whitelist-miss path:
    ``open 1 = probe (push OK)``, ``open 2 = identity sequence``.
    """

    @contextlib.asynccontextmanager
    async def connector(
        device_node: str, baudrate: int
    ) -> AsyncIterator[tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
        if not scripts_per_open_state["queue"]:
            raise AssertionError(
                f"connector invoked {scripts_per_open_state['calls']+1} times"
                f" but only {len(scripts_per_open)} scripts queued"
            )
        push = scripts_per_open_state["queue"].pop(0)
        scripts_per_open_state["calls"] += 1
        cli_r, cli_w, modem_r, modem_w = await make_stream_pair()
        await push(modem_w)
        try:
            yield cli_r, cli_w
        finally:
            cli_w.close()
            with contextlib.suppress(Exception):
                await cli_w.wait_closed()
            modem_w.close()
            with contextlib.suppress(Exception):
                await modem_w.wait_closed()

    scripts_per_open_state: dict = {
        "queue": list(scripts_per_open),
        "calls": 0,
    }
    connector.state = scripts_per_open_state  # type: ignore[attr-defined]
    return connector


# ============================================================ RateLimitedProber


class _FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_rate_limiter_first_call_allowed() -> None:
    p = RateLimitedProber(60.0, clock=_FakeClock(0.0))
    assert p.allow("COM3") is True


def test_rate_limiter_blocks_within_window() -> None:
    clock = _FakeClock(0.0)
    p = RateLimitedProber(60.0, clock=clock)
    p.record("COM3")
    clock.t = 30.0
    assert p.allow("COM3") is False


def test_rate_limiter_allows_after_window_elapses() -> None:
    clock = _FakeClock(0.0)
    p = RateLimitedProber(60.0, clock=clock)
    p.record("COM3")
    clock.t = 60.0
    assert p.allow("COM3") is True


def test_rate_limiter_per_device_independent() -> None:
    clock = _FakeClock(0.0)
    p = RateLimitedProber(60.0, clock=clock)
    p.record("COM3")
    # COM4 has no history → allowed even at t=0.
    assert p.allow("COM4") is True
    assert p.allow("COM3") is False


def test_rate_limiter_forget_resets_device() -> None:
    clock = _FakeClock(0.0)
    p = RateLimitedProber(60.0, clock=clock)
    p.record("COM3")
    p.forget("COM3")
    assert p.allow("COM3") is True


def test_rate_limiter_interval_zero_always_allows() -> None:
    clock = _FakeClock(0.0)
    p = RateLimitedProber(0.0, clock=clock)
    p.record("COM3")
    # Stays at t=0; interval 0 means "no limit" — must allow immediately.
    assert p.allow("COM3") is True


def test_rate_limiter_rejects_negative_interval() -> None:
    with pytest.raises(ValueError):
        RateLimitedProber(-1.0)


# ============================================================ probe_at_channel


async def test_probe_returns_true_when_ok_arrives() -> None:
    connector = await _make_scripted_connector({"COM3": _script_ok()})
    assert await probe_at_channel(connector, "COM3", timeout=0.5) is True


async def test_probe_returns_false_on_silence() -> None:
    connector = await _make_scripted_connector({"COM3": _script_silent()})
    assert await probe_at_channel(connector, "COM3", timeout=0.05) is False


async def test_probe_returns_false_on_error_response() -> None:
    """A modem-like ``ERROR`` reply to a bare ``AT`` is not "an AT channel"
    in the spec sense — we treat it as "not OK" so the watcher skips this
    port until the rate-limit window elapses."""
    connector = await _make_scripted_connector(
        {"COM3": _script_lines("ERROR")}
    )
    assert await probe_at_channel(connector, "COM3", timeout=0.5) is False


async def test_probe_returns_false_when_connector_raises() -> None:
    @contextlib.asynccontextmanager
    async def boom(_device: str, _baud: int):
        raise OSError("port busy")
        yield  # pragma: no cover

    assert await probe_at_channel(boom, "COM3", timeout=0.5) is False


async def test_probe_tolerates_command_echo() -> None:
    """Some modems echo the command before responding (ATE0 unset).

    The echo line ``AT`` MUST be dropped so it does not poison the
    payload, and ``OK`` immediately after still terminates the read."""

    connector = await _make_scripted_connector(
        {"COM3": _script_lines("AT", "OK")}
    )
    assert await probe_at_channel(connector, "COM3", timeout=0.5) is True


# ========================================================== read_modem_identity


async def test_read_modem_identity_happy_path() -> None:
    connector = await _make_scripted_connector(
        {
            "COM3": _script_full_identity(
                manufacturer="HUAWEI",
                model="E3372",
                imei="867234050123456",
                iccid="89860422000000000001",
            )
        }
    )
    ident = await read_modem_identity(connector, "COM3", command_timeout=0.5)
    assert ident == ModemIdentity(
        manufacturer="HUAWEI",
        model="E3372",
        imei="867234050123456",
        iccid="89860422000000000001",
    )


async def test_read_modem_identity_strips_prefix() -> None:
    """Some modems prepend ``+CGMI:`` / ``+CCID:`` to the response."""

    connector = await _make_scripted_connector(
        {
            "COM3": _script_lines(
                "+CGMI: Quectel",
                "OK",
                "+CGMM: EC25",
                "OK",
                "+CGSN: 861234560000007",
                "OK",
                "+CCID: 89860422000000000099",
                "OK",
            )
        }
    )
    ident = await read_modem_identity(connector, "COM3", command_timeout=0.5)
    assert ident.manufacturer == "Quectel"
    assert ident.model == "EC25"
    assert ident.imei == "861234560000007"
    assert ident.iccid == "89860422000000000099"


async def test_read_modem_identity_raises_on_first_stage_error() -> None:
    connector = await _make_scripted_connector(
        {"COM3": _script_lines("+CME ERROR: 100")}
    )
    with pytest.raises(ModemInitFailedError) as ei:
        await read_modem_identity(connector, "COM3", command_timeout=0.5)
    assert ei.value.stage == "CGMI"
    assert "+CME ERROR" in ei.value.detail


async def test_read_modem_identity_raises_on_intermediate_timeout() -> None:
    """CGMI returns OK, CGMM never responds → timeout at CGMM stage."""

    connector = await _make_scripted_connector(
        {
            "COM3": _script_lines(
                "HUAWEI",
                "OK",
                # nothing for CGMM
            )
        }
    )
    with pytest.raises(ModemInitFailedError) as ei:
        await read_modem_identity(connector, "COM3", command_timeout=0.05)
    assert ei.value.stage == "CGMM"
    assert ei.value.detail == "timeout"


async def test_read_modem_identity_falls_back_to_iccid_alias() -> None:
    """AT+CCID returns ERROR → retry AT+ICCID → success."""

    connector = await _make_scripted_connector(
        {
            "COM3": _script_lines(
                # CGMI / CGMM / CGSN OK
                "SIMCom",
                "OK",
                "A7670",
                "OK",
                "868000000000001",
                "OK",
                # CCID ERROR, then ICCID OK
                "ERROR",
                "89860422111111111111",
                "OK",
            )
        }
    )
    ident = await read_modem_identity(connector, "COM3", command_timeout=0.5)
    assert ident.iccid == "89860422111111111111"


async def test_read_modem_identity_fallback_failure_carries_both_details() -> None:
    """If both AT+CCID and AT+ICCID fail, the error detail names both."""

    connector = await _make_scripted_connector(
        {
            "COM3": _script_lines(
                "SIMCom",
                "OK",
                "A7670",
                "OK",
                "868000000000001",
                "OK",
                "ERROR",  # CCID
                "+CME ERROR: 100",  # ICCID
            )
        }
    )
    with pytest.raises(ModemInitFailedError) as ei:
        await read_modem_identity(connector, "COM3", command_timeout=0.5)
    assert ei.value.stage == "CCID"
    assert "AT+CCID=ERROR" in ei.value.detail
    assert "+CME ERROR" in ei.value.detail


async def test_read_modem_identity_ccid_timeout_does_not_retry() -> None:
    """A timeout (no terminator at all) is treated as a real
    communication failure — we do NOT retry as ICCID because the modem
    is unresponsive, not selectively rejecting the command."""

    connector = await _make_scripted_connector(
        {
            "COM3": _script_lines(
                "Quectel",
                "OK",
                "EC25",
                "OK",
                "861234560000007",
                "OK",
                # silent for CCID
            )
        }
    )
    with pytest.raises(ModemInitFailedError) as ei:
        await read_modem_identity(connector, "COM3", command_timeout=0.05)
    assert ei.value.stage == "CCID"
    assert ei.value.detail == "timeout"


# ====================================================== identify_modem_channel


_WHITELIST = {
    ("12d1", "1506"),  # Huawei
    ("2c7c", "0125"),  # Quectel
}


async def test_identify_short_circuits_on_whitelist_hit() -> None:
    """Known VID:PID → skip probe, go straight to identity read.

    Connector is invoked **once** — the identity sequence — not twice.
    """

    connector = _staged_connector(
        _script_full_identity(
            manufacturer="HUAWEI",
            model="E3372",
            imei="867234050123456",
            iccid="89860422000000000001",
        ),
    )
    prober = RateLimitedProber(DEFAULT_PROBE_INTERVAL_S, clock=_FakeClock(0.0))

    result = await identify_modem_channel(
        connector,
        "COM3",
        vendor_id="12d1",
        product_id="1506",
        whitelist=_WHITELIST,
        prober=prober,
        command_timeout=0.5,
    )

    assert result.is_at_channel is True
    assert result.identity is not None
    assert result.identity.model == "E3372"
    assert result.init_failure is None
    assert connector.state["calls"] == 1, "probe MUST be skipped for whitelist match"


async def test_identify_falls_back_to_probe_for_unknown_vid_pid() -> None:
    """VID:PID miss → AT probe (open #1) → identity read (open #2)."""

    connector = _staged_connector(
        _script_ok(),  # open 1: probe → OK
        _script_full_identity(
            manufacturer="ZTE",
            model="MF710",
            imei="350000000000001",
            iccid="89860422000000000050",
        ),  # open 2: identity
    )
    prober = RateLimitedProber(DEFAULT_PROBE_INTERVAL_S, clock=_FakeClock(0.0))

    result = await identify_modem_channel(
        connector,
        "COM7",
        vendor_id="19d2",
        product_id="0117",  # not in _WHITELIST
        whitelist=_WHITELIST,
        prober=prober,
        probe_timeout=0.5,
        command_timeout=0.5,
    )

    assert result.is_at_channel is True
    assert result.identity is not None
    assert result.identity.manufacturer == "ZTE"
    assert connector.state["calls"] == 2


async def test_identify_rate_limited_skips_probe() -> None:
    """Same device probed twice within the interval → second call returns
    ``rate_limited`` without opening the connector at all."""

    connector = _staged_connector(_script_silent())  # would fail → "probe_no_ok"
    clock = _FakeClock(0.0)
    prober = RateLimitedProber(60.0, clock=clock)
    prober.record("COM7")  # stamp as just probed

    result = await identify_modem_channel(
        connector,
        "COM7",
        vendor_id="dead",
        product_id="beef",
        whitelist=_WHITELIST,
        prober=prober,
        probe_timeout=0.05,
    )

    assert result.is_at_channel is False
    assert result.skipped_reason == "rate_limited"
    assert result.identity is None
    assert connector.state["calls"] == 0


async def test_identify_probe_no_ok_returns_skipped_reason() -> None:
    """Unknown VID:PID, probe times out → not a modem."""

    connector = _staged_connector(_script_silent())
    prober = RateLimitedProber(DEFAULT_PROBE_INTERVAL_S, clock=_FakeClock(0.0))

    result = await identify_modem_channel(
        connector,
        "COM9",
        vendor_id="dead",
        product_id="beef",
        whitelist=_WHITELIST,
        prober=prober,
        probe_timeout=0.05,
    )

    assert result.is_at_channel is False
    assert result.skipped_reason == "probe_no_ok"
    assert connector.state["calls"] == 1


async def test_identify_records_probe_attempt_even_when_no_ok() -> None:
    """``prober.record`` is called **before** the probe runs, so a port
    that times out is still subject to the rate limit on the next add
    event — otherwise the watcher would re-probe non-modem devices
    every poll tick."""

    connector = _staged_connector(_script_silent())
    clock = _FakeClock(0.0)
    prober = RateLimitedProber(60.0, clock=clock)

    await identify_modem_channel(
        connector,
        "COM9",
        vendor_id="dead",
        product_id="beef",
        whitelist=_WHITELIST,
        prober=prober,
        probe_timeout=0.05,
    )

    # Immediately after the failed probe, a second identify on the same
    # port (clock has not advanced) MUST be rate-limited.
    assert prober.allow("COM9") is False


async def test_identify_init_failure_is_folded_into_result() -> None:
    """Whitelist hit but CCID fails → ``is_at_channel=True``, ``init_failure``
    carries stage + detail so the caller can ``emit HardwareAlert``."""

    connector = _staged_connector(
        _script_lines(
            "HUAWEI",
            "OK",
            "E3372",
            "OK",
            "867234050123456",
            "OK",
            "+CME ERROR: 100",  # CCID
            "+CME ERROR: 100",  # ICCID fallback also fails
        ),
    )
    prober = RateLimitedProber(DEFAULT_PROBE_INTERVAL_S, clock=_FakeClock(0.0))

    result = await identify_modem_channel(
        connector,
        "COM3",
        vendor_id="12d1",
        product_id="1506",
        whitelist=_WHITELIST,
        prober=prober,
        command_timeout=0.5,
    )

    assert result.is_at_channel is True
    assert result.identity is None
    assert result.init_failure is not None
    assert result.init_failure.stage == "CCID"


async def test_identify_multi_port_modem_only_at_channel_yields_identity() -> None:
    """Simulate a 3-COM Huawei: one AT channel, two non-AT (PCM/debug).

    All three carry the modem's whitelisted VID:PID — the channel that
    answers the identity sequence is the AT channel; the others fail
    init and surface as ``init_failure`` for HardwareAlert.

    The watcher emits one ``add`` per COM port (per
    ``windows_serial.test_diff_multi_port_modem``); the identifier
    then has to pick the AT channel out of the bunch. This test
    verifies the contract that lets the caller do that picking by
    inspection of ``IdentifyResult.identity``.
    """

    async def probe_only_com4(port: str) -> Callable:
        # COM3 / COM5: identity sequence times out at CGMI.
        if port == "COM4":
            return _script_full_identity(
                manufacturer="HUAWEI",
                model="E3372",
                imei="867234050123456",
                iccid="89860422000000000001",
            )
        return _script_silent()

    @contextlib.asynccontextmanager
    async def per_port_connector(
        device_node: str, baudrate: int
    ) -> AsyncIterator[tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
        cli_r, cli_w, _modem_r, modem_w = await make_stream_pair()
        push = await probe_only_com4(device_node)
        await push(modem_w)
        try:
            yield cli_r, cli_w
        finally:
            cli_w.close()
            with contextlib.suppress(Exception):
                await cli_w.wait_closed()
            modem_w.close()
            with contextlib.suppress(Exception):
                await modem_w.wait_closed()

    prober = RateLimitedProber(DEFAULT_PROBE_INTERVAL_S, clock=_FakeClock(0.0))

    results: dict[str, IdentifyResult] = {}
    for port in ("COM3", "COM4", "COM5"):
        results[port] = await identify_modem_channel(
            per_port_connector,
            port,
            vendor_id="12d1",
            product_id="1506",  # whitelisted → skip probe, go to identity
            whitelist=_WHITELIST,
            prober=prober,
            command_timeout=0.05,
        )

    assert results["COM4"].identity is not None
    assert results["COM4"].identity.model == "E3372"

    # The non-AT channels: identity stage times out at CGMI.
    assert results["COM3"].identity is None
    assert results["COM3"].init_failure is not None
    assert results["COM3"].init_failure.stage == "CGMI"
    assert results["COM5"].identity is None
    assert results["COM5"].init_failure is not None
