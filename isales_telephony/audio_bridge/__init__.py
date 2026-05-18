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


def get_default_rtc_session_class() -> type["RtcSession"]:
    """Return the platform-appropriate :class:`RtcSession` implementation.

    - ``win32`` → :class:`WindowsRtcSession` (real ARTC via pybind11).
    - ``darwin`` → :class:`MacosArtcPyObjCSession` (real ARTC via PyObjC
      bridge, dev / QA) when the optional ``[macos-artc]`` extras +
      ``AliRTCSdk.framework`` are present. Falls back to
      :class:`MacosRtcSession` mock loopback when they are not (WARN
      log + install hint).
    - other → ``NotImplementedError``.

    Both real bindings (Windows pybind11, macOS PyObjC) import lazily so
    the rest of the package keeps importing cleanly on every platform
    for tests.
    """
    if sys.platform == "win32":
        from isales_telephony.audio_bridge.windows_rtc_session import WindowsRtcSession
        return WindowsRtcSession
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
                        "pip install -e '.[macos-artc]' and unzip "
                        "AliRTCSdk_macos to ~/codes/vendor/AliRTCSdk_macos/ "
                        "to enable real ARTC on macOS dev / QA"
                    ),
                },
            )
            return MacosRtcSession
    raise NotImplementedError(
        f"No edge RtcSession implementation for platform {sys.platform!r}. "
        "macOS走 PyObjC binding (fallback mock)，Windows 走 pybind11 binding；"
        "Linux 边缘形态不支持 (v1.0)。"
    )


__all__ = [
    "AudioBridge",
    "MacosRtcSession",
    "MacosRtcSessionConfig",
    "PcmRingBuffer",
    "Resampler",
    "get_default_rtc_session_class",
]
