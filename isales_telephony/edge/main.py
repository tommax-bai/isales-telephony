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

Dev / QA mode (macOS only):

- ``--dev-no-modem`` skips the modem stack entirely. The orchestrator
  routes Cloud2Edge.dial straight into ``rtc_session.join`` using the
  real Aliyun ARTC PaaS via :class:`MacosArtcPyObjCSession`. mac mic /
  speaker are handled by the ARTC SDK's default audio device routing.
  Spec: openspec/changes/macos-artc-pyobjc-binding/.

Shutdown: SIGTERM triggers an asyncio cancellation that propagates
through the task group; the orchestrator's :meth:`stop` runs cleanly
and tears down all in-flight calls before the process exits.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys

from isales_telephony.audio_bridge import get_default_rtc_session_factory
from isales_telephony.edge.grpc_heartbeat import grpc_heartbeat_loop
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
        from isales_telephony.modem_controller.audio.windows_serial_pcm import (
            WindowsSerialPcmCapture,
            WindowsSerialPcmPlayback,
        )

        from isales_telephony.modem_controller.platforms.windows_serial import (
            ModemDiscoveryError,
            discover_modem_serial_paths,
        )

        # Auto-discover the audio COM port by USB descriptor (no env COM
        # fallback — COM numbers re-enumerate every re-plug, so a hardcoded
        # value is guaranteed stale). Capture (read) and playback (write)
        # MUST share ONE handle on the Audio port: Windows COM ports are
        # exclusive, and uplink to the Diagnostics port (COM10) was proven
        # SILENT (2026-05-31). See deploy/edge/windows/STATE.md.
        try:
            _at_path, audio_path = discover_modem_serial_paths()
        except ModemDiscoveryError as exc:
            raise RuntimeError(
                f"windows audio backend: modem auto-discovery failed: {exc}"
            ) from exc
        capture = WindowsSerialPcmCapture(audio_path)
        playback = WindowsSerialPcmPlayback(audio_path)
        capture.open_port()
        playback.adopt_serial_from(capture)
        return capture, playback
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


