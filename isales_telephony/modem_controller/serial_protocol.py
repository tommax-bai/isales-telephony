"""Asynchronous AT command client for GSM modems.

Spec: device-hardware § AT 命令通道 — controllers send ATD<n>; / ATH /
AT+CSQ / AT+CCID / AT+CGSN / AT+CGREG and parse URCs (RING / NO CARRIER /
BUSY / +CLIP).

Design (design.md § Decisions §3 + §6): a single reader coroutine splits
the line stream into two queues — command responses (lines starting with
OK / ERROR / +CME / +CMS) and unsolicited result codes (everything else
matching the URC patterns). Commands are dispatched serially through an
asyncio.Queue; URCs fan out to registered async callbacks.

This module has no real-serial code at the boundary so it can be unit
tested with pty pairs (see tests/at_client/).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator

import structlog

logger = structlog.get_logger(__name__)


class AtError(Exception):
    """Base for AT command failures."""


class AtTimeoutError(AtError):
    """Modem failed to acknowledge within the deadline."""


class AtCommandError(AtError):
    """Modem returned ERROR / +CME ERROR / +CMS ERROR."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(slots=True)
class AtResponse:
    """A completed command-response exchange."""

    ok: bool
    lines: list[str] = field(default_factory=list)
    """Echoed lines between command and OK/ERROR (excluding both terminals)."""
    error: AtCommandError | None = None


class UrcType(str, Enum):
    RING = "RING"
    NO_CARRIER = "NO CARRIER"
    BUSY = "BUSY"
    NO_DIALTONE = "NO DIALTONE"
    NO_ANSWER = "NO ANSWER"
    CLIP = "+CLIP"
    CREG = "+CREG"
    CMTI = "+CMTI"  # SMS notification — v1 ignores
    OTHER = "OTHER"


@dataclass(slots=True)
class Urc:
    type: UrcType
    raw: str
    """Original line, including the leading +TAG: when applicable."""


UrcHandler = Callable[[Urc], Awaitable[None]]


# Lines starting with these prefixes terminate a command exchange.
_TERMINAL_OK = "OK"
_TERMINAL_ERROR = "ERROR"
_CME_ERROR = "+CME ERROR"
_CMS_ERROR = "+CMS ERROR"

# Recognised URC line prefixes.
_URC_PATTERNS: dict[str, UrcType] = {
    "RING": UrcType.RING,
    "NO CARRIER": UrcType.NO_CARRIER,
    "BUSY": UrcType.BUSY,
    "NO DIALTONE": UrcType.NO_DIALTONE,
    "NO ANSWER": UrcType.NO_ANSWER,
    "+CLIP": UrcType.CLIP,
    "+CREG": UrcType.CREG,
    "+CMTI": UrcType.CMTI,
}


class AtClient:
    """Serial async AT command client with URC fan-out.

    Usage:

        async with AtClient.open_streams(reader, writer) as client:
            client.on_urc(my_handler)
            response = await client.send("AT+CSQ")
            print(response.lines)
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        default_timeout: float = 5.0,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._default_timeout = default_timeout
        self._command_lock = asyncio.Lock()
        self._response_queue: asyncio.Queue[AtResponse] = asyncio.Queue()
        # Lines collected for the currently-in-flight command.
        self._pending_lines: list[str] = []
        self._urc_handlers: list[UrcHandler] = []
        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False

    @classmethod
    @asynccontextmanager
    async def open_streams(
        cls,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        default_timeout: float = 5.0,
    ) -> AsyncIterator[AtClient]:
        client = cls(reader, writer, default_timeout=default_timeout)
        client.start()
        try:
            yield client
        finally:
            await client.aclose()

    def start(self) -> None:
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(
                self._read_loop(), name="at_client_reader"
            )

    def on_urc(self, handler: UrcHandler) -> None:
        """Register a coroutine that runs whenever a URC arrives."""

        self._urc_handlers.append(handler)

    async def send(self, command: str, *, timeout: float | None = None) -> AtResponse:
        """Send a single AT command and await OK / ERROR."""

        if self._closed:
            raise AtError("client_closed")
        deadline = timeout if timeout is not None else self._default_timeout
        async with self._command_lock:
            # Don't drain the queue here: when sends are issued back-to-back
            # the modem may have already fed the next OK before the lock is
            # re-acquired (especially in tests with pre-seeded responses),
            # and the lock plus the FIFO ensures 1:1 command/response
            # ordering on its own.
            self._pending_lines = []
            line = command if command.endswith("\r") else command + "\r"
            self._writer.write(line.encode("ascii"))
            await self._writer.drain()
            logger.debug("at_send", command=command)
            try:
                response = await asyncio.wait_for(
                    self._response_queue.get(), timeout=deadline
                )
            except TimeoutError as exc:
                raise AtTimeoutError(f"timed out waiting for response to {command!r}") from exc
            if response.error is not None:
                raise response.error
            return response

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:  # noqa: BLE001 — close is best-effort
            pass

    async def _read_loop(self) -> None:
        try:
            while True:
                raw = await self._reader.readline()
                if not raw:
                    # EOF — surface to any waiting command, then exit.
                    if self._command_lock.locked():
                        await self._response_queue.put(
                            AtResponse(
                                ok=False,
                                error=AtCommandError("EOF", "modem closed connection"),
                            )
                        )
                    return
                line = raw.decode("ascii", errors="replace").strip("\r\n ")
                if not line:
                    continue
                logger.debug("at_recv", line=line)
                self._dispatch_line(line)
        except asyncio.CancelledError:
            return

    def _dispatch_line(self, line: str) -> None:
        """Classify the line as command echo, terminal, or URC."""

        if line == _TERMINAL_OK:
            self._response_queue.put_nowait(
                AtResponse(ok=True, lines=list(self._pending_lines))
            )
            self._pending_lines = []
            return

        if line == _TERMINAL_ERROR:
            self._response_queue.put_nowait(
                AtResponse(
                    ok=False,
                    lines=list(self._pending_lines),
                    error=AtCommandError("ERROR"),
                )
            )
            self._pending_lines = []
            return

        for prefix in (_CME_ERROR, _CMS_ERROR):
            if line.startswith(prefix):
                detail = line[len(prefix) :].lstrip(": ")
                self._response_queue.put_nowait(
                    AtResponse(
                        ok=False,
                        lines=list(self._pending_lines),
                        error=AtCommandError(prefix, detail),
                    )
                )
                self._pending_lines = []
                return

        urc_type = _classify_urc(line)
        if urc_type is not None:
            urc = Urc(type=urc_type, raw=line)
            for handler in self._urc_handlers:
                asyncio.create_task(self._safe_dispatch(handler, urc))
            return

        # Otherwise, treat as part of an in-flight command's response body.
        self._pending_lines.append(line)

    @staticmethod
    async def _safe_dispatch(handler: UrcHandler, urc: Urc) -> None:
        try:
            await handler(urc)
        except Exception:  # noqa: BLE001 — never let one handler crash others
            logger.exception("urc_handler_failed", urc=urc.raw)


def _classify_urc(line: str) -> UrcType | None:
    """Return the URC type if ``line`` matches a known pattern, else None.

    Exposed at module scope so tests can drive line classification without
    spinning up an AtClient."""

    for prefix, urc_type in _URC_PATTERNS.items():
        if line == prefix or line.startswith(prefix + ":") or line.startswith(prefix + " "):
            return urc_type
    return None
