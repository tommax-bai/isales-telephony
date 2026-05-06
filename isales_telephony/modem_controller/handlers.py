"""Built-in IPC command handlers.

PR #6 ships placeholder dial/hangup/audio_downstream handlers (logging only).
PR #7 swaps dial / hangup for the MockATClient-driven implementation that
emits ``connected`` / ``remote_hangup`` events asynchronously.
"""

from __future__ import annotations

import logging
from typing import Any

from isales_telephony.modem_controller.ipc_server import Connection

logger = logging.getLogger(__name__)


async def handle_status(_conn: Connection, _msg: dict[str, Any]) -> dict[str, Any]:
    return {"event": "status", "status": "ok"}


async def handle_dial(_conn: Connection, msg: dict[str, Any]) -> dict[str, Any]:
    logger.info("dial(stub) received: %s", msg)
    # PR #7 will replace with MockATClient.dial + connected/remote_hangup events.
    return {"event": "dial_ack", "call_id": "stub-call"}


async def handle_hangup(_conn: Connection, msg: dict[str, Any]) -> dict[str, Any]:
    logger.info("hangup(stub) received: %s", msg)
    return {"event": "hangup_ack"}


async def handle_audio_downstream(_conn: Connection, msg: dict[str, Any]) -> None:
    # Audio frames are fire-and-forget — no immediate reply.
    bytes_field = msg.get("bytes_b64", "")
    n = len(bytes_field) if isinstance(bytes_field, str) else 0
    logger.debug("audio_downstream %d b64-chars", n)
    return None


DEFAULT_HANDLERS = {
    "status": handle_status,
    "dial": handle_dial,
    "hangup": handle_hangup,
    "audio_downstream": handle_audio_downstream,
}
