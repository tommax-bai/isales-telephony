"""IPC command handlers.

Built via :func:`build_handlers` so dial/hangup can close over an ``ATClient``
and the optional database ``sessionmaker``. Without a ``sessionmaker``,
handlers still emit events to the IPC peer but do not write device.status —
this is the mode unit tests use.

Device state machine (per device-hardware spec):
  idle → dialing  (when /devices/select picked it; or on dial cmd if not pre-claimed)
  dialing → in_call   (on `connected` event from AT layer)
  in_call → idle      (on `remote_hangup`)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from isales_common.enums import DeviceStatus
from isales_common.models import Device
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from isales_telephony.modem_controller.at_client import ATClient, MockATClient
from isales_telephony.modem_controller.ipc_server import Connection, Handler

logger = logging.getLogger(__name__)


async def _set_device_status(
    sm: async_sessionmaker[AsyncSession] | None,
    device_id: int | None,
    status: DeviceStatus,
) -> None:
    if sm is None or device_id is None:
        return
    async with sm() as session:
        dev = await session.get(Device, device_id)
        if dev is None:
            logger.warning("dial handler: device %s not found", device_id)
            return
        dev.status = status
        await session.commit()


def build_handlers(
    *,
    at_client: ATClient | None = None,
    sm: async_sessionmaker[AsyncSession] | None = None,
) -> Mapping[str, Handler]:
    """Build the handler dict. Dial/hangup close over the AT client and DB."""
    client = at_client or MockATClient()

    async def handle_status(_conn: Connection, _msg: dict[str, Any]) -> dict[str, Any]:
        return {"event": "status", "status": "ok"}

    async def handle_dial(conn: Connection, msg: dict[str, Any]) -> dict[str, Any]:
        device_id = msg.get("device_id") if isinstance(msg.get("device_id"), int) else None
        number = msg.get("number")
        if not isinstance(number, str):
            return {"error": "invalid_request", "detail": "number required"}

        await _set_device_status(sm, device_id, DeviceStatus.DIALING)
        call_id, events = await client.dial(number)

        async def _pump() -> None:
            try:
                async for ev in events:
                    if ev.event == "connected":
                        await _set_device_status(sm, device_id, DeviceStatus.IN_CALL)
                    elif ev.event == "remote_hangup":
                        await _set_device_status(sm, device_id, DeviceStatus.IDLE)
                    payload: dict[str, Any] = {"event": ev.event, "call_id": ev.call_id}
                    if ev.cause:
                        payload["cause"] = ev.cause
                    if device_id is not None:
                        payload["device_id"] = device_id
                    await conn.send(payload)
            except Exception:
                logger.exception("dial handler event pump failed (call_id=%s)", call_id)

        asyncio.create_task(_pump())
        return {"event": "dial_ack", "call_id": call_id}

    async def handle_hangup(_conn: Connection, msg: dict[str, Any]) -> dict[str, Any]:
        call_id = msg.get("call_id")
        if isinstance(call_id, str):
            await client.hangup(call_id)
        return {"event": "hangup_ack"}

    async def handle_audio_downstream(
        _conn: Connection, msg: dict[str, Any]
    ) -> None:
        bytes_field = msg.get("bytes_b64", "")
        n = len(bytes_field) if isinstance(bytes_field, str) else 0
        logger.debug("audio_downstream %d b64-chars", n)
        return None

    return {
        "status": handle_status,
        "dial": handle_dial,
        "hangup": handle_hangup,
        "audio_downstream": handle_audio_downstream,
    }


# Convenience for callers that don't need an AT client / DB hook (e.g. PR #6
# tests). Equivalent to ``build_handlers()`` with all defaults.
DEFAULT_HANDLERS = build_handlers()
