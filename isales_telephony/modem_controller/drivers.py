"""ModemDriver per-vendor specialisation layer.

Spec: device-hardware § AT 命令通道 + § GSM hangup_cause 映射 — driver subs
encapsulate model-specific quirks while exposing a uniform dial / hangup /
signal-query / iccid / imei interface.

Design (design.md § Decisions §1): three concrete drivers (A7670 default,
SIM800C, Quectel UC20). Auto-detection via AT+GMI + AT+GMM at start; if
nothing is returned the env hint ``ISALES_MODEM_DRIVER`` selects the
fallback.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import structlog

from isales_telephony.modem_controller.serial_protocol import AtClient

logger = structlog.get_logger(__name__)


# Spec § GSM hangup_cause 映射 — keyed off the AT response phrase /
# +CEER cause code that the modem produces. Values match the
# call-state-machine spec hangup_cause enum.
HANGUP_CAUSE_MAP: dict[str, str] = {
    "NO CARRIER": "user_hangup",
    "BUSY": "callee_busy",
    "NO ANSWER": "callee_no_answer",
    "NO DIALTONE": "device_error",
    # +CEER cause numbers (3GPP TS 24.008 §10.5.4.11 subset):
    "1": "callee_no_route",  # Unallocated number
    "16": "user_hangup",  # Normal call clearing
    "17": "callee_busy",
    "18": "callee_no_answer",  # No user responding
    "19": "callee_no_answer",  # User alerting, no answer
    "27": "device_error",  # Destination out of order
    "31": "device_error",  # Normal, unspecified
    "34": "carrier_congestion",  # No circuit/channel available
    "38": "device_error",  # Network out of order
    "41": "carrier_congestion",  # Temporary failure
}


@dataclass(slots=True)
class DialResult:
    """Outcome of a dial attempt."""

    connected: bool
    hangup_cause: str | None = None
    """Set when ``connected=False``; one of call-state-machine.hangup_cause values."""


@dataclass(slots=True)
class ModemInfo:
    manufacturer: str
    model: str
    imei: str


class ModemDriver(ABC):
    """Abstract per-vendor driver."""

    name: str = "abstract"

    def __init__(self, at: AtClient) -> None:
        self._at = at

    @abstractmethod
    async def init(self) -> None:
        """Run vendor-specific initialisation (echo off, format, etc.)."""

    @abstractmethod
    async def dial(self, phone: str, *, timeout: float = 60.0) -> DialResult:
        """Place a voice call. Returns when the line connects, hangs up, or times out."""

    @abstractmethod
    async def hangup(self) -> None:
        """Terminate the active call (ATH or +CHUP)."""

    async def signal_strength(self) -> int:
        """0-31 RSSI value (AT+CSQ); 99 means unknown."""

        response = await self._at.send("AT+CSQ")
        return _parse_csq(response.lines)

    async def iccid(self) -> str:
        response = await self._at.send("AT+CCID")
        return _strip_prefix(response.lines, "+CCID")

    async def imei(self) -> str:
        response = await self._at.send("AT+CGSN")
        # Some modems echo +CGSN: "...", others return the IMEI on a bare line.
        if response.lines and response.lines[0].startswith("+CGSN"):
            return _strip_prefix(response.lines, "+CGSN")
        return response.lines[0] if response.lines else ""

    async def info(self) -> ModemInfo:
        manuf = await self._at.send("AT+GMI")
        model = await self._at.send("AT+GMM")
        return ModemInfo(
            manufacturer=manuf.lines[0] if manuf.lines else "",
            model=model.lines[0] if model.lines else "",
            imei=await self.imei(),
        )


class A7670Driver(ModemDriver):
    """Simcom A7670 — v1 default driver."""

    name = "a7670"

    async def init(self) -> None:
        await self._at.send("ATE0")        # echo off
        await self._at.send("AT+CMEE=1")   # numeric +CME ERROR
        await self._at.send("AT+CLIP=1")   # caller line identification

    async def dial(self, phone: str, *, timeout: float = 60.0) -> DialResult:
        try:
            response = await self._at.send(f"ATD{phone};", timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — convert to spec-mapped result
            cause = _classify_dial_failure(str(exc))
            return DialResult(connected=False, hangup_cause=cause)
        # OK from ATD<n>; just means the dial command was accepted; the
        # actual outcome is signalled via URCs (NO CARRIER / BUSY) handled
        # by the IPC server's URC subscriber. The spec scenario "拨号期间
        # 状态" lets us report 'connected' optimistically here; the IPC
        # layer flips to disconnected on URC arrival.
        return DialResult(connected=response.ok)

    async def hangup(self) -> None:
        await self._at.send("ATH")


class SIM800CDriver(A7670Driver):
    """SIM800C — same A-style commands but quirkier; v1 stub reuses A7670."""

    name = "sim800c"


class QuectelUC20Driver(A7670Driver):
    """Quectel UC20 — overlaps A7670 ATE/CMEE/CLIP semantics; v1 stub."""

    name = "quectel_uc20"


_DRIVERS: dict[str, type[ModemDriver]] = {
    A7670Driver.name: A7670Driver,
    SIM800CDriver.name: SIM800CDriver,
    QuectelUC20Driver.name: QuectelUC20Driver,
}


def get_driver(name: str, at: AtClient) -> ModemDriver:
    cls = _DRIVERS.get(name.lower(), A7670Driver)
    return cls(at)


async def detect_driver(at: AtClient, *, hint: str = "") -> ModemDriver:
    """Identify the modem family via AT+GMI/AT+GMM, falling back to ``hint``."""

    if hint:
        return get_driver(hint, at)
    try:
        manuf = await at.send("AT+GMI")
        model = await at.send("AT+GMM")
    except Exception:  # noqa: BLE001 — early-boot failures fall back to default
        logger.warning("modem_detect_failed; using a7670")
        return A7670Driver(at)
    blob = " ".join(manuf.lines + model.lines).lower()
    if "simcom" in blob and "a7670" in blob:
        return A7670Driver(at)
    if "simcom" in blob and "800" in blob:
        return SIM800CDriver(at)
    if "quectel" in blob:
        return QuectelUC20Driver(at)
    return A7670Driver(at)


def _parse_csq(lines: list[str]) -> int:
    for line in lines:
        if not line.startswith("+CSQ"):
            continue
        # Format: "+CSQ: <rssi>,<ber>"
        try:
            payload = line.split(":", 1)[1]
            rssi = int(payload.split(",", 1)[0].strip())
            return rssi
        except (IndexError, ValueError):
            continue
    return 99


def _strip_prefix(lines: list[str], prefix: str) -> str:
    for line in lines:
        if line.startswith(prefix):
            return line.split(":", 1)[-1].strip().strip('"')
        # Some firmware skips the prefix and emits the value on a bare line.
        return line.strip()
    return ""


def _classify_dial_failure(message: str) -> str:
    """Map an AT exception's message to a hangup_cause."""

    upper = message.upper()
    for token, cause in HANGUP_CAUSE_MAP.items():
        if token in upper:
            return cause
    return "device_error"
