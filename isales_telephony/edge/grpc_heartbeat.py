"""Edge-side gRPC heartbeat loop — reports device status to the cloud.

Spec: arch-cloud-edge-split § device-hardware "modem-controller 心跳与失联探测".

Sends ``Edge2Cloud.Heartbeat`` with a ``DeviceHealth`` entry every
``interval_seconds`` (default 30 s). The cloud-side ``heartbeat_handler``
syncs ``device.status`` and ``device.last_seen_at`` in PG so the
scheduler's ``pick_idle_device()`` sees the correct state.

The FIRST heartbeat is sent immediately on connect — this ensures that
after an edge restart (crash recovery from a stuck ``dialing`` state),
the cloud resets the device to ``idle`` without waiting a full interval.

Usage (wired into the edge daemon's task group)::

    tg.create_task(
        grpc_heartbeat_loop(
            client=grpc_client,
            device_id=device_id,
            status_provider=lambda: pb.DEVICE_STATUS_IDLE,
        ),
        name="grpc_heartbeat",
    )
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from google.protobuf.timestamp_pb2 import Timestamp  # type: ignore[import-untyped]
from isales_common.proto import cloud_edge_pb2 as pb
from isales_common.transport.cloud_edge import CloudEdgeClient, EdgeNotConnected

logger = logging.getLogger(__name__)

#: Default heartbeat interval matches proto spec ("every 30s by the edge").
HEARTBEAT_INTERVAL_S = 30

#: Type alias for the callable that returns the current proto DeviceStatus.
DeviceStatusProvider = Callable[[], int]


def _now_ts() -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(datetime.now(tz=UTC))
    return ts


def _build_heartbeat(
    device_id: int,
    status: int,
    signal_strength: int = -1,
) -> pb.Edge2Cloud:
    """Construct an Edge2Cloud heartbeat frame."""
    return pb.Edge2Cloud(
        heartbeat=pb.Heartbeat(
            ts=_now_ts(),
            devices=[
                pb.DeviceHealth(
                    device_id=device_id,
                    status=status,
                    signal_strength=signal_strength,
                ),
            ],
        )
    )


async def grpc_heartbeat_loop(
    *,
    client: CloudEdgeClient,
    device_id: int,
    status_provider: DeviceStatusProvider,
    signal_strength_provider: Callable[[], int] | None = None,
    interval_seconds: float = HEARTBEAT_INTERVAL_S,
    iterations: int | None = None,
) -> None:
    """Send periodic gRPC heartbeats until cancelled.

    Args:
        client: the edge's gRPC client (must already be started).
        device_id: PG ``device.id`` for this edge's modem.
        status_provider: callable returning the current proto
            ``DeviceStatus`` enum value (e.g. ``DEVICE_STATUS_IDLE``).
        signal_strength_provider: optional callable returning AT+CSQ
            value (0-31); ``None`` → -1 (unknown).
        interval_seconds: seconds between heartbeats (default 30).
        iterations: if set, exit after N heartbeats (for tests).
    """
    completed = 0
    while iterations is None or completed < iterations:
        status = status_provider()
        sig = signal_strength_provider() if signal_strength_provider else -1
        msg = _build_heartbeat(device_id, status, sig)
        try:
            await client.send(msg, critical=False)
            logger.debug(
                "grpc_heartbeat_sent",
                extra={"device_id": device_id, "status": status},
            )
        except EdgeNotConnected:
            # Stream is down; the reconnect loop will re-establish it.
            # Don't log at WARNING — heartbeat drops are expected during
            # brief disconnects and the connect-loop already logs them.
            logger.debug(
                "grpc_heartbeat_skipped_disconnected",
                extra={"device_id": device_id},
            )
        except Exception:  # noqa: BLE001 — never let heartbeat crash the process
            logger.warning(
                "grpc_heartbeat_send_failed",
                extra={"device_id": device_id},
                exc_info=True,
            )
        completed += 1
        if iterations is not None and completed >= iterations:
            return
        await asyncio.sleep(interval_seconds)


__all__ = ["DeviceStatusProvider", "grpc_heartbeat_loop"]
