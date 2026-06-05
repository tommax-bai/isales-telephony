"""Edge-side :class:`RtcSession` implementation for macOS dev / QA hosts.

Spec: openspec/changes/engine-rtc-dingrtc-migration § 8.

Companion to :mod:`isales_telephony.audio_bridge.session` (the in-process
mock loopback kept for CI / unit-tests) and
:mod:`isales_telephony.audio_bridge.windows_rtc_session` (pybind11 binding,
Windows commercial path). The wire-format (:class:`PcmFrame`) is identical
across all three — the engine / :class:`AudioBridge` code paths get the
same data shape they exercise in unit tests.

Architecture
------------

The macOS DingRTC SDK ships as ``DingRTC.framework`` — a Mach-O universal
binary with Obj-C-only Headers (DingRtcEngine.h + DingRtcEngineTypes.h
+ ...). There is no Python wrapper. We bridge into Python via PyObjC:

- ``objc.loadBundle("DingRTC", bundle_path=...)`` injects every Obj-C
  class declared in the framework's headers into the calling module's
  globals (so ``DingRtcEngine``, ``DingRtcAuthInfo``,
  ``DingRtcAudioDataSample`` etc. become attributes on this module).
- DingRTC 3.x changed the API shape vs. the legacy AliRTC ARTC SDK
  pattern (which was used pre-2026-05-26 via ``macos_artc_pyobjc.py``):

  * ``DingRtcEngine`` requires ``sharedInstance:extras:`` with the
    delegate baked into construction; the old ``setRtcEngineDelegate:``
    pattern is gone.
  * ``destroy`` is a class method.
  * ``joinChannel:`` takes a ``DingRtcAuthInfo`` struct (channelId +
    userId + appId + token + gslbServer) rather than three flat
    strings.
  * ``onJoinChannelResult:channel:userId:elapsed:`` adds a ``userId:``
    slot.
  * Inbound audio is delivered via a *separate* ``DingRtcAudioFrameDelegate``
    protocol (``onPlaybackAudioFrame:`` etc.) wired through
    ``registerAudioFrameObserver:`` + ``enableAudioFrameObserver:position:``.
  * ``pushExternalAudioFrame:`` takes a ``DingRtcAudioDataSample`` Obj-C
    object (with ``void *`` ``data`` pointer); the AliRTC flat-args
    ``pushExternalAudioFrameRawData_sampleRate_channels_timestamp_`` is
    gone.
  * External audio source is configured via
    ``setExternalAudioSource:withSampleRate:channelsPerFrame:``
    (3 args, not 1) — mirrors the Linux SDK shape.

- Two ``NSObject`` subclasses play the delegate roles:

  * ``_DingRtcEngineDelegate`` implements the channel / connection /
    error / remote-user lifecycle selectors of ``DingRtcEngineDelegate``.
  * ``_DingRtcAudioFrameDelegate`` implements the audio-frame ingestion
    side of ``DingRtcAudioFrameDelegate``.

- Delegate callbacks run on Cocoa / random SDK threads; the bridge
  marshals every event back to the asyncio loop via
  ``loop.call_soon_threadsafe(...)``. Closures capture *values only*
  (ints / strs / bytes / Python tuples) — never PyObjC objects — to
  avoid cross-thread Obj-C autorelease-pool races.

The PyObjC + framework dependencies are optional (extras
``[macos-dingrtc]``; legacy ``[macos-artc]`` alias still resolves until
the next minor release). Importing this module without them raises
:class:`ImportError` with an explicit install hint.
:func:`audio_bridge.get_default_rtc_session_class` catches that
``ImportError`` on macOS and falls back to the mock loopback session
with a WARN log.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
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
# PyObjC import (extras `[macos-dingrtc]`)
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
            "Install with: pip install -e '.[macos-dingrtc]' (macOS dev / "
            "QA only — Windows commercial path uses pybind11)."
        ) from exc
    raise


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_SAMPLE_RATE = 16000
_DEFAULT_CHANNELS = 1

#: Default framework search path. Overridable via env var
#: ``ISALES_MACOS_DINGRTC_FRAMEWORK_PATH`` (lower priority than
#: constructor ``framework_path`` argument). Legacy
#: ``ISALES_MACOS_ARTC_FRAMEWORK_PATH`` is honoured one more release
#: with a WARN log.
DEFAULT_FRAMEWORK_PATH = (
    Path.home() / "codes" / "vendor" / "DingRTC_macOS_SDK_3_9_0" / "DingRTC.framework"
)

#: Env var override (primary).
FRAMEWORK_PATH_ENV = "ISALES_MACOS_DINGRTC_FRAMEWORK_PATH"

#: Legacy env var carried over from the AliRTC era; emits a WARN log
#: when consumed. Remove in the release after dingrtc-migration archive.
LEGACY_FRAMEWORK_PATH_ENV = "ISALES_MACOS_ARTC_FRAMEWORK_PATH"

#: Join timeout (mirrors WindowsRtcSession's 5 s).
_JOIN_TIMEOUT_SECONDS = 5.0

#: Default GSLB endpoint. DingRTC 3.x bakes this URL into ``DingRtcAuthInfo``;
#: keeping it as a class-level constant lets tests / non-prod environments
#: override without monkey-patching the engine. Matches the Linux binding's
#: ``DingRtcSession`` default in isales-engine.
DEFAULT_GSLB_SERVER = "https://gslb.dingrtc.com"

#: ``DingRtcAudioObservePositionPlayback = 1 << 3`` per
#: ``DingRtcEngineTypes.h``. Mirror of ``dingrtc_pywrap.POSITION_PLAYBACK``
#: in isales-engine. Stable across DingRTC 3.x patches.
_AUDIO_OBSERVE_PLAYBACK = 1 << 3

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
    """Resolve the framework path with priority arg > env > legacy env > default."""
    if framework_path is not None:
        return Path(framework_path)
    env = os.environ.get(FRAMEWORK_PATH_ENV)
    if env:
        return Path(env)
    legacy = os.environ.get(LEGACY_FRAMEWORK_PATH_ENV)
    if legacy:
        logger.warning(
            "macos_dingrtc_legacy_env_in_use",
            extra={
                "env": LEGACY_FRAMEWORK_PATH_ENV,
                "value": legacy,
                "hint": (
                    f"Rename to {FRAMEWORK_PATH_ENV}; legacy alias removed "
                    "after dingrtc-migration archive."
                ),
            },
        )
        return Path(legacy)
    return DEFAULT_FRAMEWORK_PATH


def _load_framework(framework_path: str | os.PathLike[str] | None) -> dict[str, Any]:
    """Load ``DingRTC.framework`` and return key Obj-C classes.

    Strategy: ``ctypes.CDLL(..., mode=RTLD_GLOBAL)`` the framework
    binary (and its sibling ``libffmpeg.dylib`` dep) so Obj-C class
    constructors register with the runtime, then look up each class
    via ``objc.lookUpClass``. We deliberately avoid
    ``objc.loadBundle`` — DingRTC.framework's ``@rpath`` deps don't
    resolve through NSBundle's loader without setting DYLD env, and
    requiring callers to set DYLD before launching python is a sharp
    edge we'd rather not impose. Pre-loading dependencies through
    ctypes is the documented PyObjC workaround for `@rpath`-burdened
    third-party frameworks (PyObjC docs §"Loading frameworks").

    Returns a dict ``{name: class}`` so callers can grab
    ``DingRtcEngine`` / ``DingRtcAuthInfo`` / ``DingRtcAudioDataSample``
    without polluting module globals at arbitrary points in the test
    lifecycle.

    Raises:
        RtcError: if the path does not exist, is not a framework bundle,
            ``ctypes.CDLL`` rejects it (wrong arch / missing dep), or
            required Obj-C classes cannot be looked up afterwards.
    """
    resolved = _resolve_framework_path(framework_path)
    if not resolved.exists():
        raise RtcError(
            f"DingRTC framework not found at {resolved!s}. "
            f"Download DingRTC_macOS_SDK_3_9_0.zip "
            f"(https://dingrtc.oss-cn-zhangjiakou.aliyuncs.com/sdk/mac/3.9.0/"
            f"DingRTC_macOS_SDK_3_9_0.zip) and unzip to "
            f"~/codes/vendor/DingRTC_macOS_SDK_3_9_0/, or set "
            f"{FRAMEWORK_PATH_ENV} to override."
        )
    if not resolved.is_dir() or resolved.suffix != ".framework":
        raise RtcError(
            f"Path {resolved!s} is not a .framework bundle directory. "
            f"Expected an unzipped DingRTC.framework directory."
        )
    binary = resolved / resolved.stem
    if not binary.exists():
        raise RtcError(
            f"Framework binary missing: {binary!s}. Re-extract the SDK zip; "
            f"`file {binary}` should report a Mach-O universal binary."
        )

    # Pre-load the vendor's sibling ffmpeg dep (the framework's
    # ``otool -L`` lists ``@rpath/libffmpeg.dylib``); without it the
    # subsequent CDLL on the framework binary fails to resolve symbols.
    ffmpeg_dep = resolved.parent / "libffmpeg.dylib"
    if ffmpeg_dep.exists():
        try:
            ctypes.CDLL(str(ffmpeg_dep), mode=ctypes.RTLD_GLOBAL)
        except OSError as exc:
            raise RtcError(
                f"ctypes.CDLL(libffmpeg.dylib) failed: {exc}",
            ) from exc

    try:
        ctypes.CDLL(str(binary), mode=ctypes.RTLD_GLOBAL)
    except OSError as exc:
        raise RtcError(
            f"ctypes.CDLL failed for {binary!s}: {exc}. "
            f"Verify the binary is a universal Mach-O (run "
            f"`file {binary}`) and the host architecture matches. "
            f"If `@rpath/libfoo.dylib not found`, that dep needs to "
            f"live in {resolved.parent!s}/ next to the framework.",
        ) from exc

    # After ctypes preload, NSBundle can resolve the framework's
    # ``@rpath`` deps. Run ``objc.loadBundle`` so the Obj-C runtime
    # registers DingRTC's formal protocols (e.g.
    # ``DingRtcEngineDelegate``). Without protocol registration,
    # PyObjC subclasses that try to claim conformance via
    # ``protocols=[...]`` raise ``ProtocolError``, and the SDK
    # segfaults when it invokes selectors on a non-conformant
    # delegate.
    with contextlib.suppress(Exception):
        objc.loadBundle(
            "DingRTC",
            module_globals=globals(),
            bundle_path=str(resolved),
        )

    required = ("DingRtcEngine", "DingRtcAuthInfo", "DingRtcAudioDataSample")
    classes: dict[str, Any] = {}
    for name in required:
        try:
            cls = objc.lookUpClass(name)
        except Exception as exc:  # noqa: BLE001 — PyObjC raises a broad family
            raise RtcError(
                f"objc.lookUpClass({name!r}) failed after loading "
                f"{resolved!s}: {exc}. The framework may be stripped / wrong "
                f"build; DingRTC 3.9.0 declares {required}.",
            ) from exc
        classes[name] = cls

    # Register the formal DingRtcEngineDelegate protocol on our
    # delegate subclass once. PyObjC binds the protocol metadata at
    # class creation time, so we re-create the class with the protocol
    # if it hasn't been claimed yet. Idempotent on repeat loads.
    _bind_engine_delegate_protocol()
    return classes


_ENGINE_DELEGATE_PROTOCOL_BOUND = False


def _bind_engine_delegate_protocol() -> None:
    """Late-bind ``DingRtcEngineDelegate`` onto :class:`_DingRtcEngineDelegate`.

    Idempotent. Lookup is best-effort: if the formal protocol metadata
    is absent (e.g. older SDK patch), we leave the subclass as-is and
    let runtime behaviour surface any conformance issue.
    """
    global _ENGINE_DELEGATE_PROTOCOL_BOUND
    if _ENGINE_DELEGATE_PROTOCOL_BOUND:
        return
    try:
        proto = objc.protocolNamed("DingRtcEngineDelegate")
    except Exception:  # noqa: BLE001
        return
    # PyObjC exposes ``addObservedSelector`` / ``conformsToProtocol_``
    # but the right way to attach a formal protocol post-class-creation
    # is via the ``__pyobjc_protocols__`` attribute. Mutate cautiously.
    existing = list(getattr(_DingRtcEngineDelegate, "__pyobjc_protocols__", []))
    if proto not in existing:
        existing.append(proto)
        _DingRtcEngineDelegate.__pyobjc_protocols__ = existing  # type: ignore[attr-defined]
    _ENGINE_DELEGATE_PROTOCOL_BOUND = True


# ---------------------------------------------------------------------------
# PyObjC delegates
# ---------------------------------------------------------------------------
#
# Why every delegate selector below is wrapped in ``objc.selector(...,
# signature=b'...')``: PyObjC's default auto-detection treats every
# Python argument of an NSObject method as ``@`` (id). That is fine for
# selectors called from Python, but the SDK invokes our delegate from
# Objective-C using the C calling convention for the declared
# signature in ``DingRtcEngine.h`` — primitive ``int`` / NSInteger
# arguments live in integer registers, not as boxed objects. Without
# an explicit signature PyObjC tries to autorelease / read those
# integer-register values as ObjC pointers → instant SIGSEGV the moment
# the SDK fires any callback with non-``@`` args.
#
# Signature reference (vendor header
# ``DingRTC.framework/Headers/DingRtcEngine.h``):
#
# - ``onJoinChannelResult:channel:userId:elapsed:`` — ``(int, NSString *,
#   NSString *, int)`` → ``v@:i@@i``
# - ``onLeaveChannelResult:stats:`` — ``(int, DingRtcStats *)`` → ``v@:i@``
# - ``onOccurError:message:`` — ``(int, NSString *)`` → ``v@:i@``
# - ``onConnectionStatusChanged:reason:`` — both args are
#   ``NS_ENUM(NSInteger, ...)`` (DingRtcConnectionStatus /
#   DingRtcConnectionStatusChangeReason) → ``v@:qq``
# - ``onRemoteUserOnLineNotify:elapsed:`` — ``(NSString *, int)`` → ``v@:@i``
# - ``onRemoteUserOffLineNotify:offlineReason:`` — ``(NSString *,
#   DingRtcUserOfflineReason)`` (NSInteger enum) → ``v@:@q``
# - ``onPlaybackAudioFrame:`` — ``(DingRtcAudioDataSample *)`` → ``v@:@``
#
# Type-encoding cheat sheet (subset PyObjC uses):
#   v=void  @=id  :=SEL  i=int (32-bit)  q=long long (64-bit, NSInteger
#   on 64-bit macOS)  Q=unsigned long long (NSUInteger).


def _typed_selector(signature: bytes):
    """Wrap a Python method with an explicit PyObjC ABI signature.

    Returns a decorator that calls ``objc.selector(fn,
    signature=signature)``. The decorated attribute remains usable as a
    regular Python method (PyObjC's ``selector`` is dual-purpose).

    Use this on every NSObject subclass method that is invoked *from*
    Objective-C (delegate callbacks, target-action handlers, etc.) and
    takes any argument that is not ``id``. Methods called only from
    Python (e.g. ``initWith…`` factory selectors invoked via PyObjC's
    bridge) do not need this because PyObjC handles the marshalling
    in that direction itself.
    """

    def deco(fn):  # noqa: ANN001 — internal helper
        return objc.selector(fn, signature=signature)

    return deco


class _DingRtcEngineDelegate(NSObject):  # type: ignore[misc,valid-type]
    """``DingRtcEngineDelegate`` subset for v1.0 audio-only callbacks.

    Cocoa selectors are mapped to Python methods using PyObjC's
    selector-name convention: each ``:`` becomes ``_`` and the trailing
    ``:`` becomes a trailing ``_``. E.g.
    ``onJoinChannelResult:channel:userId:elapsed:`` →
    ``onJoinChannelResult_channel_userId_elapsed_``.

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

    def initWithSession_(
        self, session: "MacosDingRtcPyObjCSession",
    ) -> "_DingRtcEngineDelegate":
        """Bind the delegate to its owning Python session."""
        self = objc.super(_DingRtcEngineDelegate, self).init()
        if self is None:
            return None
        self._session = session  # type: ignore[attr-defined]
        return self

    # ---- channel lifecycle ------------------------------------------------

    @_typed_selector(b"v@:i@@i")
    def onJoinChannelResult_channel_userId_elapsed_(
        self, result, channel, user_id, elapsed,
    ):
        # DingRTC 3.x adds the userId slot vs. the AliRTC 3-arg variant.
        session = self._session  # type: ignore[attr-defined]
        loop = session._loop
        if loop is not None:
            loop.call_soon_threadsafe(
                session._on_join_result,
                int(result), str(channel), str(user_id), int(elapsed),
            )

    @_typed_selector(b"v@:i@")
    def onLeaveChannelResult_stats_(self, result, _stats):
        session = self._session  # type: ignore[attr-defined]
        loop = session._loop
        if loop is not None:
            loop.call_soon_threadsafe(session._on_leave_result, int(result))

    @_typed_selector(b"v@:i@")
    def onOccurError_message_(self, error, message):
        session = self._session  # type: ignore[attr-defined]
        loop = session._loop
        if loop is not None:
            loop.call_soon_threadsafe(
                session._on_error, int(error), str(message),
            )

    @_typed_selector(b"v@:qq")
    def onConnectionStatusChanged_reason_(self, status, reason):
        session = self._session  # type: ignore[attr-defined]
        loop = session._loop
        if loop is not None:
            loop.call_soon_threadsafe(
                session._on_connection_status_changed,
                int(status), int(reason),
            )

    # ---- remote-user notifications ----------------------------------------

    @_typed_selector(b"v@:@i")
    def onRemoteUserOnLineNotify_elapsed_(self, uid, elapsed):
        session = self._session  # type: ignore[attr-defined]
        loop = session._loop
        if loop is not None:
            loop.call_soon_threadsafe(
                session._on_remote_user_online, str(uid), int(elapsed),
            )

    @_typed_selector(b"v@:@q")
    def onRemoteUserOffLineNotify_offlineReason_(self, uid, reason):
        session = self._session  # type: ignore[attr-defined]
        loop = session._loop
        if loop is not None:
            loop.call_soon_threadsafe(
                session._on_remote_user_offline, str(uid), int(reason),
            )


class _DingRtcAudioFrameDelegate(NSObject):  # type: ignore[misc,valid-type]
    """``DingRtcAudioFrameDelegate`` subset.

    Only ``onPlaybackAudioFrame:`` (the mixed downstream PCM) is consumed
    for v1.0; that matches the Linux SDK's ``POSITION_PLAYBACK`` wiring
    in isales-engine. ``onRemoteUserAudioFrame:frame:`` is logged but
    not delivered to the bridge — the mixer already includes remote
    audio, and per-uid demux is not needed for the single-call form
    factor.
    """

    def initWithSession_(
        self, session: "MacosDingRtcPyObjCSession",
    ) -> "_DingRtcAudioFrameDelegate":
        self = objc.super(_DingRtcAudioFrameDelegate, self).init()
        if self is None:
            return None
        self._session = session  # type: ignore[attr-defined]
        return self

    @_typed_selector(b"v@:@")
    def onPlaybackAudioFrame_(self, audio_frame):
        """Mixed downstream PCM (post-merge across all remote uids).

        ``audio_frame`` is a ``DingRtcAudioDataSample`` Obj-C object.
        We copy out the PCM bytes + metadata as primitives BEFORE
        dispatching to asyncio — the Obj-C object must not survive past
        this method (the SDK reuses the underlying buffer).
        """
        session = self._session  # type: ignore[attr-defined]
        loop = session._loop
        if loop is None:
            return

        # DingRtcAudioDataSample has a ``void *`` data field + sample
        # counts. Read via the documented property names; fall back to
        # safe defaults so a slightly different SDK patch level cannot
        # crash the delegate.
        num_samples = int(_safe_call(audio_frame, "numOfSamples", 0))
        bytes_per_sample = int(_safe_call(audio_frame, "bytesPerSample", 2))
        channels = int(_safe_call(audio_frame, "numOfChannels", _DEFAULT_CHANNELS))
        sample_rate = int(_safe_call(audio_frame, "sampleRate", _DEFAULT_SAMPLE_RATE))
        timestamp_ms = int(_safe_call(audio_frame, "timestamp", 0))
        data_ptr = _safe_call(audio_frame, "data", 0)

        nbytes = max(0, num_samples * bytes_per_sample * channels)
        if data_ptr and nbytes:
            try:
                pcm_bytes = bytes(
                    (ctypes.c_ubyte * nbytes).from_address(int(data_ptr))
                )
            except Exception:  # noqa: BLE001
                pcm_bytes = b""
        else:
            pcm_bytes = b""

        # 端输出 timing diag: per-second peak of the AI playback PCM reaching
        # the edge (≈ when it renders to the mac speaker). Cheap 16-point peak
        # (no audioop — removed in Py 3.14); aggregated per second.
        if pcm_bytes and bytes_per_sample == 2:
            import struct as _st  # noqa: PLC0415
            import time as _t  # noqa: PLC0415
            _n = len(pcm_bytes) // 2
            _peak = 0
            if _n:
                _stride = max(1, _n // 16)
                for _i in range(0, _n, _stride):
                    _v = _st.unpack_from("<h", pcm_bytes, _i * 2)[0]
                    if abs(_v) > _peak:
                        _peak = abs(_v)
            _now = _t.monotonic()
            self._pb_peak = max(getattr(self, "_pb_peak", 0), _peak)
            self._pb_cnt = getattr(self, "_pb_cnt", 0) + 1
            if _now - getattr(self, "_pb_last", 0.0) >= 1.0:
                logger.info(
                    "dev_playback_recv_1s frames=%s peak=%s sr=%s ch=%s ts_ms=%s",
                    self._pb_cnt, self._pb_peak, sample_rate, channels, timestamp_ms,
                )
                self._pb_last = _now
                self._pb_peak = 0
                self._pb_cnt = 0

        loop.call_soon_threadsafe(
            session._on_audio_frame,
            "", pcm_bytes, sample_rate, channels, timestamp_ms,
        )


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


def _maybe_invoke(obj: Any, selector: str, *args: Any) -> bool:
    """Invoke an Obj-C selector if the object responds to it.

    Returns ``True`` when the selector was found and called (regardless
    of what it returned), ``False`` when the object does not expose it.
    Lets callers chain alternates with ``or``.

    Exceptions raised by the underlying method propagate to the caller —
    they indicate a real SDK-level failure, not "API absent".
    """
    method = getattr(obj, selector, None)
    if method is None or not callable(method):
        return False
    method(*args)
    return True


# ---------------------------------------------------------------------------
# RtcSession implementation
# ---------------------------------------------------------------------------


class MacosDingRtcPyObjCSession(RtcSession):
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
        env var ``ISALES_MACOS_DINGRTC_FRAMEWORK_PATH`` (or legacy
        ``ISALES_MACOS_ARTC_FRAMEWORK_PATH``) and then to
        ``~/codes/vendor/DingRTC_macOS_SDK_3_9_0/DingRTC.framework``.
    app_id:
        DingRTC AppId. v1.0 reads from ``ISALES_RTC_APP_ID`` env in the
        ``production`` classmethod; constructor accepts it directly so
        tests can pass a stub value.
    gslb_server:
        DingRTC GSLB endpoint; defaults to the production URL.
    """

    def __init__(
        self,
        *,
        inbound_capacity: int = 64,
        framework_path: str | os.PathLike[str] | None = None,
        app_id: str = "",
        gslb_server: str = DEFAULT_GSLB_SERVER,
    ) -> None:
        self._inbound_capacity = inbound_capacity
        self._framework_path = framework_path
        self._app_id = app_id
        self._gslb_server = gslb_server

        self._joined = False
        self._closed = False
        self._send_sample_rate = _DEFAULT_SAMPLE_RATE
        self._send_channels = _DEFAULT_CHANNELS

        # SDK handles — populated by join(), cleared by leave().
        self._classes: dict[str, Any] = {}
        self._engine: Any = None
        self._engine_delegate: _DingRtcEngineDelegate | None = None
        self._audio_delegate: _DingRtcAudioFrameDelegate | None = None

        # Cross-thread bridges.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._join_future: asyncio.Future[int] | None = None
        self._frame_queue: asyncio.Queue[PcmFrame] | None = None

        # Outbound buffer kept alive across the SDK push call so the
        # ``void *`` pointer we stash into DingRtcAudioDataSample remains
        # valid. The SDK copies internally, so reuse on next push is safe.
        self._push_buf: ctypes.Array[ctypes.c_ubyte] | None = None
        self._push_sample: Any = None

    # ----- factory ---------------------------------------------------------

    @classmethod
    def production(
        cls,
        *,
        app_id: str | None = None,
        framework_path: str | os.PathLike[str] | None = None,
    ) -> "MacosDingRtcPyObjCSession":
        """Return a session wired to production DingRTC credentials.

        Mirrors ``isales_engine.transport.dingrtc.DingRtcSession.production``:
        AppId from ``ISALES_RTC_APP_ID`` env unless explicitly passed;
        the caller still mints + supplies the token via :meth:`join`.
        """
        resolved_app_id = app_id if app_id is not None else os.environ.get("ISALES_RTC_APP_ID", "")
        if not resolved_app_id:
            raise RtcError(
                "MacosDingRtcPyObjCSession.production: ISALES_RTC_APP_ID "
                "env not set and no app_id passed.",
            )
        return cls(app_id=resolved_app_id, framework_path=framework_path)

    # ----- lifecycle -------------------------------------------------------

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
            raise RtcError("MacosDingRtcPyObjCSession already joined")
        if self._closed:
            raise RtcError(
                "MacosDingRtcPyObjCSession is closed; create a new instance",
            )
        if not self._app_id:
            raise RtcError(
                "MacosDingRtcPyObjCSession: app_id not set on instance; "
                "use MacosDingRtcPyObjCSession.production() or pass app_id=...",
            )

        self._send_sample_rate = send_sample_rate
        self._send_channels = send_channels
        self._loop = asyncio.get_running_loop()
        self._join_future = self._loop.create_future()
        self._frame_queue = asyncio.Queue(
            maxsize=self._inbound_capacity * _QUEUE_OVERSUBSCRIBE,
        )

        # Load the framework lazily so unit-tests that hit the factory
        # fallback (no SDK on disk) never touch this branch.
        self._classes = _load_framework(self._framework_path)
        engine_cls = self._classes["DingRtcEngine"]
        auth_cls = self._classes["DingRtcAuthInfo"]

        # DingRTC 3.x bakes the delegate into construction via
        # ``sharedInstance:extras:``. Build the engine delegate first.
        self._engine_delegate = _DingRtcEngineDelegate.alloc().initWithSession_(self)
        try:
            engine = engine_cls.sharedInstance_extras_(self._engine_delegate, "")
        except AttributeError as exc:
            raise RtcError(
                "DingRtcEngine.sharedInstance:extras: not present on the "
                "loaded framework — wrong SDK version? Expected 3.9.x.",
            ) from exc
        self._engine = engine

        # setAudioDenoise removed 2026-06-03: empirical 3-mode A/B/A test
        # (OFF / DSP mode=1 / Enhance mode=2 with Gatekeeper-approved
        # hbal_se.dylib) — engine inbound RMS baseline ~600-1700 in all 3
        # modes (within env-noise variance). SDK accepts rc=True but
        # external-audio-source mode (set_external_audio_source + push_
        # external_audio) bypasses SDK internal NS pipeline. Conclusion:
        # SDK denoise only processes its own mic-capture flow; iSales
        # pushes raw PCM directly so we must do NS application-side or
        # change interruption signal (planned: barge-in ASR-text-length
        # threshold instead of VAD-energy).

        # Register the audio-frame delegate (separate protocol) +
        # enable the playback observe position (mixed downstream PCM).
        self._audio_delegate = (
            _DingRtcAudioFrameDelegate.alloc().initWithSession_(self)
        )
        try:
            _maybe_invoke(engine, "registerAudioFrameObserver_", self._audio_delegate)
            _maybe_invoke(
                engine,
                "enableAudioFrameObserver_position_",
                True, _AUDIO_OBSERVE_PLAYBACK,
            )
        except Exception as exc:  # noqa: BLE001
            await self._teardown_partial()
            raise RtcError(
                f"registerAudioFrameObserver / enableAudioFrameObserver "
                f"failed: {exc}",
            ) from exc

        # External audio source: tells SDK we'll be pushing PCM
        # manually rather than relying on Core Audio mic capture.
        try:
            _maybe_invoke(
                engine,
                "setExternalAudioSource_withSampleRate_channelsPerFrame_",
                True, send_sample_rate, send_channels,
            )
        except Exception as exc:  # noqa: BLE001
            await self._teardown_partial()
            raise RtcError(
                f"setExternalAudioSource failed: {exc}",
            ) from exc

        # DingRTC default behavior: local audio stream NOT published to the
        # channel after setExternalAudioSource(true). pushExternalAudioFrame
        # then queues PCM into the SDK's internal buffer but the channel
        # peer never receives it (engine-side inbound RMS=0 evidence,
        # call 21+, mac mic_capture_pump pushing real audio with RMS up
        # to 2700 but ECS engine sees silence). Explicit publish needed
        # to wire push_audio output onto the channel.
        try:
            _maybe_invoke(engine, "publishLocalAudioStream_", True)
        except Exception as exc:  # noqa: BLE001
            # Non-fatal: log + continue. If the SDK rejects the call we
            # still get the existing wire (mic upstream broken but
            # downstream + RTC join intact).
            logger.warning(
                "macos_dingrtc_publish_local_audio_stream_failed: %s", exc,
            )

        # Build DingRtcAuthInfo per the 3.x API shape.
        auth = auth_cls.alloc().init()
        _maybe_invoke(auth, "setChannelId_", channel)
        _maybe_invoke(auth, "setUserId_", uid)
        _maybe_invoke(auth, "setAppId_", self._app_id)
        _maybe_invoke(auth, "setToken_", token)
        _maybe_invoke(auth, "setGslbServer_", self._gslb_server)

        # Kick join. Pass nil block and rely on the delegate's
        # onJoinChannelResult:channel:userId:elapsed: for completion —
        # blocks-as-Python-callables across PyObjC is finicky and the
        # delegate path is what the Linux / Windows bindings use too.
        try:
            _maybe_invoke(
                engine,
                "joinChannel_name_onResultWithUserId_",
                auth, uid, None,
            )
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
                "onJoinChannelResult:channel:userId:elapsed:",
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
        engine_cls = self._classes.get("DingRtcEngine") if self._classes else None

        if self._engine is not None:
            # DingRTC 3.x ``destroy`` is a class method (``+ destroy``).
            # The instance-method ``release`` fallback is kept for any
            # older 3.x patch that exposed both.
            with contextlib.suppress(Exception):
                if engine_cls is not None and hasattr(engine_cls, "destroy"):
                    engine_cls.destroy()
                elif not _maybe_invoke(self._engine, "destroy"):
                    _maybe_invoke(self._engine, "release")
            self._engine = None

        self._engine_delegate = None
        self._audio_delegate = None
        self._classes = {}
        self._push_buf = None
        self._push_sample = None

        if self._frame_queue is not None:
            with contextlib.suppress(asyncio.QueueFull):
                self._frame_queue.put_nowait(_DRAINER_SENTINEL)

    # ----- delegate dispatch (asyncio thread) ------------------------------

    def _on_join_result(
        self, result: int, channel: str, user_id: str, elapsed: int,
    ) -> None:
        logger.info(
            "macos_dingrtc_join_result",
            extra={
                "result": result, "channel": channel, "user_id": user_id,
                "elapsed_ms": elapsed,
            },
        )
        fut = self._join_future
        if fut is not None and not fut.done():
            fut.set_result(result)

    def _on_leave_result(self, result: int) -> None:
        logger.info("macos_dingrtc_leave_result", extra={"result": result})

    def _on_error(self, code: int, message: str) -> None:
        logger.error(
            "macos_dingrtc_error",
            extra={"code": code, "message": message},
        )
        fut = self._join_future
        if fut is not None and not fut.done():
            fut.set_exception(
                RtcError(f"DingRTC error during join (code={code}, msg={message!r})"),
            )

    def _on_connection_status_changed(self, status: int, reason: int) -> None:
        logger.info(
            "macos_dingrtc_connection_status",
            extra={"status": status, "reason": reason},
        )

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

    def _on_remote_user_online(self, uid: str, elapsed: int) -> None:
        logger.info(
            "macos_dingrtc_remote_user_online",
            extra={"uid": uid, "elapsed_ms": elapsed},
        )

    def _on_remote_user_offline(self, uid: str, reason: int) -> None:
        logger.info(
            "macos_dingrtc_remote_user_offline",
            extra={"uid": uid, "reason": reason},
        )

    # ----- inbound PCM -----------------------------------------------------

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

    # ----- outbound PCM ----------------------------------------------------

    async def push_audio(self, pcm: bytes, *, timestamp_ms: int) -> None:
        if not self._joined or self._engine is None:
            raise RtcNotJoined(
                "push_audio() called before join() or after leave()",
            )
        if self._send_sample_rate <= 0 or self._send_channels <= 0:
            raise RtcNotJoined(
                "push_audio() before join() (rate/channels unset)",
            )

        sample_cls = self._classes.get("DingRtcAudioDataSample")
        if sample_cls is None:
            raise RtcError(
                "DingRtcAudioDataSample class not loaded; framework "
                "loader didn't expose the expected types — re-extract "
                "the SDK zip and confirm DingRtcEngineTypes.h is present.",
            )

        # Build a ctypes buffer with the PCM payload + stash it on the
        # session so the ``void *`` we hand to DingRtcAudioDataSample
        # remains valid for the duration of the SDK call. The SDK copies
        # internally on push (vendor doc confirms); reuse on next push
        # is safe.
        nbytes = len(pcm)
        bytes_per_sample = 2
        channels = self._send_channels
        if nbytes == 0 or nbytes % (bytes_per_sample * channels) != 0:
            raise RtcError(
                f"push_audio: pcm length {nbytes} not a multiple of "
                f"bytes_per_sample*channels={bytes_per_sample * channels}",
            )
        num_samples = nbytes // (bytes_per_sample * channels)
        buf = (ctypes.c_ubyte * nbytes)(*pcm)
        self._push_buf = buf  # keep alive across the SDK call

        sample = sample_cls.alloc().init()
        _maybe_invoke(sample, "setData_", ctypes.addressof(buf))
        _maybe_invoke(sample, "setNumOfSamples_", num_samples)
        _maybe_invoke(sample, "setBytesPerSample_", bytes_per_sample)
        _maybe_invoke(sample, "setNumOfChannels_", channels)
        _maybe_invoke(sample, "setSampleRate_", self._send_sample_rate)
        # DingRtcAudioDataSample (3.9.0) does NOT expose a timestamp
        # property; ``timestamp_ms`` is recorded only via push-side
        # logging. The SDK derives timestamps internally from the
        # capture clock + sampleRate.
        _ = timestamp_ms
        self._push_sample = sample  # also keep alive; PyObjC may release

        try:
            if not _maybe_invoke(self._engine, "pushExternalAudioFrame_", sample):
                raise RtcError(
                    "DingRtcEngine.pushExternalAudioFrame: selector not "
                    "found on engine — SDK version mismatch.",
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


__all__ = [
    "DEFAULT_FRAMEWORK_PATH",
    "DEFAULT_GSLB_SERVER",
    "FRAMEWORK_PATH_ENV",
    "LEGACY_FRAMEWORK_PATH_ENV",
    "MacosDingRtcPyObjCSession",
]
