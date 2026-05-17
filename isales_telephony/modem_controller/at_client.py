"""AT client abstraction.

``ATClient`` is the Protocol every concrete implementation honours:
- ``MockATClient`` returns canned values + simulates a short call (CI / dev)
- ``SerialATClient`` speaks AT over ``pyserial`` + ``ModemDriver`` to a real
  GSM modem (production; impl-real-at)

The dial flow returns ``(call_id, AsyncIterator[ATEvent])``: the call id is
available *immediately* (so the IPC layer can ack the dial command), and the
caller iterates the event stream to receive ``connected`` / ``remote_hangup``
asynchronously. Iteration ends when the call terminates.

Mock timing is configurable via env vars so stage-4 engine integration tests
can exercise different orderings without recompiling:

  MOCK_DIAL_DELAY_MS         (default 1000) — dial → connected delay
  MOCK_CALL_DURATION_MS      (default 5000) — connected → remote_hangup delay
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from isales_telephony.modem_controller.platforms import serial_lock
from isales_telephony.modem_controller.platforms.serial_lock import (
    BusyDeviceError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ATEvent:
    event: str  # "connected" | "remote_hangup"
    call_id: str
    cause: str | None = None


class PcmEnableError(RuntimeError):
    """Raised when ``AT+CPCMREG=1`` did not return ``OK``.

    SIMCom-class modems gate the audio COM port byte stream behind an
    AT-side switch (``AT+CPCMREG=1`` per the SIMCom application manual).
    The command only succeeds while a call is connected; outside of a
    call the modem responds ``ERROR`` because the audio DSP is not
    holding a PCM context.

    The orchestrator catches this in
    :meth:`EdgeOrchestrator._handle_connected` and converts to a
    ``HardwareAlert`` + remote hangup so the cloud retire-retry path
    treats the call as a hardware failure rather than a no-answer.
    """

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or "AT+CPCMREG=1 did not return OK")
        self.detail = detail


class ATClient(Protocol):
    async def dial(self, number: str) -> tuple[str, AsyncIterator[ATEvent]]:
        ...

    async def hangup(self, call_id: str) -> None:
        ...

    async def get_signal(self) -> int:
        ...

    async def get_iccid(self) -> str:
        ...

    async def get_imei(self) -> str:
        ...

    async def cpcmreg_enable(self) -> None:
        """Enable the SerialPcm byte stream on a SIMCom audio COM port.

        Wraps ``AT+CPCMREG=1``. SHALL raise :class:`PcmEnableError` if
        the modem returns ``ERROR`` / times out, so the orchestrator
        can convert to a HardwareAlert and tear down the call. Called
        per-call between ``connected`` and ``audio_bridge.join_rtc``.
        Drivers for modems with a different PCM enable protocol MAY
        override.
        """
        ...

    async def cpcmreg_disable(self) -> None:
        """Disable the SerialPcm byte stream.

        Wraps ``AT+CPCMREG=0``. Failure is logged but MUST NOT raise —
        teardown is the only caller and a failed CPCMREG=0 is recoverable
        on the next CPCMREG=1.
        """
        ...


def _ms(env: str, default: int) -> float:
    raw = os.environ.get(env)
    if raw is None:
        return default / 1000
    try:
        return int(raw) / 1000
    except ValueError:
        return default / 1000


class MockATClient:
    """Returns canned identity, simulates a short call.

    ``dial(number)`` returns immediately with a call_id and an event stream
    that yields ``connected`` after MOCK_DIAL_DELAY_MS and ``remote_hangup``
    MOCK_CALL_DURATION_MS later. ``hangup(call_id)`` cancels the in-flight
    stream and yields ``remote_hangup`` early (cause=manual_hangup).
    """

    def __init__(self) -> None:
        self._cancellers: dict[str, asyncio.Event] = {}

    async def dial(self, number: str) -> tuple[str, AsyncIterator[ATEvent]]:
        call_id = uuid.uuid4().hex[:12]
        canceller = asyncio.Event()
        self._cancellers[call_id] = canceller

        async def stream() -> AsyncIterator[ATEvent]:
            try:
                # dial → connected
                if not await _wait_or_cancelled(_ms("MOCK_DIAL_DELAY_MS", 1000), canceller):
                    yield ATEvent(event="remote_hangup", call_id=call_id, cause="manual_hangup")
                    return
                yield ATEvent(event="connected", call_id=call_id)
                # connected → remote_hangup
                if not await _wait_or_cancelled(_ms("MOCK_CALL_DURATION_MS", 5000), canceller):
                    yield ATEvent(event="remote_hangup", call_id=call_id, cause="manual_hangup")
                    return
                yield ATEvent(event="remote_hangup", call_id=call_id, cause="normal_clearing")
            finally:
                self._cancellers.pop(call_id, None)

        # log who we're dialing for traceability — number is still useful as a tag
        _ = number
        return call_id, stream()

    async def hangup(self, call_id: str) -> None:
        canceller = self._cancellers.get(call_id)
        if canceller is not None:
            canceller.set()

    async def get_signal(self) -> int:
        return 22  # 0..31 fake CSQ value

    async def get_iccid(self) -> str:
        return "8986" + "0" * 16

    async def get_imei(self) -> str:
        return "0" * 15

    async def cpcmreg_enable(self) -> None:
        # Mock has no audio plane; pretend the modem ACKed.
        return

    async def cpcmreg_disable(self) -> None:
        return


async def _wait_or_cancelled(seconds: float, canceller: asyncio.Event) -> bool:
    """Sleep ``seconds`` or until cancelled; return True if slept fully."""
    try:
        await asyncio.wait_for(canceller.wait(), timeout=seconds)
        return False
    except TimeoutError:
        return True


__all__ = [
    "ATClient",
    "ATEvent",
    "BusyDeviceError",
    "MockATClient",
    "PcmEnableError",
    "SerialATClient",
]


class SerialATClient:
    """Production ATClient bridging pyserial + ModemDriver + URC stream.

    Spec: device-hardware § "ATClient 实现策略" + § "URC → ATEvent 翻译契约".

    Wires up three layers:
      1. ``pyserial`` (or ``pyserial-asyncio``) opens the tty
      2. ``serial_protocol.AtClient`` handles AT command framing + URC fan-out
      3. ``drivers.ModemDriver`` (auto-detected vendor subclass) sends ATD /
         ATH / AT+CSQ / AT+CCID / AT+CGSN

    URC translation: this class registers an ``on_urc`` callback that
    classifies remote-hangup URCs (``NO CARRIER`` / ``BUSY`` / ``NO ANSWER``
    / ``NO DIALTONE``) and pushes ``ATEvent`` into the active call queue.

    Connected detection: A7670-class modems don't push a ``CONNECT`` URC for
    ATD voice calls — they only push ``NO CARRIER`` / ``BUSY`` / ``NO ANSWER``
    on failure. To detect successful connection, ``dial()`` starts an
    ``AT+CLCC`` poller (1 s cadence) that watches for state=0 (active).
    """

    # Maps URC types -> hangup_cause string in drivers.HANGUP_CAUSE_MAP terms.
    # Imported lazily inside __init__ to avoid module-import cycle with drivers.py.
    _HANGUP_URC_CAUSES: dict[str, str] = {}  # populated in __init_subclass__-like way below

    _CLCC_POLL_INTERVAL_S: float = 1.0
    _ATD_ACK_TIMEOUT_S: float = 60.0

    def __init__(self, at, driver, *, lock_fd: int | None = None) -> None:
        from isales_telephony.modem_controller.drivers import HANGUP_CAUSE_MAP
        from isales_telephony.modem_controller.serial_protocol import UrcType

        # Build per-instance map (keeps drivers/serial_protocol imports local
        # so unit tests can monkey-patch HANGUP_CAUSE_MAP if needed).
        self._hangup_urc_causes: dict[UrcType, str] = {
            UrcType.NO_CARRIER: HANGUP_CAUSE_MAP["NO CARRIER"],
            UrcType.BUSY: HANGUP_CAUSE_MAP["BUSY"],
            UrcType.NO_ANSWER: HANGUP_CAUSE_MAP["NO ANSWER"],
            UrcType.NO_DIALTONE: HANGUP_CAUSE_MAP["NO DIALTONE"],
        }
        self._at = at
        self._driver = driver
        self._lock_fd = lock_fd
        # Per-call state. Only one active call at a time (single modem).
        self._active_call_id: str | None = None
        self._call_queue: asyncio.Queue[ATEvent | None] | None = None
        self._poller_task: asyncio.Task[None] | None = None
        self._at.on_urc(self._on_urc)

    @classmethod
    async def create_from_tty(
        cls,
        tty_path: str,
        *,
        driver_hint: str = "",
        baudrate: int = 115200,
    ) -> SerialATClient:
        """Open ``tty_path``, take exclusive flock, detect driver, init modem.

        Raises:
            BusyDeviceError: another process holds the tty lock.
            RuntimeError: the tty cannot be opened (permission / not present).
        """
        import serial_asyncio  # type: ignore[import-untyped]

        from isales_telephony.modem_controller.drivers import detect_driver
        from isales_telephony.modem_controller.serial_protocol import AtClient

        try:
            reader, writer = await serial_asyncio.open_serial_connection(
                url=tty_path, baudrate=baudrate
            )
        except Exception as exc:  # noqa: BLE001 — classify before surfacing
            # Windows OS-level exclusive open conflict surfaces as
            # PermissionError / "Access is denied" SerialException; map
            # it to BusyDeviceError so callers see one error type across
            # platforms. POSIX open-time errors are deploy/permission
            # misconfigurations, not lock contention — keep as RuntimeError.
            busy = serial_lock.translate_open_error(exc, tty_path)
            if busy is not None:
                raise busy from exc
            raise RuntimeError(f"failed to open {tty_path}: {exc}") from exc

        # Pull the underlying serial fd off pyserial-asyncio's transport.
        # Layout: transport.serial is the pyserial Serial object; .fd is
        # the POSIX OS fd. Windows pyserial does not expose a POSIX fd
        # (no such concept) — fd=None there is expected and acquire_exclusive
        # short-circuits to a no-op (OS already enforces exclusive open).
        transport = writer.transport
        serial_obj = getattr(transport, "serial", None)
        fd: int | None = getattr(serial_obj, "fd", None) if serial_obj else None
        try:
            lock_handle = serial_lock.acquire_exclusive(fd, tty_path)
        except (BusyDeviceError, RuntimeError):
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            raise

        at = AtClient(reader, writer, default_timeout=5.0)
        at.start()
        try:
            driver = await detect_driver(at, hint=driver_hint)
            await driver.init()
        except Exception:
            await at.aclose()
            serial_lock.release(lock_handle)
            raise
        return cls(at, driver, lock_fd=lock_handle)

    async def dial(self, number: str) -> tuple[str, AsyncIterator[ATEvent]]:
        """Issue ATD; return call_id + URC-translated event stream.

        Per spec: ATD OK ACK does NOT trigger ``connected``. The event stream
        yields ``connected`` only after AT+CLCC polling detects an active
        call, or yields ``remote_hangup`` if the call fails before connect.
        """
        call_id = uuid.uuid4().hex[:12]
        if self._active_call_id is not None:
            # Single-modem invariant: refuse concurrent dial.
            raise RuntimeError(
                f"another call is active (call_id={self._active_call_id})"
            )
        self._active_call_id = call_id
        self._call_queue = asyncio.Queue()

        try:
            result = await self._driver.dial(number, timeout=self._ATD_ACK_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — translate to remote_hangup
            from isales_telephony.modem_controller.drivers import (
                _classify_dial_failure,
            )

            cause = _classify_dial_failure(str(exc))
            self._reset_call_state()
            return call_id, _single_event_stream(
                ATEvent("remote_hangup", call_id, cause=cause)
            )

        if not result.connected:
            cause = result.hangup_cause or "network_out_of_order"
            self._reset_call_state()
            return call_id, _single_event_stream(
                ATEvent("remote_hangup", call_id, cause=cause)
            )

        # ATD accepted; start AT+CLCC poller to detect "connected".
        self._poller_task = asyncio.create_task(
            self._poll_for_connected(call_id), name=f"at_clcc_poll_{call_id}"
        )
        return call_id, self._urc_stream(call_id)

    async def hangup(self, call_id: str) -> None:
        """Hang up the active call. Idempotent for non-active call_id."""
        if self._active_call_id != call_id:
            return  # not our active call (already hung up, or stale id)
        queue = self._call_queue
        try:
            await self._driver.hangup()
        except Exception:  # noqa: BLE001 — log + still emit manual_hangup
            logger.exception("driver.hangup failed for call_id=%s", call_id)
        if queue is not None:
            await queue.put(
                ATEvent("remote_hangup", call_id, cause="manual_hangup")
            )

    async def get_signal(self) -> int:
        return await self._driver.signal_strength()

    async def get_iccid(self) -> str:
        return await self._driver.iccid()

    async def get_imei(self) -> str:
        return await self._driver.imei()

    async def cpcmreg_enable(self) -> None:
        """``AT+CPCMREG=1`` — open the audio COM PCM byte stream.

        Raises :class:`PcmEnableError` on ERROR / timeout so the
        orchestrator can convert to ``HardwareAlert.ModemInitFailed``
        (stage=``"CPCMREG_ENABLE"``) and hang up the call.
        """
        from isales_telephony.modem_controller.serial_protocol import AtError

        try:
            await self._at.send("AT+CPCMREG=1", timeout=2.0)
        except AtError as exc:
            raise PcmEnableError(detail=str(exc)) from exc

    async def cpcmreg_disable(self) -> None:
        """``AT+CPCMREG=0`` — close the audio COM PCM byte stream.

        Best-effort: log on failure but do NOT raise. Teardown calls
        this after ``audio_bridge.leave`` so failing here just leaves
        the modem in a slightly stale audio-DSP state that the next
        ``cpcmreg_enable`` will reset.
        """
        from isales_telephony.modem_controller.serial_protocol import AtError

        try:
            await self._at.send("AT+CPCMREG=0", timeout=2.0)
        except AtError as exc:
            logger.warning("cpcmreg_disable failed: %s", exc)

    async def aclose(self) -> None:
        """Release the URC queue, close AtClient, release flock."""
        if self._call_queue is not None:
            with contextlib.suppress(Exception):
                self._call_queue.put_nowait(None)
        if self._poller_task is not None and not self._poller_task.done():
            self._poller_task.cancel()
            with contextlib.suppress(BaseException):
                await self._poller_task
        with contextlib.suppress(Exception):
            await self._at.aclose()
        if self._lock_fd is not None:
            serial_lock.release(self._lock_fd)
            self._lock_fd = None

    # ----- internals -----

    def _reset_call_state(self) -> None:
        self._active_call_id = None
        self._call_queue = None
        if self._poller_task is not None and not self._poller_task.done():
            self._poller_task.cancel()
        self._poller_task = None

    async def _on_urc(self, urc) -> None:
        """AtClient URC callback. Translate remote-hangup URCs to ATEvent."""
        call_id = self._active_call_id
        queue = self._call_queue
        if call_id is None or queue is None:
            return  # no active call; URC ignored (RING / +CLIP outside call)
        cause = self._hangup_urc_causes.get(urc.type)
        if cause is None:
            return  # not a hangup-class URC
        await queue.put(ATEvent("remote_hangup", call_id, cause=cause))

    async def _poll_for_connected(self, call_id: str) -> None:
        """Poll AT+CLCC; emit ``connected`` ATEvent on state=0 (active)."""
        from isales_telephony.modem_controller.serial_protocol import AtError

        try:
            while self._active_call_id == call_id:
                await asyncio.sleep(self._CLCC_POLL_INTERVAL_S)
                if self._active_call_id != call_id:
                    return
                try:
                    response = await self._at.send("AT+CLCC", timeout=2.0)
                except AtError:
                    continue
                if _clcc_has_active_call(response.lines):
                    queue = self._call_queue
                    if queue is not None:
                        await queue.put(ATEvent("connected", call_id))
                    return
        except asyncio.CancelledError:
            return

    async def _urc_stream(self, call_id: str) -> AsyncIterator[ATEvent]:
        """Drain the per-call queue; dedup connected; terminate on hangup."""
        queue = self._call_queue
        if queue is None:
            return
        try:
            connected_seen = False
            while True:
                ev = await queue.get()
                if ev is None:
                    return  # aclose() sentinel
                if ev.event == "connected":
                    if connected_seen:
                        continue
                    connected_seen = True
                yield ev
                if ev.event == "remote_hangup":
                    return
        finally:
            if self._active_call_id == call_id:
                self._reset_call_state()


async def _single_event_stream(event: ATEvent) -> AsyncIterator[ATEvent]:
    yield event


def _clcc_has_active_call(lines: list[str]) -> bool:
    """Parse AT+CLCC response; return True if any call has state=0 (active).

    Format: ``+CLCC: <idx>,<dir>,<stat>,<mode>,<mpty>[,<number>,<type>]``
    where ``stat`` = 0 means active (3GPP TS 27.007 §7.18).
    """
    for line in lines:
        if not line.startswith("+CLCC"):
            continue
        try:
            payload = line.split(":", 1)[1]
        except IndexError:
            continue
        parts = [p.strip() for p in payload.split(",")]
        if len(parts) >= 3 and parts[2] == "0":
            return True
    return False
