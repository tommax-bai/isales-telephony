"""Edge daemon entry point: ``isales-telephony-edge`` / ``python -m
isales_telephony``.

Spec: arch-cloud-edge-split / deployment-topology § "边缘 launchd" —
single LaunchAgent runs this entrypoint, which fan-outs into:

- the cloud-edge bidi gRPC stream (:class:`CloudEdgeGrpcClient`),
- the modem AT channel (:class:`SerialATClient` in prod /
  :class:`MockATClient` for CI),
- the orchestrator (:class:`EdgeOrchestrator`) wiring those two with
  per-call :class:`AudioBridge` instances.

Environment contract (see ``deploy/edge/env/edge.env.example``):

- ``ISALES_CLOUD_EDGE_ENDPOINT`` (required) — gRPC target, e.g.
  ``isales.example.com:443``.
- ``ISALES_EDGE_DEVICE_TOKEN`` (required) — bearer token signed by
  ``isales-edge-token-mint`` on the cloud side.
- ``ISALES_MODEM_SERIAL_PATH`` (required for real modem) — same env
  var the standalone modem-controller already consumes; absence with
  ``ISALES_ALLOW_MOCK_AT=1`` switches to :class:`MockATClient`.
- ``ISALES_RTC_SDK_BACKEND`` (optional, default ``macos_mock``) —
  selects RTC backend. v1.0 only ships ``macos_mock``; the D1 path
  will add a Windows-native backend.

Optional services:

- The telephony-api HTTP server is **not** started by this entry point
  in A2 — it is a separate launchd unit bound to ``127.0.0.1`` for
  boss-console device queries (see ``deploy/edge/launchd/``). Keeping
  the two units separate means a telephony-api crash doesn't kill the
  control plane.

Shutdown: SIGTERM triggers an asyncio cancellation that propagates
through the task group; the orchestrator's :meth:`stop` runs cleanly
and tears down all in-flight calls before the process exits.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from isales_telephony.audio_bridge.session import MacosRtcSession
from isales_telephony.edge.orchestrator import EdgeOrchestrator
from isales_telephony.modem_controller.audio_pipe import (
    CaptureBackend,
    PlaybackBackend,
)
from isales_telephony.modem_controller.main import _make_at_client
from isales_telephony.transport.grpc_client import CloudEdgeGrpcClient
from isales_telephony.transport.sqlite_buffer import SqliteEventBuffer

logger = logging.getLogger("isales.edge")


def _required_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"{name} is required to start isales-telephony-edge")
    return val


def _build_audio_backends() -> tuple[CaptureBackend, PlaybackBackend]:
    """Resolve modem-side capture / playback backends from env.

    v1.0 (A2) ships macOS Core Audio backends; Linux ALSA is reserved
    for the dev/CI fallback. Returns the already-open backends — they
    are HW-scoped, not per-call.
    """
    backend = os.environ.get("ISALES_EDGE_AUDIO_BACKEND", "macos")
    if backend == "macos":
        from isales_telephony.modem_controller.audio.macos_coreaudio import (
            MacOSCoreAudioCapture,
            MacOSCoreAudioPlayback,
        )

        capture = MacOSCoreAudioCapture()
        playback = MacOSCoreAudioPlayback()
        return capture, playback
    if backend == "linux":  # pragma: no cover — CI default uses mock
        from isales_telephony.modem_controller.audio.linux_alsa import (
            LinuxAlsaCapture,
            LinuxAlsaPlayback,
        )

        return LinuxAlsaCapture(), LinuxAlsaPlayback()
    if backend == "windows":  # pragma: no cover — Windows-only path
        from isales_telephony.modem_controller.audio.windows_wasapi import (
            WindowsWASAPICapture,
            WindowsWASAPIPlayback,
        )

        return WindowsWASAPICapture(), WindowsWASAPIPlayback()
    raise RuntimeError(f"unknown ISALES_EDGE_AUDIO_BACKEND={backend!r}")


def _build_event_buffer() -> SqliteEventBuffer | None:
    """Open the durable event buffer if the deploy provides a path.

    No path → in-memory only (dev / CI). See
    :class:`SqliteEventBuffer` for spec semantics.
    """
    path = os.environ.get("ISALES_EDGE_EVENT_BUFFER_PATH")
    if not path:
        return None
    buf = SqliteEventBuffer(path=path)
    buf.open()
    return buf


async def _arun() -> None:
    logging.basicConfig(level=os.environ.get("ISALES_LOG_LEVEL", "INFO"))

    endpoint = _required_env("ISALES_CLOUD_EDGE_ENDPOINT")
    token = _required_env("ISALES_EDGE_DEVICE_TOKEN")

    at_client = await _make_at_client()
    capture, playback = _build_audio_backends()
    event_buffer = _build_event_buffer()

    grpc_client = CloudEdgeGrpcClient(event_buffer=event_buffer)
    orchestrator = EdgeOrchestrator(
        grpc_client=grpc_client,
        at_client=at_client,
        capture=capture,
        playback=playback,
        rtc_session_factory=MacosRtcSession,
    )

    stop_event = asyncio.Event()

    def _request_stop() -> None:
        logger.info("edge_daemon_signal_received_stop")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:  # pragma: no cover — Windows
            signal.signal(sig, lambda *_: _request_stop())

    try:
        await grpc_client.start(endpoint=endpoint, token=token)
        await orchestrator.start()
        logger.info(
            "edge_daemon_started",
            extra={"endpoint": endpoint, "audio_backend": os.environ.get(
                "ISALES_EDGE_AUDIO_BACKEND", "macos"
            )},
        )
        await stop_event.wait()
    finally:
        logger.info("edge_daemon_stopping")
        await orchestrator.stop()
        await grpc_client.stop()
        if event_buffer is not None:
            event_buffer.close()
        if hasattr(at_client, "aclose"):
            try:
                await at_client.aclose()
            except Exception:  # noqa: BLE001
                logger.exception("at_client.aclose failed")
        logger.info("edge_daemon_stopped")


def run() -> None:
    """Console-script entry point: ``isales-telephony-edge``."""
    try:
        asyncio.run(_arun())
    except KeyboardInterrupt:  # pragma: no cover
        logger.info("edge_daemon_keyboard_interrupt")


if __name__ == "__main__":  # pragma: no cover
    run()
