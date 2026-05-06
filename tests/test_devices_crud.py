"""Integration tests for /devices CRUD."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


async def _create(client: AsyncClient, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "name": "dev1",
        "usb_port": "/dev/ttyUSB0",
        "modem_model": "EC25",
        "imei": "111",
    }
    payload.update(overrides)
    resp = await client.post("/devices", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio(loop_scope="session")
class TestDeviceCRUD:
    async def test_create_then_get(self, client: AsyncClient) -> None:
        created = await _create(client)
        device_id = created["id"]
        resp = await client.get(f"/devices/{device_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "dev1"
        assert body["status"] == "unknown"

    async def test_list_pagination_and_status_filter(self, client: AsyncClient) -> None:
        for i in range(3):
            await _create(client, name=f"dev{i}", imei=f"imei-{i}")
        resp = await client.get("/devices", params={"page_size": 2, "page": 1})
        body = resp.json()
        assert body["total"] == 3
        assert len(body["items"]) == 2

        await client.patch(
            f"/devices/{body['items'][0]['id']}", json={"status": "idle"}
        )
        resp = await client.get("/devices", params={"status": "idle"})
        idle = resp.json()
        assert idle["total"] == 1
        assert idle["items"][0]["status"] == "idle"

    async def test_get_404(self, client: AsyncClient) -> None:
        resp = await client.get("/devices/9999")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "device_not_found"

    async def test_patch_404(self, client: AsyncClient) -> None:
        resp = await client.patch("/devices/9999", json={"name": "x"})
        assert resp.status_code == 404

    async def test_create_validation_422(self, client: AsyncClient) -> None:
        resp = await client.post("/devices", json={"name": ""})
        assert resp.status_code == 422

    async def test_delete_then_404(self, client: AsyncClient) -> None:
        created = await _create(client)
        resp = await client.delete(f"/devices/{created['id']}")
        assert resp.status_code == 204
        assert (await client.get(f"/devices/{created['id']}")).status_code == 404
