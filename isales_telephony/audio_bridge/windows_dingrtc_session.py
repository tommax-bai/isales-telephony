"""Edge-side :class:`RtcSession` for Windows backed by DingRTC pybind11 binding.

Spec: openspec/changes/engine-rtc-dingrtc-migration § 7.6
      (Windows 高层 DingRtcSession 包装类).

Companion of :mod:`isales_telephony.audio_bridge.windows_rtc_session`
(ARTC) which is [LEGACY] and will be removed in § 7.11. Mirror of
:class:`isales_engine.transport.dingrtc.DingRtcSession` (cloud Linux),
adapted to the Windows pybind11 binding produced by
``deploy/edge/windows/pybind/dingrtc_pywrap/``.

Wire-format (:class:`PcmFrame`) is identical across all RtcSession
impls — the engine / ``AudioBridge`` code paths see the same data
shape they exercise in tests.

Differences from the ARTC version:

- Constructor takes ``app_id`` + ``gslb_server`` (DingRTC's
  ``RtcEngineAuthInfo`` bundles 5 credentials, two of which are now
  required at construction rather than per-join).
- ``join`` calls binding's 6-arg ``join_channel(channel, uid, app_id,
  token, gslb_server, user_name)``.
- External audio source switched from ARTC's per-stream model
  (``add_external_audio_stream → stream_id``) to DingRTC's global
  ``set_external_audio_source(True, sample_rate, channels)`` + per-frame
  ``push_external_audio`` (no stream_id).
- Audio observer requires explicit ``enable_audio_frame_observer(True,
  POSITION_PLAYBACK)`` (ARTC enabled by default once registered).
- Outbound PCM must be sliced to 10 ms grid per DingRTC SDK constraint
  (chunks > ~250 ms raise ``DingRtcError: PushExternalAudioFrame failed``;
  observed empirically on cloud Linux 3.9.0).
- No ``set_on_connection_lost`` callback — DingRTC binding exposes
  ``set_on_bye`` + ``set_on_connection_status_change`` instead; not
  wired here as v1.0 does not depend on them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import AsyncIterator

from isales_common.audio.rtc import (
    PcmFrame,
    RtcError,
    RtcNotJoined,
    RtcSession,
)

logger = logging.getLogger(__name__)

# Module-level import; resolved at module load. Tests substitute via
# ``sys.modules["dingrtc_pywrap"] = mock`` BEFORE importing this module.
# Production Windows callers must arrange the DLL search path before
# this import (``os.add_dll_directory(<vendor>/lib/x64)``) or rely on
# build.ps1's vendor-DLL-next-to-pyd co-location for the frozen exe.
try:
    import dingrtc_pywrap as _dingrtc
except ImportError as exc:  # pragma: no cover — production deployment guarantees presence
    if sys.platform == "win32":
        raise ImportError(
            "dingrtc_pywrap is missing. On Windows edge, build it via "
            "deploy/edge/windows/pybind/dingrtc_pywrap (CMake) — see "
            "isales-telephony/deploy/edge/windows/STATE.md "
            "§ DingRTC binding build for the recipe — or run the full "
            "deploy/edge/windows/build.ps1. Vendor DLLs must be on the "
            "DLL search path (use os.add_dll_directory or copy next "
            "to the .pyd)."
        ) from exc
    raise


# Sample format used by the iSales edge ↔ engine channel. Edge audio-
# bridge resamples modem 8 kHz → 16 kHz before push_audio; remote PCM
# arrives at 16 kHz and is resampled back to 8 kHz downstream.
_DEFAULT_SAMPLE_RATE = 16000
_DEFAULT_CHANNELS = 1
_DEFAULT_BYTES_PER_SAMPLE = 2
_DEFAULT_GSLB = "https://gslb.dingrtc.com"

# Drainer cadence — pulls up to 8 frames per tick (≈ 160 ms of audio
# at 20 ms / frame) every 5 ms. With the SDK producing ~50 Hz this
# averages to <1 frame / tick under normal load.
_DRAIN_TICK_SECONDS = 0.005
_DRAIN_BATCH_SIZE = 8

# OnJoinChannelResult must fire within this window; otherwise leave +
# raise. Cloud uses 10s; mirror.
_JOIN_TIMEOUT_SECONDS = 10.0

# DingRTC SDK constraint: PushExternalAudioFrame expects 10 ms PCM frames
# (``samples_per_10ms = sample_rate / 100``). Chunks > ~250 ms raise
# ``DingRtcError: PushExternalAudioFrame failed`` (cloud Linux 3.9.0
# observation: SDK internal jitter buffer ~200-250 ms). Slice + pace
# at ~8 ms / frame (slightly faster than real-time so SDK has time to
# drain into network).
_PUSH_FRAME_GRID_MS = 10
_PUSH_PACE_SECONDS = 0.008


class WindowsDingRtcSession(RtcSession):
    """Real :class:`RtcSession` for Windows edge backed by DingRTC binding.

    Parameters
    ----------
    app_id:
        DingRTC AppId (vendor-issued; demo path uses ``a4zfr1hn``,
        production reads from ``ISALES_RTC_APP_ID`` env on the edge).
        Required — DingRTC's RtcEngineAuthInfo bundles AppId into
        every join call.
    gslb_server:
        DingRTC GSLB endpoint. Default ``https://gslb.dingrtc.com``
        works for production and demo. ``fetch_demo_token`` may return
        a per-token GSLB override (vendor demo path) — caller passes
        that via this kwarg.
    inbound_capacity:
        Ring-buffer capacity (frames). 64 ≈ 1.3 s of audio @ 50 Hz —
        plenty of headroom for asyncio scheduler hiccups.
    """

    def __init__(
        self,
        *,
        app_id: str,
        gslb_server: str = _DEFAULT_GSLB,
        inbound_capacity: int = 64,
    ) -> None:
        if not app_id:
            raise ValueError("WindowsDingRtcSession requires app_id")
        self._app_id = app_id
        self._gslb_server = gslb_server
        self._inbound_capacity = inbound_capacity

        self._engine: _dingrtc.EngineHandle | None = None
        self._listener: _dingrtc.EngineListener | None = None
        self._observer: _dingrtc.AudioObserver | None = None
        self._buffer: _dingrtc.FrameRingBuffer | None = None

        self._joined = False
        self._closed = False
        self._send_sample_rate = _DEFAULT_SAMPLE_RATE
        self._send_channels = _DEFAULT_CHANNELS

        # Bridges SDK callback threads → asyncio loop.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._join_future: asyncio.Future[int] | None = None

        # async iterator plumbing.
        self._frame_queue: asyncio.Queue[PcmFrame] | None = None
        self._drainer_task: asyncio.Task[None] | None = None

    # ----- lifecycle -----

    @property
    def is_joined(self) -> bool:
        return self._joined

    async def join(
        self,
        channel: str,
        token: str,
        uid: str,
        *,
        send_sample_rate: int = _DEFAULT_SAMPLE_RATE,
        send_channels: int = _DEFAULT_CHANNELS,
    ) -> None:
        if self._joined:
            raise RtcError("WindowsDingRtcSession already joined")
        if self._closed:
            raise RtcError("WindowsDingRtcSession is closed; create new instance")

        self._send_sample_rate = int(send_sample_rate)
        self._send_channels = int(send_channels)
        self._loop = asyncio.get_running_loop()
        self._join_future = self._loop.create_future()

        engine = _dingrtc.EngineHandle()
        listener = _dingrtc.EngineListener()
        frame_buffer = _dingrtc.FrameRingBuffer(self._inbound_capacity)
        observer = _dingrtc.AudioObserver(frame_buffer, "")

        loop = self._loop
        join_future = self._join_future

        def _safe_set_result(value: int) -> None:
            if not join_future.done():
                join_future.set_result(value)

        def _safe_set_exception(exc: BaseException) -> None:
            if not join_future.done():
                join_future.set_exception(exc)

        def _on_join(result: int, channel_id: str, user_id: str, elapsed: int) -> None:
            logger.info(
                "dingrtc_join_result rc=%d (0x%08X) channel=%s uid=%s elapsed_ms=%d",
                result, result & 0xFFFFFFFF, channel_id, user_id, elapsed,
            )
            loop.call_soon_threadsafe(_safe_set_result, result)

        def _on_leave(stats=None) -> None:
            logger.info("dingrtc_leave_result stats=%s", stats)

        def _on_error(code: int, message: str) -> None:
            logger.error(
                "dingrtc_error code=%d (0x%08X) msg=%s",
                code, code & 0xFFFFFFFF, message,
            )
            # Surface as a join failure if we're still pending OnJoinChannelResult.
            loop.call_soon_threadsafe(
                _safe_set_exception,
                RtcError(f"DingRTC error during join (code={code}): {message}"),
            )

        listener.set_on_join(_on_join)
        listener.set_on_leave(_on_leave)
        listener.set_on_error(_on_error)

        try:
            engine.create("")
            engine.set_event_listener(listener)
            engine.register_audio_observer(observer)
            engine.enable_audio_frame_observer(
                enabled=True, position=_dingrtc.POSITION_PLAYBACK,
            )
            engine.set_external_audio_source(
                enable=True,
                sample_rate=self._send_sample_rate,
                channels=self._send_channels,
            )
            engine.publish_local_audio_stream(enabled=True)
            engine.publish_local_video_stream(enabled=False)
            engine.subscribe_all_remote_audio_streams(sub=True)
            engine.join_channel(
                channel_id=channel,
                user_id=uid,
                app_id=self._app_id,
                token=token,
                gslb_server=self._gslb_server,
                user_name=uid,
            )
        except _dingrtc.DingRtcError as exc:
            # Tear down partially-constructed engine before propagating.
            with contextlib.suppress(Exception):
                engine.destroy()
            raise RtcError(f"DingRTC join setup failed: {exc}") from exc

        # Stash refs so leave() can tear down + observer / listener stay
        # alive for the SDK to call back into.
        self._engine = engine
        self._listener = listener
        self._buffer = frame_buffer
        self._observer = observer

        try:
            result = await asyncio.wait_for(
                self._join_future, timeout=_JOIN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            await self._teardown_partial()
            raise RtcError(
                f"JoinChannel timed out after {_JOIN_TIMEOUT_SECONDS}s "
                "waiting for OnJoinChannelResult"
            ) from exc
        except RtcError:
            await self._teardown_partial()
            raise

        if result != 0:
            await self._teardown_partial()
            raise RtcError(
                f"OnJoinChannelResult rc={result} "
                f"(0x{result & 0xFFFFFFFF:08X}) — see DingRTC error code reference"
            )

        # Start drainer.
        self._frame_queue = asyncio.Queue(maxsize=self._inbound_capacity * 2)
        self._drainer_task = self._loop.create_task(
            self._drain_loop(), name="windows-dingrtc-drainer",
        )
        self._joined = True

    async def leave(self) -> None:
        if not self._joined and self._closed:
            return  # idempotent
        try:
            if self._engine is not None:
                with contextlib.suppress(Exception):
                    self._engine.leave_channel()
        finally:
            await self._teardown_partial()
            self._joined = False
            self._closed = True

    async def _teardown_partial(self) -> None:
        if self._drainer_task is not None:
            self._drainer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, BaseException):
                await self._drainer_task
            self._drainer_task = None

        if self._engine is not None:
            with contextlib.suppress(Exception):
                self._engine.unregister_audio_observer()
            with contextlib.suppress(Exception):
                self._engine.destroy()
            self._engine = None

        if self._listener is not None:
            with contextlib.suppress(Exception):
                self._listener.clear_callbacks()
            self._listener = None
        self._observer = None
        self._buffer = None

        if self._frame_queue is not None:
            # Wake any waiter on audio_frames so it can terminate cleanly.
            with contextlib.suppress(asyncio.QueueFull):
                self._frame_queue.put_nowait(_DRAINER_SENTINEL)

    # ----- inbound PCM -----

    async def audio_frames(self) -> AsyncIterator[PcmFrame]:
        if not self._joined or self._frame_queue is None:
            raise RtcNotJoined("audio_frames() called before join() or after leave()")
        queue = self._frame_queue
        while True:
            frame = await queue.get()
            if frame is _DRAINER_SENTINEL:
                return
            yield frame

    async def _drain_loop(self) -> None:
        """Pull frames from the C++ ring buffer into asyncio.Queue."""
        assert self._buffer is not None
        assert self._frame_queue is not None
        buf = self._buffer
        q = self._frame_queue
        try:
            while True:
                frames = buf.drain(_DRAIN_BATCH_SIZE)
                if not frames:
                    await asyncio.sleep(_DRAIN_TICK_SECONDS)
                    continue
                for raw in frames:
                    pcm_frame = PcmFrame(
                        sender_uid=getattr(raw, "remote_uid", "") or "",
                        pcm=bytes(getattr(raw, "pcm", b"")),
                        sample_rate=int(
                            getattr(raw, "sample_rate", 0) or _DEFAULT_SAMPLE_RATE
                        ),
                        channels=int(
                            getattr(raw, "channels", 1) or _DEFAULT_CHANNELS
                        ),
                        timestamp_ms=int(getattr(raw, "timestamp", 0) or 0),
                    )
                    try:
                        q.put_nowait(pcm_frame)
                    except asyncio.QueueFull:
                        # Drop oldest pending frame, keep the new one.
                        with contextlib.suppress(asyncio.QueueEmpty):
                            q.get_nowait()
                        q.put_nowait(pcm_frame)
        except asyncio.CancelledError:
            raise

    # ----- outbound PCM -----

    async def push_audio(self, pcm: bytes, *, timestamp_ms: int) -> None:
        engine = self._engine
        if not self._joined or engine is None:
            raise RtcNotJoined("push_audio() called before join() or after leave()")
        if self._send_sample_rate <= 0 or self._send_channels <= 0:
            raise RtcNotJoined(
                "push_audio before join completed (rate/channels unset)"
            )
        if not pcm:
            return

        # DingRTC SDK requires 10 ms PCM frames. Slice + pace per cloud
        # observation: SDK internal jitter buffer ~200-250 ms, bigger chunks
        # raise PushExternalAudioFrame failed. ts_step matches the grid.
        frame_size = (
            (self._send_sample_rate // 100)
            * self._send_channels
            * _DEFAULT_BYTES_PER_SAMPLE
        )
        ts = int(timestamp_ms)
        try:
            for off in range(0, len(pcm), frame_size):
                frame = pcm[off:off + frame_size]
                # Pad the last (potentially short) frame to a full SDK
                # frame so the codec sees a clean 10 ms shape.
                if len(frame) < frame_size:
                    frame = frame + b"\x00" * (frame_size - len(frame))
                engine.push_external_audio(
                    pcm=frame,
                    sample_rate=self._send_sample_rate,
                    channels=self._send_channels,
                    bytes_per_sample=_DEFAULT_BYTES_PER_SAMPLE,
                    timestamp=ts,
                )
                ts += _PUSH_FRAME_GRID_MS
                if off + frame_size < len(pcm):
                    await asyncio.sleep(_PUSH_PACE_SECONDS)
        except _dingrtc.DingRtcError as exc:
            code = exc.args[0] if exc.args else -1
            logger.warning(
                "dingrtc_push_audio_failed rc=%s msg=%s frame_size=%s "
                "send_rate=%s send_ch=%s pcm_len=%s",
                code, str(exc), frame_size,
                self._send_sample_rate, self._send_channels, len(pcm),
            )
            # TODO §7.7 实测: identify ERR_AUDIO_BUFFER_FULL family for
            # RtcPushBackpressure surfacing; until then raise plain RtcError.
            raise RtcError(str(exc)) from exc


_DRAINER_SENTINEL: PcmFrame = PcmFrame(
    sender_uid="__sentinel__", pcm=b"", sample_rate=0, channels=0, timestamp_ms=0,
)
