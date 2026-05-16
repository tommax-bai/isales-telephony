"""ActivationController tests — drive the validate / write / restart
flow without instantiating a Qt QDialog.

The pieces that interact with PySide6 (``run_dialog``) are exercised
on the Windows runner during D1 PoC week 1; here we cover the pure
controller logic which is what main_windows.py loops over.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from isales_telephony.ui.activation import ActivationController
from isales_telephony.ui.state import CloudLinkState, StateBus


class _FakeGrpcRestarter:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._raises = raises

    async def restart(self, *, endpoint: str, token: str) -> None:
        self.calls.append((endpoint, token))
        if self._raises is not None:
            raise self._raises


@pytest.mark.asyncio(loop_scope="session")
async def test_happy_path_writes_env_and_restarts_grpc(tmp_path: Path) -> None:
    bus = StateBus()
    restarter = _FakeGrpcRestarter()
    controller = ActivationController(
        state_bus=bus,
        grpc_restarter=restarter,
        env_path_resolver=lambda: tmp_path / "telephony.env",
    )

    attempt = await controller.process_submission(
        token="abcdef1234567890",
        endpoint="isales.example.com:443",
    )

    assert attempt.error is None
    assert restarter.calls == [("isales.example.com:443", "abcdef1234567890")]
    assert (tmp_path / "telephony.env").exists()
    snap = bus.status
    # Optimistic state: writer flipped to DISCONNECTED; the real gRPC
    # client will move it to CONNECTED once the stream is up.
    assert snap.cloud_link is CloudLinkState.DISCONNECTED
    assert snap.last_activation_error is None


@pytest.mark.asyncio(loop_scope="session")
async def test_validation_failure_doesnt_call_grpc(tmp_path: Path) -> None:
    bus = StateBus()
    restarter = _FakeGrpcRestarter()
    controller = ActivationController(
        state_bus=bus,
        grpc_restarter=restarter,
        env_path_resolver=lambda: tmp_path / "telephony.env",
    )

    attempt = await controller.process_submission(
        token="short",
        endpoint="isales.example.com:443",
    )

    assert attempt.error is not None
    assert "too short" in attempt.error
    assert restarter.calls == []
    # No env file written.
    assert not (tmp_path / "telephony.env").exists()
    assert bus.status.last_activation_error == attempt.error


@pytest.mark.asyncio(loop_scope="session")
async def test_grpc_rejection_flips_to_auth_rejected(tmp_path: Path) -> None:
    bus = StateBus()
    restarter = _FakeGrpcRestarter(raises=RuntimeError("UNAUTHENTICATED"))
    controller = ActivationController(
        state_bus=bus,
        grpc_restarter=restarter,
        env_path_resolver=lambda: tmp_path / "telephony.env",
    )

    attempt = await controller.process_submission(
        token="abcdef1234567890",
        endpoint="isales.example.com:443",
    )

    assert attempt.error is not None
    assert "UNAUTHENTICATED" in attempt.error
    snap = bus.status
    assert snap.cloud_link is CloudLinkState.AUTH_REJECTED
    assert "UNAUTHENTICATED" in (snap.last_activation_error or "")


@pytest.mark.asyncio(loop_scope="session")
async def test_endpoint_defaults_when_empty(tmp_path: Path) -> None:
    bus = StateBus()
    restarter = _FakeGrpcRestarter()
    controller = ActivationController(
        state_bus=bus,
        grpc_restarter=restarter,
        env_path_resolver=lambda: tmp_path / "telephony.env",
        default_endpoint="default.example.com:443",
    )

    await controller.process_submission(
        token="abcdef1234567890",
        endpoint="",
    )

    assert restarter.calls == [("default.example.com:443", "abcdef1234567890")]


@pytest.mark.asyncio(loop_scope="session")
async def test_clears_previous_error_on_success(tmp_path: Path) -> None:
    bus = StateBus()
    bus.update(last_activation_error="先前的错误")
    restarter = _FakeGrpcRestarter()
    controller = ActivationController(
        state_bus=bus,
        grpc_restarter=restarter,
        env_path_resolver=lambda: tmp_path / "telephony.env",
    )

    await controller.process_submission(
        token="abcdef1234567890",
        endpoint="isales.example.com:443",
    )
    assert bus.status.last_activation_error is None