def build_argparser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser.

    Exposed for unit tests (see ``tests/edge/test_main_cli.py``).
    """
    p = argparse.ArgumentParser(
        prog="isales-telephony-edge",
        description=(
            "iSales edge daemon. Production: env-configured, attached to "
            "a real GSM modem. Dev / QA (macOS only): --dev-no-modem "
            "skips the modem stack and uses real ARTC via PyObjC."
        ),
    )
    p.add_argument(
        "--dev-no-modem",
        action="store_true",
        help=(
            "macOS dev / QA only: skip modem stack, run audio over real "
            "ARTC via PyObjC binding. Fails fast on non-macOS hosts."
        ),
    )
    p.add_argument(
        "--dev-channel",
        type=str,
        default=None,
        help="dev RTC channel label (informational; cloud DialCommand "
        "supplies the actual rtc_channel used for join).",
    )
    p.add_argument(
        "--dev-uid",
        type=str,
        default=None,
        help="dev RTC uid label (informational; cloud DialCommand "
        "supplies the actual rtc_uid_edge used for join).",
    )
    p.add_argument(
        "--dev-peer-uid",
        type=str,
        default=None,
        help="dev RTC peer uid label (informational).",
    )
    return p


async def _arun_real(args: argparse.Namespace) -> None:
    """Production / non-dev startup: env-configured, modem-attached."""
    del args  # production path is env-driven; CLI flags are dev-only
    endpoint = _required_env("ISALES_CLOUD_EDGE_ENDPOINT")
    token = _required_env("ISALES_EDGE_DEVICE_TOKEN")

    # Device ID for gRPC heartbeat (PG device.id).
    device_id_raw = os.environ.get("ISALES_DEVICE_ID", "")
    device_id: int | None = int(device_id_raw) if device_id_raw.strip() else None
    if not device_id:
        logger.warning("ISALES_DEVICE_ID not set; gRPC heartbeat will be disabled")

    at_client = await _make_at_client()
    capture, playback = _build_audio_backends()
    event_buffer = _build_event_buffer()

    grpc_client = CloudEdgeGrpcClient(event_buffer=event_buffer)
    orchestrator = EdgeOrchestrator(
        grpc_client=grpc_client,
        at_client=at_client,
        capture=capture,
        playback=playback,
        rtc_session_factory=get_default_rtc_session_factory(
            app_id=os.environ.get("ISALES_RTC_APP_ID", ""),
        ),
    )

    stop_event = asyncio.Event()
    heartbeat_task: asyncio.Task[None] | None = None

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

        # Start gRPC heartbeat loop to sync device status to cloud.
        if device_id is not None:
            from isales_common.proto import cloud_edge_pb2 as pb  # noqa: PLC0415

            def _status_provider() -> int:
                if orchestrator.active_call_ids:
                    return int(pb.DEVICE_STATUS_IN_CALL)
                return int(pb.DEVICE_STATUS_IDLE)

            heartbeat_task = asyncio.create_task(
                grpc_heartbeat_loop(
                    client=grpc_client,
                    device_id=device_id,
                    status_provider=_status_provider,
                ),
                name="grpc_heartbeat",
            )

        logger.info(
            "edge_daemon_started",
            extra={"endpoint": endpoint, "audio_backend": os.environ.get(
                "ISALES_EDGE_AUDIO_BACKEND", "macos"
            )},
        )
        await stop_event.wait()
    finally:
        logger.info("edge_daemon_stopping")
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
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


async def _arun_dev_no_modem(args: argparse.Namespace) -> None:
    """macOS dev / QA startup: no modem, real ARTC via PyObjC.

    Cloud-side still drives DialCommand → DialAck → CallEvent the same
    way. The orchestrator's dev path uses ``dial.rtc_channel`` and
    ``dial.rtc_token`` from the cloud (same as the modem path); the
    ``--dev-channel`` / ``--dev-uid`` flags are informational labels
    written into the structured log so dev sessions are easy to
    correlate.
    """
    endpoint = _required_env("ISALES_CLOUD_EDGE_ENDPOINT")
    token = _required_env("ISALES_EDGE_DEVICE_TOKEN")
    event_buffer = _build_event_buffer()

    # Device ID for gRPC heartbeat (PG device.id).
    device_id_raw = os.environ.get("ISALES_DEVICE_ID", "")
    device_id: int | None = int(device_id_raw) if device_id_raw.strip() else None
    if not device_id:
        logger.warning("ISALES_DEVICE_ID not set; gRPC heartbeat will be disabled (dev)")

    grpc_client = CloudEdgeGrpcClient(event_buffer=event_buffer)
    orchestrator = EdgeOrchestrator(
        grpc_client=grpc_client,
        rtc_session_factory=get_default_rtc_session_factory(
            app_id=os.environ.get("ISALES_RTC_APP_ID", ""),
        ),
        dev_no_modem=True,
        dev_channel=args.dev_channel,
        dev_uid=args.dev_uid,
        dev_peer_uid=args.dev_peer_uid,
    )

    stop_event = asyncio.Event()
    heartbeat_task: asyncio.Task[None] | None = None

    def _request_stop() -> None:
        logger.info("edge_daemon_signal_received_stop_dev")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:  # pragma: no cover — non-Unix
            signal.signal(sig, lambda *_: _request_stop())

    try:
        await grpc_client.start(endpoint=endpoint, token=token)
        await orchestrator.start()

        # Start gRPC heartbeat loop to sync device status to cloud.
        if device_id is not None:
            from isales_common.proto import cloud_edge_pb2 as pb  # noqa: PLC0415

            def _status_provider() -> int:
                if orchestrator.active_call_ids:
                    return int(pb.DEVICE_STATUS_IN_CALL)
                return int(pb.DEVICE_STATUS_IDLE)

            heartbeat_task = asyncio.create_task(
                grpc_heartbeat_loop(
                    client=grpc_client,
                    device_id=device_id,
                    status_provider=_status_provider,
                ),
                name="grpc_heartbeat_dev",
            )

        logger.info(
            "edge_daemon_started_dev_no_modem",
            extra={
                "endpoint": endpoint,
                "dev_channel": args.dev_channel,
                "dev_uid": args.dev_uid,
                "dev_peer_uid": args.dev_peer_uid,
            },
        )
        await stop_event.wait()
    finally:
        logger.info("edge_daemon_stopping_dev")
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
        # Dev teardown: emit remote_hangup{dev_terminate} for any
        # in-flight call before tearing down the RTC sessions, so the
        # cloud's state machine retires the call cleanly.
        await orchestrator.dev_terminate_active_calls()
        await orchestrator.stop()
        await grpc_client.stop()
        if event_buffer is not None:
            event_buffer.close()
        logger.info("edge_daemon_stopped_dev")


async def _arun(args: argparse.Namespace) -> None:
    logging.basicConfig(level=os.environ.get("ISALES_LOG_LEVEL", "INFO"))
    if args.dev_no_modem:
        await _arun_dev_no_modem(args)
    else:
        await _arun_real(args)


def run(argv: list[str] | None = None) -> None:
    """Console-script entry point: ``isales-telephony-edge``.

    Args:
        argv: argv tail to parse. ``None`` uses ``sys.argv[1:]``.
            Exposed for unit-tests (see ``tests/edge/test_main_cli.py``).
    """
    parser = build_argparser()
    args = parser.parse_args(argv)

    if args.dev_no_modem and sys.platform != "darwin":
        print(
            "--dev-no-modem 仅 macOS 支持；Windows 商用走真 GSM modem + "
            "DingRTC pybind 真 RTC 路径。",
            file=sys.stderr,
        )
        raise SystemExit(2)

    try:
        asyncio.run(_arun(args))
    except KeyboardInterrupt:  # pragma: no cover
        logger.info("edge_daemon_keyboard_interrupt")


if __name__ == "__main__":  # pragma: no cover
    run()
