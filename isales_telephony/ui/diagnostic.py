"""Read-only PySide6 diagnostic window.

Spec: tasks.md § 5.3 — "PySide6 诊断小窗（cloud-edge 连接状态、modem
列表、最近 10 条 log）".

This is the simplest possible widget that satisfies D1: one ``show()``
+ live re-render when the state bus pushes a new snapshot. D2
``hardware-observability`` will expand this into the "一键诊断" panel
with per-modem SIM info, alert history, etc.; we keep the surface area
minimal here so D2 doesn't have to refactor binding state plumbing —
only widen the rendered view.

Threading
---------

The widget lives on the Qt main thread (== asyncio thread via qasync).
``StateBus`` may publish from arbitrary threads; we forward into the
Qt thread via a ``QtCore.QObject.event`` PostEvent. Implementation
detail: we connect the subscriber to a ``Signal`` defined on the
widget, which automatically marshals across thread boundaries.

Log capture
-----------

The "最近 10 条 log" line list is sourced from a ring buffer in
``isales_telephony.ui.state.EdgeStatus.recent_log_lines``. The actual
log capture (a ``logging.Handler`` that appends to that ring) is
installed by ``main_windows.py`` so unit tests can supply pre-baked
log lines via ``state_bus.update(recent_log_lines=...)``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from isales_telephony.ui.state import EdgeStatus, StateBus

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

RECENT_LOG_LINES_MAX = 10


class DiagnosticWindow:
    """Wrap a PySide6 ``QWidget`` and bind it to the state bus.

    Constructed lazily by ``main_windows.py`` — we don't keep the
    widget around between right-click ``"打开诊断窗口"`` invocations to
    avoid leaking a top-level window if the user dismisses the
    activation dialog mid-flight.
    """

    def __init__(self, *, state_bus: StateBus) -> None:
        from PySide6 import QtCore, QtWidgets  # noqa: PLC0415

        self._bus = state_bus

        widget = QtWidgets.QWidget()
        widget.setWindowTitle("iSales 诊断")
        widget.setMinimumSize(420, 320)

        layout = QtWidgets.QVBoxLayout(widget)

        self._cloud_label = QtWidgets.QLabel()
        self._cloud_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self._cloud_label)

        modem_group = QtWidgets.QGroupBox("Modem 列表")
        modem_v = QtWidgets.QVBoxLayout(modem_group)
        self._modem_list = QtWidgets.QListWidget()
        modem_v.addWidget(self._modem_list)
        layout.addWidget(modem_group)

        log_group = QtWidgets.QGroupBox(f"最近 {RECENT_LOG_LINES_MAX} 条日志")
        log_v = QtWidgets.QVBoxLayout(log_group)
        self._log_list = QtWidgets.QListWidget()
        self._log_list.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.NoSelection
        )
        log_v.addWidget(self._log_list)
        layout.addWidget(log_group)

        self._error_label = QtWidgets.QLabel()
        self._error_label.setStyleSheet("color: #c0392b;")
        self._error_label.setWordWrap(True)
        layout.addWidget(self._error_label)

        self._widget = widget
        self._unsubscribe = self._bus.subscribe(self._on_status_thread_safe)
        # Initial render.
        self._render(self._bus.status)

    def show(self) -> None:
        """Bring the window to the foreground (Windows raise + focus)."""
        self._widget.show()
        self._widget.raise_()
        self._widget.activateWindow()

    def close(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._widget.close()

    def _on_status_thread_safe(self, status: EdgeStatus) -> None:
        """StateBus callback. May fire from any thread; we marshal onto
        the Qt thread via QMetaObject.invokeMethod with QueuedConnection.
        """
        from PySide6 import QtCore  # noqa: PLC0415

        # Capture status in a closure for the queued invocation.
        def _do_render() -> None:
            self._render(status)

        QtCore.QMetaObject.invokeMethod(
            self._widget,
            lambda: _do_render(),  # type: ignore[arg-type]
            QtCore.Qt.ConnectionType.QueuedConnection,
        )

    def _render(self, status: EdgeStatus) -> None:
        """Synchronous UI update — call from the Qt thread."""
        self._cloud_label.setText(
            f"云端连接: {status.cloud_link.value}"
        )

        self._modem_list.clear()
        if not status.modems:
            self._modem_list.addItem("(尚未发现 modem)")
        else:
            for m in status.modems:
                self._modem_list.addItem(f"{m.device_node}  —  {m.state.value}")

        self._log_list.clear()
        lines = list(status.recent_log_lines[-RECENT_LOG_LINES_MAX:])
        for line in lines:
            self._log_list.addItem(line)

        self._error_label.setText(status.last_activation_error or "")


__all__ = [
    "RECENT_LOG_LINES_MAX",
    "DiagnosticWindow",
]
