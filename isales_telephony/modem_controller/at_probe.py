"""AT-channel probe + modem identity reader.

Spec: ``device-hardware`` § Windows USB GSM modem 设备识别 (D1
``windows-client-core`` tasks 3.3 / 3.4 / 3.5):

- Multi-COM-port modems (Huawei E398, Quectel multi-mode etc.) expose an
  AT channel, a PCM channel and a debug channel under separate ``COMn``
  numbers. Only the AT channel answers ``AT\\r\\n`` with ``OK`` — that
  is the channel we want.
- A USB VID:PID match against the modem-vendor whitelist short-circuits
  the probe (saves a serial open per add event for known-good devices).
- VID:PID miss → AT probe falls back, but **at most once per minute per
  device_node** so that genuinely non-modem serial devices (Bluetooth
  virtual ports, FTDI debug adapters, …) are not pounded on every
  poll-tick of the watcher.
- Once a port is identified as an AT channel, ``read_modem_identity``
  runs the canonical init sequence (``CGMI`` / ``CGMM`` / ``CGSN`` /
  ``CCID``). Any failure raises :class:`ModemInitFailedError`, which the
  caller is expected to convert into a ``HardwareAlert.ModemInitFailed``
  proto and dispatch over the cloud-edge gRPC stream.

This module deliberately keeps the cloud-edge transport out of scope —
it depends only on stdlib + ``pyserial-asyncio`` for the default
connector — so unit tests can drive it from a plain ``asyncio`` socket
pair without spinning up gRPC, audio, or DB.

Platform note: ``pyserial-asyncio.open_serial_connection`` accepts
``COM3`` on Windows, ``/dev/cu.usbmodemNNN`` on macOS and
``/dev/ttyUSBn`` on Linux — pyserial handles the path translation. The
probe is therefore platform-neutral, but the D1 driver is Windows: on
Linux/macOS the existing ``SerialATClient.create_from_tty`` flow (which
takes an ``fcntl`` flock) remains canonical and ``at_probe`` is unused.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

# Public defaults -------------------------------------------------------------

DEFAULT_BAUDRATE = 115200
"""Standard 3GPP modem AT baudrate; every modem in the whitelist defaults here."""

DEFAULT_PROBE_TIMEOUT_S = 0.2
"""Per-probe overall timeout. Spec: ``200 ms 内收到 OK 视为命中``."""

DEFAULT_COMMAND_TIMEOUT_S = 2.0
"""Per-command timeout for CGMI/CGMM/CGSN/CCID. Modems answer in <100 ms when
healthy; 2 s gives headroom for slow SIM init without hanging the orchestrator."""

DEFAULT_PROBE_INTERVAL_S = 60.0
"""Rate-limit window. Spec: ``每个 COM 端口每分钟至多试探一次``."""


SerialConnector = Callable[
    [str, int],
    "contextlib.AbstractAsyncContextManager[tuple[asyncio.StreamReader, asyncio.StreamWriter]]",
]
"""Opens ``device_node`` at ``baudrate`` and yields a stream pair.

