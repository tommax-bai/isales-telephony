"""Activation-code input dialog (PySide6).

Spec: deployment-topology § Scenario "激活码注册流程".

Flow when the env file is missing or lacks ``ISALES_EDGE_DEVICE_TOKEN``:

1. ``main_windows.py`` constructs an :class:`ActivationController`
   pointing at the gRPC client + state bus.
2. ``ActivationController.run_dialog()`` shows the modal dialog.
3. User pastes a token + (optionally) overrides the cloud endpoint.
4. On submit, the controller validates inputs (via
   :mod:`isales_telephony.ui.env_writer`), writes the env file, and
   awaits a fresh ``CloudEdgeGrpcClient.start()`` with the new token.
5. Success → dialog closes, state bus flips to ``CONNECTED``, tray
   turns green.
6. Failure (gRPC rejects token or network error) → dialog re-opens
   with the error message; ``last_activation_error`` is set on the
   state bus so the diagnostic window can mirror it.

The dialog itself is intentionally minimal — two ``QLineEdit`` fields,
an ``OK`` button, a status label. Visual polish (logo / Help link) is
D3 territory; D1 must not block the MVP on cosmetics.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from isales_telephony.ui.env_writer import (
    EnvFileError,
    default_env_path,
    validate_endpoint,
    validate_token,
    write_token_and_endpoint,
)
from isales_telephony.ui.state import CloudLinkState, StateBus

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Spec § "激活码注册流程" — cloud endpoint pre-fill. For PoC this is the
# fixed Aliyun-region gRPC LB; production deploys override via env file
# before the dialog ever shows.
DEFAULT_CLOUD_ENDPOINT = "isales.example.com:443"


class GrpcRestarter(Protocol):
    """Hooks the activation flow into the cloud-edge gRPC client.

    ``main_windows.py`` provides an implementation that:
    1. ``await client.stop()`` on the existing connection (no-op if not started)
    2. ``await client.start(endpoint=..., token=...)`` with the new creds

    Returns ``None`` on success; raises any exception on failure (the
    controller catches and surfaces the error to the user).
    """

    async def restart(self, *, endpoint: str, token: str) -> None: ...


@dataclass(slots=True)
class ActivationAttempt:
    """One pass through the activation flow — useful for tests."""

    token: str
    endpoint: str
    error: str | None = None  # None on success


class ActivationController:
    """Drives the activation dialog: validate, write env, restart gRPC.

    Pure orchestration — the PySide6 widget itself is built lazily in
    :meth:`run_dialog` so unit tests can poke :meth:`process_submission`
    without a Qt event loop.
    """

    def __init__(
        self,
        *,
        state_bus: StateBus,
        grpc_restarter: GrpcRestarter,
        env_path_resolver: Callable[[], "Path"] = default_env_path,
        loop: asyncio.AbstractEventLoop | None = None,
        default_endpoint: str = DEFAULT_CLOUD_ENDPOINT,
    ) -> None:
        self._bus = state_bus
        self._grpc_restarter = grpc_restarter
        self._resolve_env_path = env_path_resolver
        self._loop = loop
        self._default_endpoint = default_endpoint

    async def process_submission(
        self,
        *,
        token: str,
        endpoint: str,
    ) -> ActivationAttempt:
        """Validate → write env → restart gRPC. Returns an attempt
        record describing the outcome.

        On success: state bus flips to ``DISCONNECTED`` (the gRPC client
        will then transition to ``CONNECTED`` once the stream is up;
        the controller does not assume the handshake is instant).

        On failure: state bus's ``last_activation_error`` is populated.
        """
        token = token.strip()
        endpoint = endpoint.strip() or self._default_endpoint
        try:
            validate_token(token)
            validate_endpoint(endpoint)
        except EnvFileError as exc:
            logger.warning("activation: validation failed: %s", exc)
            self._bus.update(last_activation_error=str(exc))
            return ActivationAttempt(token=token, endpoint=endpoint, error=str(exc))

        try:
            write_token_and_endpoint(
                token=token,
                endpoint=endpoint,
                path=self._resolve_env_path(),
            )
        except OSError as exc:
            msg = f"无法写入 env 文件: {exc}"
            logger.exception("activation: env write failed")
            self._bus.update(last_activation_error=msg)
            return ActivationAttempt(token=token, endpoint=endpoint, error=msg)

        # Optimistically flip to "DISCONNECTED" — the gRPC client will
        # transition to CONNECTED on its own when the bidi stream opens.
        # The activation dialog's UI uses this transition to close itself.
        self._bus.update(
            cloud_link=CloudLinkState.DISCONNECTED,
            last_activation_error=None,
        )

        try:
            await self._grpc_restarter.restart(endpoint=endpoint, token=token)
        except Exception as exc:  # noqa: BLE001
            msg = f"云端拒绝激活码: {exc}"
            logger.warning("activation: grpc restart rejected: %s", exc)
            self._bus.update(
                cloud_link=CloudLinkState.AUTH_REJECTED,
                last_activation_error=msg,
            )
            return ActivationAttempt(token=token, endpoint=endpoint, error=msg)

        return ActivationAttempt(token=token, endpoint=endpoint, error=None)

    async def run_dialog(self) -> ActivationAttempt | None:
        """Show the modal PySide6 dialog and run one submission attempt.

        Returns the final attempt (``error=None`` if successful) or
        ``None`` if the user cancelled. Re-prompting on error is the
        caller's responsibility (``main_windows.py`` runs this in a
        loop while ``not status.is_activated``).
        """
        from PySide6 import QtWidgets  # noqa: PLC0415

        existing = self._bus.status.last_activation_error
        dialog = _ActivationDialog(
            initial_endpoint=self._default_endpoint,
            initial_error=existing,
        )

        # Qt's ``exec()`` is blocking; we run it on the Qt main thread
        # (same as the asyncio loop via qasync). It still yields control
        # to other Qt events via the nested event loop.
        result_code = dialog.exec()
        if result_code != QtWidgets.QDialog.DialogCode.Accepted:
            logger.info("activation: user cancelled")
            return None

        token = dialog.token()
        endpoint = dialog.endpoint() or self._default_endpoint
        attempt = await self.process_submission(token=token, endpoint=endpoint)
        if attempt.error is not None:
            # Reflect into the dialog model for the *next* call. We don't
            # auto-reopen the dialog here — the main loop in
            # main_windows.py decides whether to retry, surface a tray
            # notification, etc.
            self._bus.update(last_activation_error=attempt.error)
        return attempt


class _ActivationDialog:
    """Thin PySide6 wrapper. Defined as a regular class (NOT subclass of
    QDialog at module level) so the import of PySide6 stays lazy — the
    class body imports Qt symbols inside ``__init__``.
    """

    def __init__(self, *, initial_endpoint: str, initial_error: str | None) -> None:
        from PySide6 import QtCore, QtWidgets  # noqa: PLC0415

        self._dialog = QtWidgets.QDialog()
        self._dialog.setWindowTitle("iSales 激活")
        self._dialog.setModal(True)
        self._dialog.setMinimumWidth(420)

        layout = QtWidgets.QVBoxLayout(self._dialog)

        intro = QtWidgets.QLabel(
            "请粘贴运维下发的激活码。激活成功后图标会变绿。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QtWidgets.QFormLayout()
        self._token_edit = QtWidgets.QLineEdit()
        self._token_edit.setPlaceholderText("EDGE_DEVICE_TOKEN ...")
        self._token_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Normal)
        form.addRow("激活码", self._token_edit)

        self._endpoint_edit = QtWidgets.QLineEdit()
        self._endpoint_edit.setPlaceholderText(initial_endpoint)
        self._endpoint_edit.setText(initial_endpoint)
        form.addRow("云端地址", self._endpoint_edit)
        layout.addLayout(form)

        self._error_label = QtWidgets.QLabel(initial_error or "")
        self._error_label.setStyleSheet("color: #c0392b;")
        self._error_label.setWordWrap(True)
        layout.addWidget(self._error_label)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._dialog.accept)
        buttons.rejected.connect(self._dialog.reject)
        layout.addWidget(buttons)

        # Stash for ``exec()`` proxy.
        self._exec = self._dialog.exec
        # Reserved for tests / dynamic re-show; not used by the controller.
        self._qt_core = QtCore

    def exec(self) -> int:
        return self._exec()

    def token(self) -> str:
        return self._token_edit.text()

    def endpoint(self) -> str:
        return self._endpoint_edit.text()


__all__ = [
    "DEFAULT_CLOUD_ENDPOINT",
    "ActivationAttempt",
    "ActivationController",
    "GrpcRestarter",
]
