"""Integration tests for /sim-cards CRUD."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


async def _create(client: AsyncClient, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "iccid": "89860000000000000001",
        "phone_number": "+8613800138001",
        "carrier": "CMCC",
    }
    payload.update(overrides)
    resp = await client.post("/sim-cards", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio(loop_scope="session")
class TestSimCardCRUD:
    async def test_create_get_patch_delete(self, client: AsyncClient) -> None:
        created = await _create(client)
        sid = created["id"]
        assert (await client.get(f"/sim-cards/{sid}")).status_code == 200
        resp = await client.patch(f"/sim-cards/{sid}", json={"plan": "100GB"})
        assert resp.status_code == 200
        assert resp.json()["plan"] == "100GB"
        assert (await client.delete(f"/sim-cards/{sid}")).status_code == 204
        assert (await client.get(f"/sim-cards/{sid}")).status_code == 404

    async def test_list_filter_by_carrier(self, client: AsyncClient) -> None:
        await _create(client, iccid="89860000000000000010", carrier="CMCC")
        await _create(client, iccid="89860000000000000011", carrier="CUCC")
        body = (await client.get("/sim-cards", params={"carrier": "CMCC"})).json()
        assert body["total"] == 1
        assert body["items"][0]["carrier"] == "CMCC"

    async def test_validation_422(self, client: AsyncClient) -> None:
        resp = await client.post("/sim-cards", json={"iccid": ""})
        assert resp.status_code == 422
