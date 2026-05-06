"""Smoke tests for PR #2 skeleton — entry points import, /health responds."""

from __future__ import annotations

from fastapi.testclient import TestClient

from isales_telephony.api.main import app


def test_health() -> None:
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_modem_controller_importable() -> None:
    from isales_telephony.modem_controller.main import run

    assert callable(run)
