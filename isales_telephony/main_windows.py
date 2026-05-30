"""Windows edge-client entry point.

Spec: windows-client-core / deployment-topology § "单进程异步架构".

A single Python process hosts:

- qasync-wrapped Qt event loop (PySide6 ``QApplication``).
- asyncio task group: gRPC cloud-edge client, modem-controller AT
  channel, audio-bridge / orchestrator, optional local SQLite buffer.
- pystray tray icon (daemon thread) with right-click menu.
- PySide6 activation dialog (modal, first-launch only or on reactivate).
- PySide6 diagnostic window (lazy, opens on right-click menu).

Lifecycle
---------

1. Read ``%APPDATA%\\isales\\env\\telephony.env``.
2. Build ``StateBus`` + ``CloudEdgeGrpcClient`` + (optional) AT client.
3. If env file lacks ``ISALES_EDGE_DEVICE_TOKEN`` → run the activation
   dialog. Loop on activation failures (user can re-input).
4. Start the asyncio task group: gRPC client first (so cloud connects
   in parallel), then orchestrator + status-bridge.
5. Construct + start tray icon. Tray and Qt main loop now drive UX;
   asyncio task group runs in the background of the same qasync loop.
6. On "退出" menu → cancel task group, leave RTC, close gRPC, quit Qt.

Exceptions from asyncio tasks MUST NOT crash the tray. The task-group
runner catches and logs them, surfacing a tray-red transition via the
state bus.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from typing import TYPE_CHECKING

from isales_telephony.ui.activation import (
    DEFAULT_CLOUD_ENDPOINT,
    ActivationController,
    GrpcRestarter,
)

if TYPE_CHECKING:  # pragma: no cover  - typing only; runtime imports stay lazy
    from isales_telephony.transport.grpc_client import CloudEdgeGrpcClient
    from isales_telephony.transport.sqlite_buffer import SqliteEventBuffer
from isales_telephony.ui.diagnostic import RECENT_LOG_LINES_MAX, DiagnosticWindow
from isales_telephony.ui.env_writer import default_env_path, read_env
from isales_telephony.ui.state import CloudLinkState, EdgeStatus, StateBus
from isales_telephony.ui.tray import AsyncMenuBridge, TrayIconController

logger = logging.getLogger("isales.main_windows")


# ----------------------------------------------------------- logging bridge


class _RingBufferLogHandler(logging.Handler):
    """Push the last N log lines into ``EdgeStatus.recent_log_lines``.

    Spec: tasks.md § 5.3 — diagnostic window shows "最近 10 条 log".
    """

    def __init__(self, *, state_bus: StateBus, capacity: int) -> None:
        super().__init__()
        self._bus = state_bus
        self._capacity = capacity

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001
            return
        snap = self._bus.status
        new_lines = (*snap.recent_log_lines, line)[-self._capacity:]
        self._bus.update(recent_log_lines=new_lines)


# ----------------------------------------------------------- gRPC restarter


class _GrpcClientRestarter(GrpcRestarter):
    """Wraps the ``CloudEdgeGrpcClient`` so the activation controller
    can swap in a new token without rebuilding the orchestrator graph.
    """

    def __init__(self, *, client: "CloudEdgeGrpcClient", bus: StateBus) -> None:
        self._client = client
        self._bus = bus

    async def restart(self, *, endpoint: str, token: str) -> None:
        try:
            await self._client.stop()
        except Exception:  # noqa: BLE001
            logger.exception("grpc_restarter: stop() failed; continuing")
        await self._client.start(endpoint=endpoint, token=token)
        self._bus.update(cloud_link=CloudLinkState.CONNECTED)


# ----------------------------------------------------------- connection watcher


async def _watch_grpc_connection(
    *,
    client: "CloudEdgeGrpcClient",
    bus: StateBus,
    poll_interval_s: float = 1.0,
) -> None:
    """Poll the gRPC client and reflect connect/disconnect into StateBus.

    The gRPC client doesn't expose an event-style hook (only an
    ``is_connected`` property + the existing message callback). Polling
    is cheap and lets us avoid invasive client changes for D1; D2 can
    refactor to push-based if the watcher cost matters.
    """
    last: CloudLinkState | None = None
    while True:
        if client.is_connected:
            now = CloudLinkState.CONNECTED
        else:
            # Don't overwrite AWAITING_ACTIVATION or AUTH_REJECTED — those
            # are sticky states the activation flow owns.
            current = bus.status.cloud_link
            now = (
                current
                if current in (
                    CloudLinkState.AWAITING_ACTIVATION,
                    CloudLinkState.AUTH_REJECTED,
                )
                else CloudLinkState.DISCONNECTED
            )
        if now != last:
            bus.update(cloud_link=now)
            last = now
        await asyncio.sleep(poll_interval_s)


# ----------------------------------------------------------- event buffer


def _build_event_buffer() -> "SqliteEventBuffer | None":
    path = os.environ.get("ISALES_EDGE_EVENT_BUFFER_PATH")
    if not path:
        appdata = os.environ.get("APPDATA")
        if appdata:
            target = Path(appdata) / "isales" / "sqlite" / "edge_buffer.db"
            target.parent.mkdir(parents=True, exist_ok=True)
            path = str(target)
    if not path:
        return None
    from isales_telephony.transport.sqlite_buffer import (  # noqa: PLC0415
        SqliteEventBuffer,
    )

    buf = SqliteEventBuffer(path=path)
    buf.open()
    return buf


# ----------------------------------------------------------- env loader


def _load_env_file_into_environ(path: Path | None = None) -> dict[str, str]:
    """Merge env-file entries into ``os.environ``. Existing env vars
    take precedence (so deployment-time overrides still work)."""
    path = path or default_env_path()
    parsed = read_env(path)
    for key, value in parsed.items():
        os.environ.setdefault(key, value)
    return parsed


# ----------------------------------------------------------- main


async def _arun(*, stop_event: asyncio.Event, state_bus: StateBus) -> None:
    """Asyncio side: gRPC + orchestrator + connection watcher.

    Blocks until ``stop_event`` is set by the tray "退出" handler or
    a SIGINT. Tray + Qt UI run in parallel on the same event loop via
    qasync.
    """
    # Heavy imports kept lazy so this module imports on Linux/macOS CI
    # without dragging in the POSIX-only ``fcntl`` path inside the
    # existing ``at_client`` module (windows-client-core tasks.md notes
    # the fcntl-removal as a follow-up PR; until then ``main_windows.py``
    # is only fully runnable on Windows but its helpers stay testable).
    from isales_telephony.audio_bridge.session import (  # noqa: PLC0415
        MacosRtcSession,
    )
    from isales_telephony.edge.orchestrator import (  # noqa: PLC0415
        EdgeOrchestrator,
    )
    from isales_telephony.modem_controller.audio import (  # noqa: PLC0415
        get_capture_class,
        get_playback_class,
    )
    from isales_telephony.modem_controller.main import (  # noqa: PLC0415
        _make_at_client,
    )
    from isales_telephony.transport.grpc_client import (  # noqa: PLC0415
        CloudEdgeGrpcClient,
    )

    _load_env_file_into_environ()

    endpoint = os.environ.get("ISALES_CLOUD_GRPC_ENDPOINT", DEFAULT_CLOUD_ENDPOINT)
    token = os.environ.get("ISALES_EDGE_DEVICE_TOKEN", "")

    # AT + audio backends. Real Windows path picks the WindowsSerialWatcher
    # USB-COM dispatch + WASAPI audio; if the operator overrides via env
    # we honour that too.
    at_client = await _make_at_client()
    capture = get_capture_class()()
    playback = get_playback_class()()

    event_buffer = _build_event_buffer()
    grpc_client = CloudEdgeGrpcClient(event_buffer=event_buffer)
    restarter = _GrpcClientRestarter(client=grpc_client, bus=state_bus)

    orchestrator = EdgeOrchestrator(
        grpc_client=grpc_client,
        at_client=at_client,
        capture=capture,
        playback=playback,
        rtc_session_factory=MacosRtcSession,  # stub; D2 production swaps in WindowsDingRtcSession via audio_bridge.get_default_rtc_session_factory(app_id=...) — see edge/main.py
    )

    activation_controller = ActivationController(
        state_bus=state_bus,
        grpc_restarter=restarter,
    )

    # Stash on the running loop for tray menu callbacks.
    _GLOBAL_REFS["activation"] = activation_controller
    _GLOBAL_REFS["stop_event"] = stop_event
    _GLOBAL_REFS["state_bus"] = state_bus

    if not token:
        logger.info("main_windows: no token in env; showing activation dialog")
        state_bus.update(cloud_link=CloudLinkState.AWAITING_ACTIVATION)
        # Loop until the user activates successfully or quits.
        while not stop_event.is_set():
            attempt = await activation_controller.run_dialog()
            if attempt is None:
                # User cancelled — exit cleanly.
                stop_event.set()
                return
            if attempt.error is None:
                break
            # On error, the controller has updated the bus; loop and
            # re-prompt.
    else:
        # Token present; kick off the gRPC connection in the background.
        try:
            await grpc_client.start(endpoint=endpoint, token=token)
            state_bus.update(cloud_link=CloudLinkState.CONNECTED)
        except Exception:  # noqa: BLE001
            logger.exception("main_windows: initial gRPC connect failed")
            state_bus.update(cloud_link=CloudLinkState.DISCONNECTED)

    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(orchestrator.start(), name="orchestrator")
            tg.create_task(
                _watch_grpc_connection(client=grpc_client, bus=state_bus),
                name="grpc_watcher",
            )

            # Wait for the stop signal in a tracked task so the group
            # exits cleanly when the user hits "退出".
            async def _wait_stop() -> None:
                await stop_event.wait()
                raise asyncio.CancelledError("stop_event set")

            tg.create_task(_wait_stop(), name="stop_waiter")
    except* asyncio.CancelledError:
        pass
    finally:
        logger.info("main_windows: shutting down")
        try:
            await orchestrator.stop()
        except Exception:  # noqa: BLE001
            logger.exception("main_windows: orchestrator.stop failed")
        try:
            await grpc_client.stop()
        except Exception:  # noqa: BLE001
            logger.exception("main_windows: grpc_client.stop failed")
        if event_buffer is not None:
            event_buffer.close()
        if hasattr(at_client, "aclose"):
            try:
                await at_client.aclose()
            except Exception:  # noqa: BLE001
                logger.exception("main_windows: at_client.aclose failed")


# Module-level dict so tray menu callbacks (sync, run on pystray thread)
# can reach the live orchestrator pieces without us threading them through
# every closure. Populated by ``_arun`` on startup; read by the menu
# callbacks below.
_GLOBAL_REFS: dict[str, object] = {}


def _menu_open_diagnostic() -> None:
    bus = _GLOBAL_REFS.get("state_bus")
    if not isinstance(bus, StateBus):
        return
    win = _GLOBAL_REFS.get("diagnostic_window")
    if win is None:
        win = DiagnosticWindow(state_bus=bus)
        _GLOBAL_REFS["diagnostic_window"] = win
    assert isinstance(win, DiagnosticWindow)
    win.show()


def _menu_reactivate() -> None:
    controller = _GLOBAL_REFS.get("activation")
    if not isinstance(controller, ActivationController):
        return
    loop = asyncio.get_event_loop()
    loop.create_task(controller.run_dialog(), name="reactivate_dialog")


def _menu_quit() -> None:
    stop = _GLOBAL_REFS.get("stop_event")
    if isinstance(stop, asyncio.Event):
        stop.set()


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop_event: asyncio.Event) -> None:
    """Best-effort SIGINT / SIGTERM. On Windows ``add_signal_handler``
    raises ``NotImplementedError``; fall back to ``signal.signal``."""
    def _request_stop() -> None:
        logger.info("main_windows: signal received, stopping")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, AttributeError):
            try:
                signal.signal(sig, lambda *_: _request_stop())
            except (ValueError, OSError):
                # Some signals can't be installed from non-main thread.
                pass


def run() -> None:
    """Process entry point. Constructs qasync loop, runs ``_arun``."""
    logging.basicConfig(
        level=os.environ.get("ISALES_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # qasync + PySide6 imports are kept inside ``run`` so this module
    # imports cleanly on Linux / macOS for unit testing.
    from PySide6 import QtWidgets  # noqa: PLC0415
    import qasync  # noqa: PLC0415

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    # Tray app: closing the activation dialog must NOT exit the app.
    app.setQuitOnLastWindowClosed(False)

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    state_bus = StateBus()
    log_handler = _RingBufferLogHandler(
        state_bus=state_bus, capacity=RECENT_LOG_LINES_MAX
    )
    log_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logging.getLogger().addHandler(log_handler)

    stop_event = asyncio.Event()
    _install_signal_handlers(loop, stop_event)

    menu_bridge = AsyncMenuBridge(
        loop=loop,
        on_open_diagnostic=_menu_open_diagnostic,
        on_reactivate=_menu_reactivate,
        on_quit=_menu_quit,
    )
    tray = TrayIconController(state_bus=state_bus, actions=menu_bridge)
    tray.start()
    _GLOBAL_REFS["tray"] = tray

    try:
        with loop:
            loop.run_until_complete(
                _arun(stop_event=stop_event, state_bus=state_bus)
            )
    finally:
        tray.stop()
        # Drop the log handler so a follow-up run() in tests doesn't
        # accumulate duplicates.
        logging.getLogger().removeHandler(log_handler)


if __name__ == "__main__":  # pragma: no cover
    run()


__all__ = ["run"]
