"""Test the health-monitor scan with a mocked compute_health."""

from __future__ import annotations

import pytest
from isales_common.enums import DeviceStatus
from isales_common.models import Device
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from isales_telephony.api.health_monitor import scan_devices


@pytest.mark.asyncio(loop_scope="session")
async def test_scan_flags_unhealthy(clean_engine: AsyncEngine) -> None:
    sm = async_sessionmaker(clean_engine, expire_on_commit=False)
    async with sm() as session:
        d1 = Device(name="d1", imei="i1", status=DeviceStatus.IDLE)
        d2 = Device(name="d2", imei="i2", status=DeviceStatus.IDLE)
        session.add_all([d1, d2])
        await session.commit()
        d1_id, d2_id = d1.id, d2.id

    # Mock: d1 healthy, d2 unhealthy.
    async def fake_health(device_id: int) -> bool:
        return device_id != d2_id

    await scan_devices(sm, health_fn=fake_health)

    async with sm() as session:
        rows = (
            await session.execute(select(Device).order_by(Device.id))
        ).scalars().all()
    by_id = {d.id: d.status for d in rows}
    assert by_id[d1_id] == DeviceStatus.IDLE
    assert by_id[d2_id] == DeviceStatus.FLAGGED


@pytest.mark.asyncio(loop_scope="session")
async def test_scan_idempotent_for_already_flagged(clean_engine: AsyncEngine) -> None:
    sm = async_sessionmaker(clean_engine, expire_on_commit=False)
    async with sm() as session:
        d = Device(name="d", imei="i", status=DeviceStatus.FLAGGED)
        session.add(d)
        await session.commit()

    async def always_unhealthy(_device_id: int) -> bool:
        return False

    # Should be a no-op (early-return), not raise.
    await scan_devices(sm, health_fn=always_unhealthy)
