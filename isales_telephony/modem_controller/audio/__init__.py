"""Platform-specific audio capture/playback backends.

The cross-platform ``AudioPipe`` orchestrator (resampling + jitter buffer)
lives in :mod:`isales_telephony.modem_controller.audio_pipe`. This package
holds the platform-coupled raw I/O backends:

- ``CaptureBackend`` / ``PlaybackBackend`` — the platform-neutral Protocols
  (re-exported from ``audio_pipe`` for convenience)
- ``LinuxAlsaCapture`` / ``LinuxAlsaPlayback`` — stage 6 work; placeholder
  stubs today
- ``MacOSCoreAudioCapture`` / ``MacOSCoreAudioPlayback`` — impl-deploy-macos
  PR #4

Selection is currently caller-driven (modem-controller's main.py picks an
impl). That is intentional: tests inject mock backends, and for production
the right concrete pair is platform-conditioned at call-site rather than
hidden behind a global dispatch — this matches how ``AudioPipe`` accepts
backends via constructor injection.

When PR #4 lands :func:`get_audio_backends` will provide the same
``sys.platform`` dispatch that :mod:`platforms` provides for USB watchers.
"""

from __future__ import annotations

import sys

from isales_telephony.modem_controller.audio_pipe import (
    CaptureBackend,
    PlaybackBackend,
)


class AudioBackendError(RuntimeError):
    """Raised when a platform audio backend cannot be loaded.

    ``platform`` field carries ``sys.platform`` so callers can render a
    useful message without leaking OS-specific details.
    """

    def __init__(self, *, platform: str, message: str) -> None:
        super().__init__(f"[{platform}] {message}")
        self.platform = platform


def get_capture_class() -> type[CaptureBackend]:
    """Return the platform-appropriate capture backend class.

    Stage-1 wiring: classes raise ``NotImplementedError`` from ``__init__``
    until PR #4 (macOS Core Audio) and stage 6 (Linux ALSA) land. Tests
    inject mocks and bypass this dispatch.
    """
    if sys.platform == "linux":
        from .linux_alsa import LinuxAlsaCapture  # noqa: PLC0415

        return LinuxAlsaCapture
    if sys.platform == "darwin":
        from .macos_coreaudio import MacOSCoreAudioCapture  # noqa: PLC0415

        return MacOSCoreAudioCapture
    raise AudioBackendError(
        platform=sys.platform,
        message=f"no capture backend for platform={sys.platform!r}",
    )


def get_playback_class() -> type[PlaybackBackend]:
    if sys.platform == "linux":
        from .linux_alsa import LinuxAlsaPlayback  # noqa: PLC0415

        return LinuxAlsaPlayback
    if sys.platform == "darwin":
        from .macos_coreaudio import MacOSCoreAudioPlayback  # noqa: PLC0415

        return MacOSCoreAudioPlayback
    raise AudioBackendError(
        platform=sys.platform,
        message=f"no playback backend for platform={sys.platform!r}",
    )


__all__ = [
    "AudioBackendError",
    "CaptureBackend",
    "PlaybackBackend",
    "get_capture_class",
    "get_playback_class",
]
