"""Edge-side :class:`RtcSession` implementation for Windows.

Spec: openspec/changes/windows-artc-pybind11/specs/device-hardware/spec.md
      § Requirement: Windows 边缘 ARTC SDK 通过 pybind11 binding 接入.

Companion to :mod:`isales_telephony.audio_bridge.session` (macOS mock).
This module is the **real** RTC path used on commercial Windows edge PCs.

Wire-format (:class:`PcmFrame`) is identical to the mock — the engine /
``AudioBridge`` code paths get the same data shape they exercise in
unit tests.

Architecture
------------

``aliyun_artc_pywrap`` (pybind11 binding, built from
``deploy/edge/windows/pybind/aliyun_artc_pywrap/``) exposes:

- ``EngineHandle``: create / destroy / join / leave / push outbound
  audio; thin wrapper over the SDK's ``AliEngine`` + ``IAliEngineMediaEngine``.
- ``EngineListener``: trampoline for join / leave / connection-lost /
  error events. Runs on SDK threads — we marshal events back to the
  asyncio loop via ``loop.call_soon_threadsafe``.
- ``AudioObserver`` + ``FrameRingBuffer``: SDK-thread-safe collection
  of inbound PCM. The Python drainer task pulls frames batched at a
  fixed cadence (no per-frame GIL acquire — see binding's design.md
  Decision 3).

The pybind11 binding is Windows-only; importing this module on macOS /
Linux raises :class:`ImportError`. Use :class:`MacosRtcSession` for
macOS edge or :class:`InMemoryRtcSession` (from isales-common) for tests.
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
# ``sys.modules["aliyun_artc_pywrap"] = mock`` BEFORE importing this module.
try:
    import aliyun_artc_pywrap as _artc
except ImportError as exc:  # pragma: no cover — production deployment guarantees presence
    if sys.platform == "win32":
        raise ImportError(
            "aliyun_artc_pywrap is missing. On Windows edge, build it via "
            "deploy/edge/windows/build.ps1 (the script's CMake step "
            "produces aliyun_artc_pywrap.pyd before PyInstaller packages "
            "the frozen exe). See openspec/changes/windows-artc-pybind11/."
        ) from exc
    raise


# Sample format used by the iSales edge ↔ engine channel. Edge audio-
# bridge resamples modem 8 kHz → 16 kHz before push_audio; remote PCM
# arrives at 16 kHz and is resampled back to 8 kHz downstream.
_DEFAULT_SAMPLE_RATE = 16000
_DEFAULT_CHANNELS = 1
_DEFAULT_BYTES_PER_SAMPLE = 2

# Drainer cadence — pulls up to 8 frames per tick (≈ 160 ms of audio
# at 20 ms / frame) every 5 ms. With the SDK producing ~50 Hz this
# averages to <1 frame / tick under normal load.
_DRAIN_TICK_SECONDS = 0.005
_DRAIN_BATCH_SIZE = 8


class WindowsRtcSession(RtcSession):
    """Real :class:`RtcSession` for the Windows edge.

    Parameters
    ----------
    extras:
        Optional SDK init string passed verbatim to ``AliEngine::Create``.
        v1.0 leaves this empty (SDK defaults).
    inbound_capacity:
        Ring-buffer capacity (frames). 64 ≈ 1.3 s of audio @ 50 Hz,
        plenty of headroom for asyncio scheduler hiccups.
    """

    def __init__(self, *, extras: str = "", inbound_capacity: int = 64) -> None:
        self._engine: _artc.EngineHandle | None = None
        self._listener: _artc.EngineListener | None = None
        self._observer: _artc.AudioObserver | None = None
        self._buffer: _artc.FrameRingBuffer | None = None
        self._stream_id: int | None = None

        self._extras = extras
        self._inbound_capacity = inbound_capacity

        self._joined = False
        self._send_sample_rate = _DEFAULT_SAMPLE_RATE
        self._send_channels = _DEFAULT_CHANNELS
        self._peer_uid: str | None = None

        # Bridges SDK callback threads → asyncio loop.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._join_future: asyncio.Future[int] | None = None
        self._closed = False

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
            raise RtcError("WindowsRtcSession already joined")
        if self._closed:
            raise RtcError("WindowsRtcSession is closed; create a new instance")

        self._send_sample_rate = send_sample_rate
        self._send_channels = send_channels
        self._loop = asyncio.get_running_loop()
        self._join_future = self._loop.create_future()
        # peer uid mirrors the iSales A2 convention: edge joins as
        # "edge-{call_id}", engine joins as "engine-{call_id}". The
        # opposite party's uid is whatever the *other* side picked;
        # callers pass it via send_audio sender_uid metadata. For the
        # observer filter we use empty (accept all remote uids) since
        # iSales channels only ever have 2 participants.
        self._peer_uid = None

        self._engine = _artc.EngineHandle()
        self._engine.create(self._extras)

        # Wire up the event listener BEFORE join so we catch
        # OnJoinChannelResult. Use a local for capture closures.
        listener = _artc.EngineListener()

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
                "ARTC join result: rc=%d channel=%s uid=%s elapsed=%dms",
                result, channel_id, user_id, elapsed,
            )
            loop.call_soon_threadsafe(_safe_set_result, result)

        def _on_leave(result: int) -> None:
            logger.info("ARTC leave result: rc=%d", result)

        def _on_connection_lost() -> None:
            logger.warning("ARTC connection lost; SDK will attempt reconnect")

        def _on_error(code: int, message: str) -> None:
            logger.error("ARTC error %d: %s", code, message)
            # If we're still waiting for join, surface the error.
            loop.call_soon_threadsafe(
                _safe_set_exception,
                RtcError(f"ARTC error during join (code={code}): {message}"),
            )

        listener.set_on_join(_on_join)
        listener.set_on_leave(_on_leave)
        listener.set_on_connection_lost(_on_connection_lost)
        listener.set_on_error(_on_error)
        self._listener = listener
        self._engine.set_event_listener(listener)

        # Audio observer: collect remote frames into ring buffer.
        self._buffer = _artc.FrameRingBuffer(self._inbound_capacity)
        self._observer = _artc.AudioObserver(self._buffer, "")
        self._engine.register_audio_observer(self._observer)

        # External audio stream for outbound push_audio.
        self._stream_id = self._engine.add_external_audio_stream(
            channels=send_channels,
            sample_rate=send_sample_rate,
        )

        # Kick join. JoinChannel return value 0 = call accepted; we still
        # wait on _on_join for the actual result.
        try:
            self._engine.join_channel(token, channel, uid, uid)
        except _artc.AliyunArtcError as exc:
            await self._teardown_partial()
            raise RtcError(f"JoinChannel failed: {exc}") from exc

        # Wait for OnJoinChannelResult.
        try:
            result = await asyncio.wait_for(self._join_future, timeout=5.0)
        except asyncio.TimeoutError as exc:
            await self._teardown_partial()
            raise RtcError("JoinChannel timed out waiting for OnJoinChannelResult") from exc
        except RtcError:
            await self._teardown_partial()
            raise

        if result != 0:
            await self._teardown_partial()
            raise RtcError(f"OnJoinChannelResult reported failure (code={result})")

        # Start drainer.
        self._frame_queue = asyncio.Queue(maxsize=self._inbound_capacity * 2)
        self._drainer_task = self._loop.create_task(
            self._drain_loop(), name="windows-rtc-drainer",
        )
        self._joined = True

    async def leave(self) -> None:
        if not self._joined and self._closed:
            return  # idempotent
        try:
            if self._engine is not None:
                with contextlib.suppress(_artc.AliyunArtcError):
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
            if self._stream_id is not None:
                with contextlib.suppress(Exception):
                    self._engine.remove_external_audio_stream(self._stream_id)
                self._stream_id = None
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
        """Pull frames from the C++ ring buffer into asyncio.Queue.

        Runs as long as the session is joined. Cancellation is the
        normal teardown signal.
        """
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
                        sender_uid=raw.remote_uid or "",
                        pcm=bytes(raw.pcm),
                        sample_rate=raw.sample_rate or _DEFAULT_SAMPLE_RATE,
                        channels=raw.channels or _DEFAULT_CHANNELS,
                        timestamp_ms=raw.remote_capture_ms,
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
        if not self._joined or self._engine is None or self._stream_id is None:
            raise RtcNotJoined("push_audio() called before join() or after leave()")
        try:
            self._engine.push_external_audio(
                stream_id=self._stream_id,
                pcm=pcm,
                sample_rate=self._send_sample_rate,
                channels=self._send_channels,
                bytes_per_sample=_DEFAULT_BYTES_PER_SAMPLE,
            )
        except _artc.AliyunArtcError as exc:
            # Promote to RtcError; AudioBridge / EdgeOrchestrator layer
            # translates to HardwareAlert{kind="rtc_push_failed"}.
            raise RtcError(f"push_external_audio failed: {exc}") from exc


_DRAINER_SENTINEL: PcmFrame = PcmFrame(
    sender_uid="__sentinel__", pcm=b"", sample_rate=0, channels=0, timestamp_ms=0,
)
