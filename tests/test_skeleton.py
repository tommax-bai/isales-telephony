"""Smoke tests — entry points import, /health responds, OpenAPI doc complete."""

from __future__ import annotations

from fastapi.testclient import TestClient

from isales_telephony.api.main import create_app


def test_health() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_modem_controller_importable() -> None:
    from isales_telephony.modem_controller.main import run

    assert callable(run)


def test_openapi_documents_all_routes() -> None:
    app = create_app()
    with TestClient(app) as client:
        spec = client.get("/openapi.json").json()
    paths = set(spec["paths"].keys())
    assert {
        "/health",
        "/devices",
        "/devices/{device_id}",
        "/sim-cards",
        "/sim-cards/{sim_id}",
        "/device-sim-bindings",
    } <= paths
    # Schemas of shared models surface
    schemas = spec["components"]["schemas"]
    assert "DeviceRead" in schemas
    assert "SimCardRead" in schemas
