"""Integration tests for PATCH /devices/{id}/heartbeat.

Spec: device-hardware § modem-controller 心跳与失联探测.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio(loop_scope="session")
class TestDeviceHeartbeat:
    async def test_heartbeat_refreshes_last_seen_at(self, client: AsyncClient) -> None:
        created = await client.post(
            "/devices",
            json={"name": "h1", "usb_port": "/dev/ttyUSB0", "imei": "imei-h1"},
        )
        assert created.status_code == 201, created.text
        device_id = created.json()["id"]

        before = await client.get(f"/devices/{device_id}")
        assert before.json()["last_seen_at"] is None

        resp = await client.patch(
            f"/devices/{device_id}/heartbeat", json={"signal_strength": 22}
        )
        assert resp.status_code == 204, resp.text

        after = await client.get(f"/devices/{device_id}")
        body = after.json()
        assert body["last_seen_at"] is not None

    async def test_heartbeat_accepts_payload_without_signal_strength(
        self, client: AsyncClient
    ) -> None:
        created = await client.post(
            "/devices",
            json={"name": "h2", "usb_port": "/dev/ttyUSB1", "imei": "imei-h2"},
        )
        device_id = created.json()["id"]
        resp = await client.patch(f"/devices/{device_id}/heartbeat", json={})
        assert resp.status_code == 204, resp.text

    async def test_heartbeat_does_not_modify_status(self, client: AsyncClient) -> None:
        created = await client.post(
            "/devices",
            json={"name": "h3", "usb_port": "/dev/ttyUSB2", "imei": "imei-h3"},
        )
        device_id = created.json()["id"]
        # Bring it into 'idle' so we have something to assert against.
        await client.patch(f"/devices/{device_id}", json={"status": "idle"})
        await client.patch(
            f"/devices/{device_id}/heartbeat", json={"signal_strength": 18}
        )
        resp = await client.get(f"/devices/{device_id}")
        body = resp.json()
        assert body["status"] == "idle"

    async def test_heartbeat_returns_404_for_missing_device(
        self, client: AsyncClient
    ) -> None:
        resp = await client.patch(
            "/devices/999999/heartbeat", json={"signal_strength": 10}
        )
        assert resp.status_code == 404

    async def test_heartbeat_rejects_out_of_range_signal_strength(
        self, client: AsyncClient
    ) -> None:
        created = await client.post(
            "/devices",
            json={"name": "h4", "usb_port": "/dev/ttyUSB3", "imei": "imei-h4"},
        )
        device_id = created.json()["id"]
        resp = await client.patch(
            f"/devices/{device_id}/heartbeat", json={"signal_strength": 100}
        )
        assert resp.status_code == 422
