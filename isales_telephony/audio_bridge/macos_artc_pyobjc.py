"""Edge-side :class:`RtcSession` implementation for macOS dev / QA hosts.

Spec: openspec/changes/macos-artc-pyobjc-binding/specs/device-hardware/spec.md
      § Requirement: macOS 边缘 ARTC SDK 通过 PyObjC binding 接入.

Companion to :mod:`isales_telephony.audio_bridge.session` (the in-process
mock loopback kept for CI / unit-tests) and
:mod:`isales_telephony.audio_bridge.windows_rtc_session` (pybind11 binding,
Windows commercial path). The wire-format (:class:`PcmFrame`) is identical
across all three — the engine / :class:`AudioBridge` code paths get the
same data shape they exercise in unit tests.

Architecture
------------

The macOS ARTC SDK ships as ``AliRTCSdk.framework`` — a Mach-O universal
binary with ~19 Obj-C-only Headers. There is no Python wrapper. We bridge
into Python via PyObjC:

- ``objc.loadBundle("AliRTCSdk", bundle_path=...)`` injects every Obj-C
  class declared in the framework's headers into the calling module's
  globals (so ``AliRtcEngine`` etc. become attributes on this module).
- ``NSObject`` subclass ``_AliRtcAudioDelegate`` plays the role of
  ``AliRtcEngineDelegate``. It implements only the audio-only subset of
  selectors (≤ 9) that v1.0 needs — no video, no screenshare, no full
  90+ event interface.
- Delegate callbacks run on Cocoa / random SDK threads; the bridge
  marshals every event back to the asyncio loop via
  ``loop.call_soon_threadsafe(...)``. Closures capture *values only*
  (ints / strs / bytes / Python tuples) — never PyObjC objects — to
  avoid cross-thread Obj-C autorelease-pool races.

The PyObjC + framework dependencies are optional (extras ``[macos-artc]``).
Importing this module without them raises :class:`ImportError` with an
explicit install hint. :func:`audio_bridge.get_default_rtc_session_class`
catches that ``ImportError`` on macOS and falls back to the mock loopback
session with a WARN log.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from isales_common.audio.rtc import (
    PcmFrame,
    RtcError,
    RtcNotJoined,
    RtcPushBackpressure,
    RtcSession,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PyObjC import (extras `[macos-artc]`)
# ---------------------------------------------------------------------------
#
# Module-level imports so audio_bridge/__init__.py can catch ImportError
# and fall back to MacosRtcSession. Tests substitute fakes into
# sys.modules['objc'] / sys.modules['Foundation'] before importing.

try:  # pragma: no cover — exercised via fakes in tests
    import objc  # type: ignore[import-not-found]
    from Foundation import NSObject  # type: ignore[import-not-found]
except ImportError as exc:  # pragma: no cover
    if sys.platform == "darwin":
        raise ImportError(
            "pyobjc-core / pyobjc-framework-Cocoa not installed. "
            "Install with: pip install -e '.[macos-artc]' (macOS dev / "
            "QA only — Windows commercial path uses pybind11)."
        ) from exc
    raise


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_SAMPLE_RATE = 16000
_DEFAULT_CHANNELS = 1

#: Default framework search path. Overridable via env var
#: ``ISALES_MACOS_ARTC_FRAMEWORK_PATH`` (lower priority than constructor
#: ``framework_path`` argument).
DEFAULT_FRAMEWORK_PATH = Path.home() / "codes" / "vendor" / "AliRTCSdk_macos" / "AliRTCSdk.framework"

#: Env var override.
FRAMEWORK_PATH_ENV = "ISALES_MACOS_ARTC_FRAMEWORK_PATH"

#: Join timeout (mirrors WindowsRtcSession's 5 s).
_JOIN_TIMEOUT_SECONDS = 5.0

#: Inbound asyncio.Queue cap multiplier — matches WindowsRtcSession
#: (Queue maxsize = inbound_capacity * 2 lets the drainer batch up to
#: a full ring's worth of frames without blocking on put).
_QUEUE_OVERSUBSCRIBE = 2

#: Sentinel pushed onto the inbound queue when audio_frames() should
#: terminate (signals "leave() called"). Identity comparison only.
_DRAINER_SENTINEL: PcmFrame = PcmFrame(
    sender_uid="__sentinel__", pcm=b"", sample_rate=0, channels=0, timestamp_ms=0,
)


# ---------------------------------------------------------------------------
# Framework loader
# ---------------------------------------------------------------------------


def _resolve_framework_path(framework_path: str | os.PathLike[str] | None) -> Path:
    """Resolve the framework path with priority arg > env > default."""
    if framework_path is not None:
        return Path(framework_path)
    env = os.environ.get(FRAMEWORK_PATH_ENV)
    if env:
        return Path(env)
    return DEFAULT_FRAMEWORK_PATH


def _load_framework(framework_path: str | os.PathLike[str] | None) -> Any:
    """Load ``AliRTCSdk.framework`` via PyObjC and return the ``AliRtcEngine`` class.

    Args:
        framework_path: Optional explicit path to the framework bundle.
            Falls back to env var ``ISALES_MACOS_ARTC_FRAMEWORK_PATH``
            and then to the conventional ``~/codes/vendor/...`` path.

    Raises:
        RtcError: if the path does not exist, is not a framework bundle,
            ``objc.loadBundle`` rejects it (wrong arch / linker error),
            or the ``AliRtcEngine`` Obj-C class cannot be looked up after
            loading.
    """
    resolved = _resolve_framework_path(framework_path)
    if not resolved.exists():
        raise RtcError(
            f"AliRTCSdk framework not found at {resolved!s}. "
            f"Download AliRTCSdk_7.8.10000-SNAPSHOT.zip and unzip to "
            f"~/codes/vendor/AliRTCSdk_macos/, or set "
            f"{FRAMEWORK_PATH_ENV} to override."
        )
    if not resolved.is_dir() or resolved.suffix != ".framework":
        raise RtcError(
            f"Path {resolved!s} is not a .framework bundle directory. "
            f"Expected an unzipped AliRTCSdk.framework directory."
        )
    # Validate the framework's binary exists (catches "I copied just the
    # Headers dir" mistakes early).
    binary = resolved / resolved.stem
    if not binary.exists():
        raise RtcError(
            f"Framework binary missing: {binary!s}. Re-extract the SDK zip; "
            f"`file {binary}` should report a Mach-O universal binary."
        )

    # Load into our module globals so subsequent ``globals()[...]`` lookups
    # see the Obj-C classes the framework declares. PyObjC mutates the
    # provided globals dict in place.
    module_globals = globals()
    try:
        objc.loadBundle(
            "AliRTCSdk",
            module_globals=module_globals,
            bundle_path=str(resolved),
        )
    except Exception as exc:  # noqa: BLE001 — PyObjC raises a broad family
        raise RtcError(
            f"objc.loadBundle failed for {resolved!s}: {exc}. "
            f"Verify the binary is a universal Mach-O (run "
            f"`file {binary}`) and the host architecture matches."
        ) from exc

    engine_cls = module_globals.get("AliRtcEngine")
    if engine_cls is None:
        raise RtcError(
            "AliRtcEngine Obj-C class not found after loading "
            f"{resolved!s}. The framework may be a stripped / wrong "
            f"build; expected headers declare AliRtcEngine."
        )
    return engine_cls


# ---------------------------------------------------------------------------
# PyObjC delegate
# ---------------------------------------------------------------------------


class _AliRtcAudioDelegate(NSObject):  # type: ignore[misc,valid-type]
    """``AliRtcEngineDelegate`` subset for v1.0 audio-only callbacks.

    Cocoa selectors are mapped to Python methods using PyObjC's
    selector-name convention: each ``:`` becomes ``_`` and the trailing
    ``:`` becomes a trailing ``_``. E.g. ``onJoinChannelResult:channel:elapsed:``
    → ``onJoinChannelResult_channel_elapsed_``.

    Every selector runs on a Cocoa / SDK worker thread. Implementations
    MUST:

    1. Copy any field they need out of the PyObjC argument into a
       primitive Python value (int / str / bytes / tuple). Holding a
       reference to the PyObjC object across threads can race the
       Cocoa autorelease pool.
    2. Dispatch the work to the asyncio loop via
       ``self._loop.call_soon_threadsafe(...)``. NEVER touch
       ``asyncio.Future`` / ``asyncio.Queue`` from this thread.

    PyObjC binds the instance via ``initWithSession_`` (see factory
    method) — ``__init__`` is not used because NSObject's life cycle
    runs through ``alloc().init...``.
    """

    # PyObjC selector definitions. The ``def`` name encodes the Obj-C
    # selector; PyObjC reads the binding via standard Python attribute
    # access.

    def initWithSession_(self, session: "MacosArtcPyObjCSession") -> "_AliRtcAudioDelegate":
        """Bind the delegate to its owning Python session.

        Cocoa initializer pattern: returns ``self`` after super().init().
        """
        self = objc.super(_AliRtcAudioDelegate, self).init()
        if self is None:
            return None
        self._session = session  # type: ignore[attr-defined]
        return self

    def onJoinChannelResult_channel_elapsed_(
        self, result: int, channel: str, elapsed: int,
    ) -> None:
        # Copy to primitives. NSString → str is automatic in PyObjC.
        session = self._session  # type: ignore[attr-defined]
        loop = session._loop
        if loop is not None:
            loop.call_soon_threadsafe(
                session._on_join_result, int(result), str(channel), int(elapsed),
            )

    def onLeaveChannelResult_(self, result: int) -> None:
        session = self._session  # type: ignore[attr-defined]
        loop = session._loop
        if loop is not None:
            loop.call_soon_threadsafe(session._on_leave_result, int(result))

    def onError_(self, error: int) -> None:
        session = self._session  # type: ignore[attr-defined]
        loop = session._loop
        if loop is not None:
            loop.call_soon_threadsafe(session._on_error, int(error))

    def onSubscribeAudioFrame_(self, audio_frame: Any) -> None:
        """Inbound per-uid audio frame.

        ``audio_frame`` is the SDK's ``AliRtcAudioFrame`` Obj-C object.
        We copy out the PCM bytes + metadata as primitives BEFORE
        dispatching to asyncio — the Obj-C object must not survive past
        this method (the SDK reuses the underlying buffer).
        """
        session = self._session  # type: ignore[attr-defined]
        loop = session._loop
        if loop is None:
            return
        # Best-effort attribute extraction: real SDK exposes data /
        # numOfSamples / samplesPerSec / channels / timestamp. Use
        # getattr with defaults so a slightly different SNAPSHOT API
        # doesn't crash the bridge — we just lose a frame.
        pcm_obj = getattr(audio_frame, "data", lambda: b"")
        pcm_bytes = bytes(pcm_obj() if callable(pcm_obj) else pcm_obj)
        sample_rate = int(_safe_call(audio_frame, "samplesPerSec", _DEFAULT_SAMPLE_RATE))
        channels = int(_safe_call(audio_frame, "channels", _DEFAULT_CHANNELS))
        timestamp_ms = int(_safe_call(audio_frame, "timestamp", 0))
        sender_uid = str(_safe_call(audio_frame, "remoteUid", "") or "")
        loop.call_soon_threadsafe(
            session._on_audio_frame,
            sender_uid, pcm_bytes, sample_rate, channels, timestamp_ms,
        )

    def onPushAudioFrameBufferFull_(self, _info: Any) -> None:
        session = self._session  # type: ignore[attr-defined]
        loop = session._loop
        if loop is not None:
            loop.call_soon_threadsafe(session._on_push_buffer_full)

    def onConnectionLost(self) -> None:
        session = self._session  # type: ignore[attr-defined]
        loop = session._loop
        if loop is not None:
            loop.call_soon_threadsafe(session._on_connection_lost)

    def onConnectionRecovery(self) -> None:
        session = self._session  # type: ignore[attr-defined]
        loop = session._loop
        if loop is not None:
            loop.call_soon_threadsafe(session._on_connection_recovery)

    def onRemoteUserOnLineNotify_(self, uid: str) -> None:
        session = self._session  # type: ignore[attr-defined]
        loop = session._loop
        if loop is not None:
            loop.call_soon_threadsafe(session._on_remote_user_online, str(uid))

    def onRemoteUserOffLineNotify_(self, uid: str) -> None:
        session = self._session  # type: ignore[attr-defined]
        loop = session._loop
        if loop is not None:
            loop.call_soon_threadsafe(session._on_remote_user_offline, str(uid))


def _safe_call(obj: Any, name: str, default: Any) -> Any:
    """Get attribute / call zero-arg method; tolerate missing names.

    Obj-C properties can show up either as zero-arg methods or as
    attributes depending on PyObjC's view of the framework metadata.
    """
    attr = getattr(obj, name, None)
    if attr is None:
        return default
    if callable(attr):
        try:
            return attr()
        except Exception:  # noqa: BLE001
            return default
    return attr


# ---------------------------------------------------------------------------
# RtcSession implementation
# ---------------------------------------------------------------------------


class MacosArtcPyObjCSession(RtcSession):
    """Real :class:`RtcSession` for the macOS dev / QA edge.

    Lifetime mirrors :class:`WindowsRtcSession`: construct cheap, defer
    framework load to :meth:`join` (so unit-tests that exercise the
    factory fallback never need a real framework on disk).

    Parameters
    ----------
    inbound_capacity:
        Inbound frame queue cap (per direction). 64 ≈ 1.3 s of 50 Hz
        audio — plenty of headroom for asyncio scheduler hiccups.
    framework_path:
        Optional explicit framework path. Highest priority; defaults to
        env var ``ISALES_MACOS_ARTC_FRAMEWORK_PATH`` and then to
        ``~/codes/vendor/AliRTCSdk_macos/AliRTCSdk.framework``.
    """

    def __init__(
        self,
        *,
        inbound_capacity: int = 64,
        framework_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self._inbound_capacity = inbound_capacity
        self._framework_path = framework_path

        self._joined = False
        self._closed = False
        self._send_sample_rate = _DEFAULT_SAMPLE_RATE
        self._send_channels = _DEFAULT_CHANNELS

        # SDK handles — populated by join(), cleared by leave().
        self._engine_cls: Any = None
        self._engine: Any = None
        self._delegate: _AliRtcAudioDelegate | None = None

        # Cross-thread bridges.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._join_future: asyncio.Future[int] | None = None
        self._frame_queue: asyncio.Queue[PcmFrame] | None = None

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
            raise RtcError("MacosArtcPyObjCSession already joined")
        if self._closed:
            raise RtcError("MacosArtcPyObjCSession is closed; create a new instance")

        self._send_sample_rate = send_sample_rate
        self._send_channels = send_channels
        self._loop = asyncio.get_running_loop()
        self._join_future = self._loop.create_future()
        self._frame_queue = asyncio.Queue(
            maxsize=self._inbound_capacity * _QUEUE_OVERSUBSCRIBE,
        )

        # Load the framework lazily so unit-tests that hit the factory
        # fallback (no SDK on disk) never touch this branch.
        self._engine_cls = _load_framework(self._framework_path)

        # createEngine returns a singleton (per AliRtc SDK convention);
        # we still record it in self._engine for symmetry with Windows.
        try:
            engine = self._engine_cls.sharedInstance()
        except AttributeError:
            # SNAPSHOT may expose a different factory. Fall back to the
            # generic createEngine: pattern documented in Aliyun's iOS
            # / macOS headers.
            engine = self._engine_cls.alloc().init()
        self._engine = engine

        # Build delegate using PyObjC's alloc/init pattern. Note this is
        # NOT ``_AliRtcAudioDelegate(session)`` — NSObject subclasses
        # cannot be instantiated via Python's normal class call.
        self._delegate = _AliRtcAudioDelegate.alloc().initWithSession_(self)

        # Wire up. setRtcEngineDelegate: is the documented method on
        # AliRtcEngine; SNAPSHOT may use setDelegate: — try both.
        _maybe_invoke(engine, "setRtcEngineDelegate_", self._delegate) or _maybe_invoke(
            engine, "setDelegate_", self._delegate,
        )

        # External audio source: tells SDK we'll be pushing PCM
        # manually rather than relying on Core Audio mic capture.
        _maybe_invoke(engine, "setExternalAudioSource_", True)

        # Kick join. v1.0 audio-only.
        try:
            _maybe_invoke(engine, "joinChannel_channel_uid_", token, channel, uid)
        except Exception as exc:  # noqa: BLE001
            await self._teardown_partial()
            raise RtcError(f"joinChannel failed synchronously: {exc}") from exc

        try:
            result = await asyncio.wait_for(
                self._join_future, timeout=_JOIN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            await self._teardown_partial()
            raise RtcError(
                "joinChannel timed out waiting for "
                "onJoinChannelResult_channel_elapsed_",
            ) from exc
        except RtcError:
            await self._teardown_partial()
            raise

        if result != 0:
            await self._teardown_partial()
            raise RtcError(
                f"onJoinChannelResult reported failure (code={result})",
            )

        self._joined = True

    async def leave(self) -> None:
        if not self._joined and self._closed:
            return  # idempotent
        try:
            if self._engine is not None:
                with contextlib.suppress(Exception):
                    _maybe_invoke(self._engine, "leaveChannel")
        finally:
            await self._teardown_partial()
            self._joined = False
            self._closed = True

    async def _teardown_partial(self) -> None:
        if self._engine is not None:
            with contextlib.suppress(Exception):
                _maybe_invoke(self._engine, "setRtcEngineDelegate_", None)
                _maybe_invoke(self._engine, "setDelegate_", None)
            with contextlib.suppress(Exception):
                # destroy() / release patterns vary across Aliyun SDK
                # versions — try both.
                if not _maybe_invoke(self._engine, "destroy"):
                    _maybe_invoke(self._engine, "release")
            self._engine = None
        self._delegate = None
        self._engine_cls = None

        if self._frame_queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._frame_queue.put_nowait(_DRAINER_SENTINEL)

    # ----- delegate dispatch (asyncio thread) -----

    def _on_join_result(self, result: int, channel: str, elapsed: int) -> None:
        logger.info(
            "macos_artc_pyobjc_join_result",
            extra={"result": result, "channel": channel, "elapsed_ms": elapsed},
        )
        fut = self._join_future
        if fut is not None and not fut.done():
            fut.set_result(result)

    def _on_leave_result(self, result: int) -> None:
        logger.info("macos_artc_pyobjc_leave_result", extra={"result": result})

    def _on_error(self, code: int) -> None:
        logger.error("macos_artc_pyobjc_error", extra={"code": code})
        fut = self._join_future
        if fut is not None and not fut.done():
            fut.set_exception(RtcError(f"ARTC error during join (code={code})"))

    def _on_audio_frame(
        self,
        sender_uid: str,
        pcm: bytes,
        sample_rate: int,
        channels: int,
        timestamp_ms: int,
    ) -> None:
        if self._frame_queue is None:
            return
        frame = PcmFrame(
            sender_uid=sender_uid,
            pcm=pcm,
            sample_rate=sample_rate or _DEFAULT_SAMPLE_RATE,
            channels=channels or _DEFAULT_CHANNELS,
            timestamp_ms=timestamp_ms,
        )
        q = self._frame_queue
        try:
            q.put_nowait(frame)
        except asyncio.QueueFull:
            # Drop oldest pending frame, keep the new one — mirrors
            # WindowsRtcSession._drain_loop (drop-oldest on overflow).
            with contextlib.suppress(asyncio.QueueEmpty):
                q.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(frame)

    def _on_push_buffer_full(self) -> None:
        logger.warning("macos_artc_pyobjc_push_buffer_full")

    def _on_connection_lost(self) -> None:
        logger.warning("macos_artc_pyobjc_connection_lost")

    def _on_connection_recovery(self) -> None:
        logger.info("macos_artc_pyobjc_connection_recovery")

    def _on_remote_user_online(self, uid: str) -> None:
        logger.info("macos_artc_pyobjc_remote_user_online", extra={"uid": uid})

    def _on_remote_user_offline(self, uid: str) -> None:
        logger.info("macos_artc_pyobjc_remote_user_offline", extra={"uid": uid})

    # ----- inbound PCM -----

    async def audio_frames(self) -> AsyncIterator[PcmFrame]:
        if not self._joined or self._frame_queue is None:
            raise RtcNotJoined(
                "audio_frames() called before join() or after leave()",
            )
        queue = self._frame_queue
        while True:
            frame = await queue.get()
            if frame is _DRAINER_SENTINEL:
                return
            yield frame

    # ----- outbound PCM -----

    async def push_audio(self, pcm: bytes, *, timestamp_ms: int) -> None:
        if not self._joined or self._engine is None:
            raise RtcNotJoined(
                "push_audio() called before join() or after leave()",
            )
        try:
            # SNAPSHOT names: pushExternalAudioFrame_ or
            # pushExternalAudioFrameRawData_. Try the documented one
            # first; fall back to the raw-data variant.
            if not _maybe_invoke(
                self._engine,
                "pushExternalAudioFrameRawData_sampleRate_channels_timestamp_",
                pcm, self._send_sample_rate, self._send_channels, timestamp_ms,
            ) and not _maybe_invoke(
                self._engine, "pushExternalAudioFrame_", pcm,
            ):
                raise RtcError(
                    "No supported pushExternalAudioFrame selector on AliRtcEngine; "
                    "SDK SNAPSHOT may have renamed the API — see RUNBOOK.",
                )
        except RtcError:
            raise
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            if "buffer" in msg and "full" in msg:
                raise RtcPushBackpressure(
                    f"pushExternalAudioFrame backpressure: {exc}",
                ) from exc
            raise RtcError(f"pushExternalAudioFrame failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _maybe_invoke(obj: Any, selector: str, *args: Any) -> bool:
    """Invoke an Obj-C selector if the object responds to it.

    Returns ``True`` when the selector was found and called (regardless
    of what it returned), ``False`` when the object does not expose it.
    Lets callers chain alternates with ``or``::

        _maybe_invoke(engine, "setRtcEngineDelegate_", d) or \
            _maybe_invoke(engine, "setDelegate_", d)

    Exceptions raised by the underlying method propagate to the caller —
    they indicate a real SDK-level failure, not "API absent".
    """
    method = getattr(obj, selector, None)
    if method is None or not callable(method):
        return False
    method(*args)
    return True


__all__ = [
    "DEFAULT_FRAMEWORK_PATH",
    "FRAMEWORK_PATH_ENV",
    "MacosArtcPyObjCSession",
]
