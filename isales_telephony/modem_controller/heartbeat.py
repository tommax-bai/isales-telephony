"""modem-controller heartbeat coroutine.

Spec: device-hardware § modem-controller 心跳与失联探测 — once every 30s,
PATCH /devices/{id}/heartbeat for every registered device.

The heartbeat is a thin layer over httpx — kept here (not in api_client.py)
because the only consumer is the daemon entrypoint and the loop is
load-bearing for the failure-detection contract.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from typing import Protocol

logger = logging.getLogger(__name__)


HEARTBEAT_INTERVAL_SECONDS = 30


class HeartbeatPoster(Protocol):
    """Anything that can POST one heartbeat. Production = httpx; tests = mock."""

    async def heartbeat(self, device_id: int, signal_strength: int | None) -> None: ...


async def heartbeat_loop(
    poster: HeartbeatPoster,
    device_ids: Callable[[], Iterable[int]],
    *,
    signal_strength_provider: Callable[[int], int | None] | None = None,
    interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
    iterations: int | None = None,
) -> None:
    """Run the heartbeat loop forever (or for ``iterations`` if set, for tests).

    ``device_ids`` is a callable so the loop sees devices added / removed
    between ticks. ``signal_strength_provider`` returns the current AT+CSQ
    cached value per device id; ``None`` means "don't include".
    """

    completed = 0
    while iterations is None or completed < iterations:
        for device_id in list(device_ids()):
            sig = (
                signal_strength_provider(device_id)
                if signal_strength_provider is not None
                else None
            )
            try:
                await poster.heartbeat(device_id, sig)
            except Exception:  # noqa: BLE001 — never let one failure kill the loop
                logger.warning(
                    "heartbeat_failed", extra={"device_id": device_id}, exc_info=True
                )
        completed += 1
        if iterations is not None and completed >= iterations:
            return
        await asyncio.sleep(interval_seconds)


class HttpHeartbeatPoster:
    """httpx-backed poster — production wiring."""

    def __init__(self, base_url: str, *, auth_token: str = "") -> None:
        # Lazy import so unit tests don't need httpx in non-hardware envs.
        import httpx  # noqa: PLC0415

        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=10.0, headers=headers
        )

    async def heartbeat(self, device_id: int, signal_strength: int | None) -> None:
        body: dict[str, int] = {}
        if signal_strength is not None:
            body["signal_strength"] = signal_strength
        response = await self._client.patch(
            f"/devices/{device_id}/heartbeat", json=body
        )
        response.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()
