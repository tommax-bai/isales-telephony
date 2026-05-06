"""AT client abstraction.

``ATClient`` is the Protocol every concrete implementation honours:
- ``MockATClient`` (stage 2) returns canned values + simulates a short call
- ``SerialATClient`` (stage 6) speaks AT over ``pyserial`` to a real GSM modem

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
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ATEvent:
    event: str  # "connected" | "remote_hangup"
    call_id: str
    cause: str | None = None


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
    stream and yields ``remote_hangup`` early (cause=local_clearing).
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
                    yield ATEvent(event="remote_hangup", call_id=call_id, cause="local_clearing")
                    return
                yield ATEvent(event="connected", call_id=call_id)
                # connected → remote_hangup
                if not await _wait_or_cancelled(_ms("MOCK_CALL_DURATION_MS", 5000), canceller):
                    yield ATEvent(event="remote_hangup", call_id=call_id, cause="local_clearing")
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


async def _wait_or_cancelled(seconds: float, canceller: asyncio.Event) -> bool:
    """Sleep ``seconds`` or until cancelled; return True if slept fully."""
    try:
        await asyncio.wait_for(canceller.wait(), timeout=seconds)
        return False
    except TimeoutError:
        return True
