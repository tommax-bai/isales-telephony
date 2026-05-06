"""APScheduler-driven device health monitor (stage 2 stub).

In v1 stage 2 there are no ``call_record`` rows yet (engine ships in stage 4),
so the real "近 N 通接通率" metric can't be computed. This module wires the
scheduler + scan loop with a stubbed ``compute_health`` that always returns
True; PR in stage 4 will replace the stub with the actual aggregation query.

The scheduler is constructed by callers (FastAPI lifespan or tests) so the
lifecycle is explicit and the scan can be triggered manually under pytest.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from isales_common.enums import DeviceStatus
from isales_common.models import Device
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


HealthFn = Callable[[int], Awaitable[bool]]


async def compute_health(_device_id: int) -> bool:
    """Stage-2 stub. TODO(stage-4): query call_record for last-N-call answer rate."""
    return True


async def scan_devices(
    sm: async_sessionmaker[AsyncSession], *, health_fn: HealthFn = compute_health
) -> None:
    """Scan every device, flag the unhealthy ones."""
    async with sm() as session:
        device_ids = list(
            (await session.execute(select(Device.id))).scalars().all()
        )

    for device_id in device_ids:
        try:
            healthy = await health_fn(device_id)
        except Exception:
            logger.exception(
                "health-monitor: compute_health failed",
                extra={"device_id": device_id},
            )
            continue
        if healthy:
            continue
        async with sm() as session:
            dev = await session.get(Device, device_id)
            if dev is None:
                continue
            if dev.status == DeviceStatus.FLAGGED:
                continue
            dev.status = DeviceStatus.FLAGGED
            await session.commit()
            logger.warning("health-monitor: flagged device %s", device_id)


def build_scheduler(
    sm: async_sessionmaker[AsyncSession], *, interval_minutes: int = 5
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scan_devices,
        "interval",
        minutes=interval_minutes,
        kwargs={"sm": sm},
        id="device_health_scan",
        max_instances=1,
        coalesce=True,
    )
    return scheduler
