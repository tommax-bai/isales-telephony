"""Edge-side audio-bridge: glues modem PCM with the cloud RTC channel.

Spec: arch-cloud-edge-split / device-hardware § Requirement: audio-bridge
组件 + 云端 engine 的 ARTC SDK 接入 (the edge-side counterpart).

This package owns three pieces of mechanics:

- :class:`PcmRingBuffer` — bounded async ring buffer between modem-
  controller and audio-bridge (one per direction). Drop-oldest on
  overflow rather than blocking the producer.
- :class:`Resampler` — int16 mono PCM rate conversion via
  ``scipy.signal.resample_poly`` (audioop is unavailable on Python
  3.13+). Stateful per direction.
- :class:`MacosRtcSession` — :class:`RtcSession` (from isales-common)
  implementation for the macOS edge. v1.0 strategy: in-memory loopback
  + simulated backpressure (the macOS ARTC SDK ships as an Obj-C-only
  Cocoa framework with no Python wrapper; bridging via PyObjC is out
  of scope for the QA macOS edge form factor — Windows D1 is the
  commercial path with a real RTC backend).

And one orchestration class:

- :class:`AudioBridge` — owns two asyncio tasks bridging
  ``modem_upstream ring → resample 8→16 → rtc.push_audio`` and
  ``rtc.audio_frames (filter peer uid) → resample 16→8 →
  modem_downstream ring``. Honours :class:`RtcPushBackpressure` by
  pausing the upstream pump until drain.

The bridge is per-call: ``AudioBridge.join(channel, token, uid,
peer_uid)`` opens an RTC session and starts pumps; ``leave()`` tears
both down. Multiple concurrent calls = multiple bridge instances.
"""

from __future__ import annotations

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from isales_telephony.audio_bridge.bridge import AudioBridge
from isales_telephony.audio_bridge.resampler import Resampler
from isales_telephony.audio_bridge.ring_buffer import PcmRingBuffer
from isales_telephony.audio_bridge.session import (
    MacosRtcSession,
    MacosRtcSessionConfig,
)

if TYPE_CHECKING:
    from isales_common.audio.rtc import RtcSession


logger = logging.getLogger(__name__)


def get_default_rtc_session_factory(
    *, app_id: str | None = None,
) -> Callable[[], "RtcSession"]:
    """Return a zero-arg factory that constructs a platform-appropriate
    :class:`RtcSession`.

    The factory is invoked **without arguments** by ``EdgeOrchestrator``
    (``self._rtc_factory()`` per call), so any session-construction kwargs
    (such as Windows DingRTC ``app_id``) must be partial-bound here.

    - ``win32`` → :class:`WindowsDingRtcSession` partial-bound to ``app_id``
      (DingRTC via pybind11 from ``deploy/edge/windows/pybind/dingrtc_pywrap/``).
      ``app_id`` is REQUIRED — DingRTC's RtcEngineAuthInfo bundles AppId at
      construction time, not per-join.
    - ``darwin`` → :class:`MacosArtcPyObjCSession` (PyObjC bridge, dev / QA)
      when ``[macos-dingrtc]`` extras + ``DingRTC.framework`` are present.
      Falls back to :class:`MacosRtcSession` mock loopback (WARN log + install
      hint). ``app_id`` is unused on macOS — the Mac PyObjC class manages
      its own DingRTC framework binding kwargs internally.
    - other → ``NotImplementedError``.

    Real bindings (Windows pybind11, macOS PyObjC) import lazily so the
    rest of the package imports cleanly on every platform for tests.
    """
    if sys.platform == "win32":
        if not app_id:
            raise ValueError(
                "Windows edge RtcSession (DingRTC) requires app_id — set "
                "ISALES_RTC_APP_ID env in the edge process before "
                "constructing the factory."
            )
        from functools import partial
        from isales_telephony.audio_bridge.windows_dingrtc_session import (
            WindowsDingRtcSession,
        )
        return partial(WindowsDingRtcSession, app_id=app_id)
    if sys.platform == "darwin":
        try:
            from isales_telephony.audio_bridge.macos_artc_pyobjc import (
                MacosArtcPyObjCSession,
            )
            return MacosArtcPyObjCSession
        except ImportError as exc:
            logger.warning(
                "macos_artc_pyobjc_unavailable_fallback_to_mock",
                extra={
                    "detail": str(exc),
                    "hint": (
                        "pip install -e '.[macos-dingrtc]' and unzip "
                        "DingRTC_macOS_SDK_3_9_0 to ~/codes/vendor/ "
                        "to enable real DingRTC on macOS dev / QA"
                    ),
                },
            )
            return MacosRtcSession
    raise NotImplementedError(
        f"No edge RtcSession implementation for platform {sys.platform!r}. "
        "macOS走 PyObjC binding (fallback mock)，Windows 走 DingRTC "
        "pybind11 binding；Linux 边缘形态不支持 (v1.0)。"
    )


__all__ = [
    "AudioBridge",
    "MacosRtcSession",
    "MacosRtcSessionConfig",
    "PcmRingBuffer",
    "Resampler",
    "get_default_rtc_session_factory",
]
