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

from isales_telephony.audio_bridge.bridge import AudioBridge
from isales_telephony.audio_bridge.resampler import Resampler
from isales_telephony.audio_bridge.ring_buffer import PcmRingBuffer
from isales_telephony.audio_bridge.session import (
    MacosRtcSession,
    MacosRtcSessionConfig,
)

__all__ = [
    "AudioBridge",
    "MacosRtcSession",
    "MacosRtcSessionConfig",
    "PcmRingBuffer",
    "Resampler",
]
