"""Integration tests for /device-sim-bindings (read-only listing)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from isales_common.models import DeviceSimBinding
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker


async def _seed_binding(
    engine: AsyncEngine,
    *,
    device_id: int,
    sim_card_id: int,
    active: bool = True,
) -> int:
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        row = DeviceSimBinding(
            device_id=device_id,
            sim_card_id=sim_card_id,
            is_active=active,
            unbind_at=None if active else datetime.now(tz=UTC),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


async def _create_device(client: AsyncClient, name: str) -> int:
    return int(
        (
            await client.post(
                "/devices",
                json={"name": name, "imei": f"imei-{name}"},
            )
        ).json()["id"]
    )


async def _create_sim(client: AsyncClient, iccid: str) -> int:
    return int(
        (await client.post("/sim-cards", json={"iccid": iccid})).json()["id"]
    )


@pytest.mark.asyncio(loop_scope="session")
class TestDeviceSimBindings:
    async def test_list_filters(
        self, client: AsyncClient, clean_engine: AsyncEngine
    ) -> None:
        d1 = await _create_device(client, "d1")
        d2 = await _create_device(client, "d2")
        s1 = await _create_sim(client, "89860000000000000001")
        s2 = await _create_sim(client, "89860000000000000002")

        await _seed_binding(clean_engine, device_id=d1, sim_card_id=s1)
        await _seed_binding(
            clean_engine, device_id=d2, sim_card_id=s2, active=False
        )

        body = (await client.get("/device-sim-bindings")).json()
        assert len(body) == 2

        body_d1 = (
            await client.get("/device-sim-bindings", params={"device_id": d1})
        ).json()
        assert len(body_d1) == 1
        assert body_d1[0]["device_id"] == d1

        active = (
            await client.get(
                "/device-sim-bindings", params={"active_only": True}
            )
        ).json()
        assert len(active) == 1
        assert active[0]["is_active"] is True