The context manager MUST close the underlying serial port on exit so a
probe round-trip does not leak file descriptors when there are many
non-modem COM ports on the host.
"""


# Errors ----------------------------------------------------------------------


class ModemInitFailedError(Exception):
    """Raised when the device-identity sequence (CGMI/CGMM/CGSN/CCID) fails.

    Carries ``stage`` (which AT command stage) and ``detail`` (modem
    response or timeout summary) so the caller can map this directly to
    a ``HardwareAlert.ModemInitFailed`` proto.
    """

    def __init__(self, stage: str, detail: str) -> None:
        super().__init__(f"{stage}: {detail}" if detail else stage)
        self.stage = stage
        self.detail = detail


# Result types ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModemIdentity:
    """Canonical modem-identity tuple read from the AT channel.

    Stored verbatim from modem replies (no normalization beyond stripping
    surrounding whitespace) — the cloud worker is responsible for any
    further parsing (e.g. ICCID Luhn check).
    """

    manufacturer: str  # AT+CGMI
    model: str  # AT+CGMM
    imei: str  # AT+CGSN
    iccid: str  # AT+CCID (or AT+ICCID on some vendors — try both)


@dataclass(frozen=True, slots=True)
class IdentifyResult:
    """Outcome of :func:`identify_modem_channel`.

    Exactly one of (``identity``, ``init_failure``, ``skipped_reason``)
    is populated when ``is_at_channel`` is False or identity read was
    not attempted. The caller branches on these fields:

    - ``is_at_channel=True, identity=X`` → register as modem candidate.
    - ``is_at_channel=True, init_failure=E`` → emit HardwareAlert.
    - ``is_at_channel=False, skipped_reason="rate_limited"`` → wait.
    - ``is_at_channel=False, skipped_reason="probe_no_ok"`` → not a modem.
    - ``is_at_channel=False, skipped_reason="probe_io_error"`` → device
      mid-disconnect or busy; transient.
    """

    is_at_channel: bool
    identity: ModemIdentity | None = None
    init_failure: ModemInitFailedError | None = None
    skipped_reason: str | None = None


# Rate limiter ----------------------------------------------------------------


class RateLimitedProber:
    """Per-device monotonic-clock rate limiter for AT probes.

    Single-process, in-memory state. Restart of the modem-controller
    clears history — which is the right behaviour: a stale ``COMn`` that
    has been re-issued to a different device after a process restart
    should be probed fresh.

    Thread-unsafe by design; the modem-controller is single-process
    single-event-loop.
    """

    def __init__(
        self,
        interval_seconds: float = DEFAULT_PROBE_INTERVAL_S,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds < 0:
            raise ValueError("interval_seconds must be non-negative")
        self._interval = interval_seconds
        self._clock = clock
        self._last_probed: dict[str, float] = {}

    def allow(self, device_node: str) -> bool:
        """Return True iff a probe is allowed (interval has elapsed since
        the last recorded probe for this ``device_node``).

        ``interval_seconds=0`` disables limiting (always True) — useful
        in tests.
        """

        last = self._last_probed.get(device_node)
        if last is None:
            return True
        return (self._clock() - last) >= self._interval

    def record(self, device_node: str) -> None:
        """Stamp ``device_node`` as just-probed.

        Callers MUST invoke this immediately before starting the probe
        (not after) — otherwise a slow probe that overlaps the next
        watcher tick would not be rate-limited.
        """

        self._last_probed[device_node] = self._clock()

    def forget(self, device_node: str) -> None:
        """Drop history for ``device_node`` (e.g. after a USB ``remove``)."""

        self._last_probed.pop(device_node, None)


# Default connector (pyserial-asyncio) ----------------------------------------


@asynccontextmanager
async def _pyserial_connector(
    device_node: str, baudrate: int
) -> AsyncIterator[tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
    """Production connector — opens ``device_node`` over pyserial-asyncio.

    Lazy-imports pyserial-asyncio so the module is importable on hosts
    that have not installed the Windows extras (CI test selection runs
    every test on Linux / macOS using the socket-pair connector).
    """

    import serial_asyncio  # noqa: PLC0415 — see docstring

    reader, writer = await serial_asyncio.open_serial_connection(
        url=device_node, baudrate=baudrate
    )
    try:
        yield reader, writer
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


# Low-level command exchange --------------------------------------------------


async def _send_and_collect(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    command: str,
    *,
    timeout: float,
) -> tuple[bool, list[str], str | None]:
    """Issue ``command`` and read lines until ``OK`` / ``ERROR`` / timeout.

    Returns ``(ok, payload_lines, error_detail)``:

    - ``ok=True``  → terminator was ``OK``; ``payload_lines`` holds the
      echoed lines in between (excluding the terminator and excluding
      the command echo, if any).
    - ``ok=False`` and ``error_detail`` set → terminator was ``ERROR`` /
      ``+CME ERROR: …`` / ``+CMS ERROR: …``.
    - ``ok=False`` and ``error_detail=None`` → overall timeout elapsed
      before any terminator arrived.

    Modem echoing is filtered conservatively: if the first line of the
    response matches the command we sent (``ATE0`` is not always set on
    a fresh open), it is dropped.
    """

    writer.write((command + "\r\n").encode("ascii"))
    await writer.drain()

    deadline = asyncio.get_running_loop().time() + timeout
    payload: list[str] = []
    command_stripped = command.strip()

    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return False, payload, None
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=remaining)
        except (asyncio.TimeoutError, TimeoutError):
            return False, payload, None
        if not raw:
            # EOF — peer closed mid-response. Treat as timeout (no
            # terminator) so the caller logs as a transient failure
            # rather than a protocol-level ERROR.
            return False, payload, None
        line = raw.decode("ascii", errors="replace").strip("\r\n").strip()
        if not line:
            continue
        # Drop echo of our own command if the modem echoes commands back
        # (no ATE0 yet). Only the exact command-line match is dropped —
        # do not drop content that merely starts with the command.
        if line == command_stripped and not payload:
            continue
        if line == "OK":
            return True, payload, None
        if line == "ERROR":
            return False, payload, "ERROR"
        if line.startswith("+CME ERROR") or line.startswith("+CMS ERROR"):
            return False, payload, line
        payload.append(line)


# Public functions ------------------------------------------------------------


async def probe_at_channel(
    connector: SerialConnector,
    device_node: str,
    *,
    baudrate: int = DEFAULT_BAUDRATE,
    timeout: float = DEFAULT_PROBE_TIMEOUT_S,
) -> bool:
    """Open ``device_node``, send ``AT``, return True iff ``OK`` arrives
    within ``timeout``.

    Any OSError / connector exception (port busy, device disappeared
    mid-open, permission denied) is converted to ``False`` — the caller
    will retry on the next watcher tick anyway, and a noisy log on every
    transient pyserial error during USB churn would drown real signals.
    """

    try:
        async with connector(device_node, baudrate) as (reader, writer):
            ok, _payload, _err = await _send_and_collect(
                reader, writer, "AT", timeout=timeout
            )
            return ok
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — see docstring
        return False


async def read_modem_identity(
    connector: SerialConnector,
    device_node: str,
    *,
    baudrate: int = DEFAULT_BAUDRATE,
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT_S,
) -> ModemIdentity:
    """Run the canonical identity sequence on ``device_node``.

    Sequence (per device-hardware spec § 设备初始化序列):

    1. ``AT+CGMI`` — manufacturer
    2. ``AT+CGMM`` — model
    3. ``AT+CGSN`` — IMEI
    4. ``AT+CCID`` — ICCID (with ``AT+ICCID`` retry for vendors that
       only honour the non-standard form, notably some Quectel /
       SIMCom builds)

    On the first failing stage, raises :class:`ModemInitFailedError` so
    the caller can ``emit HardwareAlert.ModemInitFailed`` and skip the
    rest of the sequence (no point probing IMEI when CGMI already
    failed — the SIM is the same either way).
    """

    async with connector(device_node, baudrate) as (reader, writer):
        manufacturer = await _run_identity_command(
            reader, writer, "AT+CGMI", stage="CGMI", timeout=command_timeout
        )
        model = await _run_identity_command(
            reader, writer, "AT+CGMM", stage="CGMM", timeout=command_timeout
        )
        imei = await _run_identity_command(
            reader, writer, "AT+CGSN", stage="CGSN", timeout=command_timeout
        )
        # ICCID: try AT+CCID first, fall back to AT+ICCID once on ERROR
        # (Quectel EC25 / SIMCom A7670 historically split here).
        try:
            iccid = await _run_identity_command(
                reader, writer, "AT+CCID", stage="CCID", timeout=command_timeout
            )
        except ModemInitFailedError as primary_exc:
            if primary_exc.detail.startswith("timeout"):
                # A real timeout, not a "command unsupported" → propagate.
                raise
            try:
                iccid = await _run_identity_command(
                    reader,
                    writer,
                    "AT+ICCID",
                    stage="CCID",
                    timeout=command_timeout,
                )
            except ModemInitFailedError as fallback_exc:
                # Surface the original failure detail — operators care
                # about the canonical-command response, not the alias.
                raise ModemInitFailedError(
                    stage="CCID",
                    detail=f"AT+CCID={primary_exc.detail}; AT+ICCID={fallback_exc.detail}",
                ) from fallback_exc

    return ModemIdentity(
        manufacturer=manufacturer,
        model=model,
        imei=imei,
        iccid=iccid,
    )


async def _run_identity_command(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    command: str,
    *,
    stage: str,
    timeout: float,
) -> str:
    """Send one identity-stage command; raise :class:`ModemInitFailedError`
    on any non-OK outcome.

    Returns the **first non-blank** payload line — modems echo the actual
    identity on a single line (e.g. ``"HUAWEI"`` for CGMI). Multi-line
    responses are joined with ``" "`` for forward compatibility (some
    modems return ``"+CCID: <iccid>"`` plus a blank line).
    """

    try:
        ok, payload, err = await _send_and_collect(
            reader, writer, command, timeout=timeout
        )
    except (OSError, asyncio.IncompleteReadError) as exc:
        raise ModemInitFailedError(stage=stage, detail=f"io_error: {exc}") from exc

    if not ok:
        if err is None:
            raise ModemInitFailedError(stage=stage, detail="timeout")
        raise ModemInitFailedError(stage=stage, detail=err)

    cleaned = [line.strip() for line in payload if line.strip()]
    if not cleaned:
        raise ModemInitFailedError(stage=stage, detail="empty_response")
    # Strip leading "+CGMI:" / "+CCID:" prefixes that some modems prepend.
    value = " ".join(cleaned)
    prefix = f"+{stage}:"
    if value.startswith(prefix):
        value = value[len(prefix):].strip()
    return value


# Top-level coordinator -------------------------------------------------------


async def identify_modem_channel(
    connector: SerialConnector,
    device_node: str,
    vendor_id: str | None,
    product_id: str | None,
    *,
    whitelist: set[tuple[str, str]],
    prober: RateLimitedProber,
    baudrate: int = DEFAULT_BAUDRATE,
    probe_timeout: float = DEFAULT_PROBE_TIMEOUT_S,
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT_S,
) -> IdentifyResult:
    """End-to-end: decide whether ``device_node`` is an AT channel and,
    if so, read its identity.

    Algorithm:

    1. **Whitelist match** — if ``(vendor_id, product_id)`` ∈
       ``whitelist`` (both lowercased), assume AT channel and go
       straight to identity read. Skips the 200 ms probe per known-good
       device. Failure of identity read still yields an
       ``IdentifyResult(is_at_channel=True, init_failure=...)`` so the
       caller can emit a HardwareAlert.

    2. **Rate-limited AT probe** — for VID:PID misses, ask ``prober``
       whether we may probe now. If not, return
       ``IdentifyResult(is_at_channel=False, skipped_reason="rate_limited")``.
       Otherwise stamp the prober and send ``AT``. ``OK`` ⇒ identity
       read; no ``OK`` ⇒ ``skipped_reason="probe_no_ok"``.

    Errors during identity read are **caught** and folded into
    ``init_failure`` — the caller's job is to translate that into a
    HardwareAlert. We do not raise out of this function so the
    surrounding watcher loop is not polluted with try/except per port.
    """

    vid = vendor_id.lower() if vendor_id else None
    pid = product_id.lower() if product_id else None
    in_whitelist = (
        vid is not None and pid is not None and (vid, pid) in whitelist
    )

    if not in_whitelist:
        if not prober.allow(device_node):
            return IdentifyResult(is_at_channel=False, skipped_reason="rate_limited")
        prober.record(device_node)
        try:
            is_at = await probe_at_channel(
                connector, device_node, baudrate=baudrate, timeout=probe_timeout
            )
        except asyncio.CancelledError:
            raise
        if not is_at:
            return IdentifyResult(is_at_channel=False, skipped_reason="probe_no_ok")

    # We are committed to the identity read.
    try:
        identity = await read_modem_identity(
            connector,
            device_node,
            baudrate=baudrate,
            command_timeout=command_timeout,
        )
    except ModemInitFailedError as exc:
        return IdentifyResult(is_at_channel=True, init_failure=exc)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — fold into init_failure
        return IdentifyResult(
            is_at_channel=True,
            init_failure=ModemInitFailedError(
                stage="open", detail=f"io_error: {exc}"
            ),
        )

    return IdentifyResult(is_at_channel=True, identity=identity)


__all__ = [
    "DEFAULT_BAUDRATE",
    "DEFAULT_COMMAND_TIMEOUT_S",
    "DEFAULT_PROBE_INTERVAL_S",
    "DEFAULT_PROBE_TIMEOUT_S",
    "IdentifyResult",
    "ModemIdentity",
    "ModemInitFailedError",
    "RateLimitedProber",
    "SerialConnector",
    "identify_modem_channel",
    "probe_at_channel",
    "read_modem_identity",
]
