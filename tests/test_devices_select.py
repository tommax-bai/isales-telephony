"""Integration tests for POST /devices/select — concurrency + NULL ordering."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from isales_common.enums import DeviceStatus
from isales_common.models import (
    Campaign,
    CampaignDevice,
    Device,
    DeviceSimBinding,
    SimCard,
)
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from isales_telephony.api.main import create_app
from isales_telephony.common.auth import current_user


async def _seed(
    engine: AsyncEngine,
    n: int,
    *,
    null_indexes: set[int] | None = None,
) -> int:
    """Seed n idle devices+SIMs bound to one campaign; return campaign_id.

    null_indexes — devices whose last_call_at SHOULD remain NULL. The rest get
    a non-null timestamp so they sort after the NULL rows under NULLS FIRST.
    """
    sm = async_sessionmaker(engine, expire_on_commit=False)
    null_indexes = null_indexes or set()
    async with sm() as session:
        campaign = Campaign(name="cmp1")
        session.add(campaign)
        await session.flush()

        from datetime import UTC, datetime

        non_null_ts = datetime.now(tz=UTC)
        for i in range(n):
            dev = Device(
                name=f"d{i}",
                imei=f"imei-{i}",
                status=DeviceStatus.IDLE,
                last_call_at=None if i in null_indexes else non_null_ts,
            )
            sim = SimCard(
                iccid=f"898600000000000{i:05d}",
                phone_number=f"+861380013800{i}",
            )
            session.add_all([dev, sim])
            await session.flush()
            session.add(
                DeviceSimBinding(
                    device_id=dev.id, sim_card_id=sim.id, is_active=True
                )
            )
            session.add(CampaignDevice(campaign_id=campaign.id, device_id=dev.id))
        await session.commit()
        return campaign.id


def _make_internal_client(engine: AsyncEngine) -> AsyncClient:
    """Build a client whose app uses the same engine but no auth override —
    /devices/select is on the internal router and doesn't need JWT anyway."""
    app = create_app()
    app.state.engine = engine
    app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    app.dependency_overrides[current_user] = lambda: {"sub": "test"}
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio(loop_scope="session")
class TestSelectDevice:
    async def test_concurrent_picks_distinct_devices(
        self, clean_engine: AsyncEngine
    ) -> None:
        # 10 idle devices, fire 11 concurrent /select calls. Each /select
        # transitions the picked device to status='dialing' (claim semantics),
        # so the 11th caller sees only 10 - 10 = 0 idle devices and gets 503.
        campaign_id = await _seed(clean_engine, n=10)
        async with _make_internal_client(clean_engine) as client:
            results = await asyncio.gather(
                *(
                    client.post(
                        "/devices/select", json={"campaign_id": campaign_id}
                    )
                    for _ in range(11)
                )
            )
        successes = [r for r in results if r.status_code == 200]
        failures = [r for r in results if r.status_code == 503]
        assert len(successes) == 10, [r.text for r in failures]
        assert len(failures) == 1
        assert failures[0].json()["detail"] == "no_idle_device"
        picked: list[int] = [r.json()["device_id"] for r in successes]
        assert len(set(picked)) == 10, f"duplicates picked: {picked}"

    async def test_select_flips_status_to_dialing(
        self, clean_engine: AsyncEngine
    ) -> None:
        campaign_id = await _seed(clean_engine, n=1)
        async with _make_internal_client(clean_engine) as client:
            resp = await client.post(
                "/devices/select", json={"campaign_id": campaign_id}
            )
        assert resp.status_code == 200
        sm = async_sessionmaker(clean_engine, expire_on_commit=False)
        async with sm() as session:
            dev = await session.get(Device, resp.json()["device_id"])
            assert dev is not None
            assert dev.status == DeviceStatus.DIALING

    async def test_select_emits_deprecation_header(
        self, clean_engine: AsyncEngine
    ) -> None:
        # arch-cloud-edge-split task 7.7: surface RFC 8594 § 3
        # `Deprecation` header on any successful /devices/select so
        # leftover callers in the cloud-edge topology show up in logs.
        campaign_id = await _seed(clean_engine, n=1)
        async with _make_internal_client(clean_engine) as client:
            resp = await client.post(
                "/devices/select", json={"campaign_id": campaign_id}
            )
        assert resp.status_code == 200
        assert resp.headers.get("deprecation") == "1"

    async def test_null_last_call_at_picked_first(
        self, clean_engine: AsyncEngine
    ) -> None:
        # 3 devices: indexes 0,1 have non-null last_call_at, index 2 is NULL.
        campaign_id = await _seed(clean_engine, n=3, null_indexes={2})
        async with _make_internal_client(clean_engine) as client:
            resp = await client.post(
                "/devices/select", json={"campaign_id": campaign_id}
            )
            assert resp.status_code == 200
            # device_id is auto-incrementing; index 2 → 3rd device → id 3
            picked = resp.json()["device_id"]
            sm = async_sessionmaker(clean_engine, expire_on_commit=False)
            async with sm() as session:
                devs: dict[int, Any] = {
                    d.id: d
                    for d in (await session.scalars(_select_all_devices())).all()
                }
            # The picked device MUST be the one whose last_call_at was NULL
            # before the call (it is now updated to now()). Identify by index 2
            # which we created last → highest id.
            assert picked == max(devs.keys())

    async def test_no_devices_returns_503(self, clean_engine: AsyncEngine) -> None:
        # Seed a campaign but zero devices.
        sm = async_sessionmaker(clean_engine, expire_on_commit=False)
        async with sm() as session:
            c = Campaign(name="empty")
            session.add(c)
            await session.commit()
            cid = c.id
        async with _make_internal_client(clean_engine) as client:
            resp = await client.post("/devices/select", json={"campaign_id": cid})
        assert resp.status_code == 503


def _select_all_devices() -> Any:
    from sqlalchemy import select as sa_select

    return sa_select(Device)
