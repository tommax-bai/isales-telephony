"""Linux ALSA capture/playback backends — stage 6 work.

Today these are deliberate placeholders: stage 2 stops at modem-controller
IPC + AT command mock; real ALSA wiring lands at stage 6 alongside true
USB GSM modem hardware. impl-deploy-macos PR #1 introduces this package
solely to make the dispatch surface symmetrical with the macOS backends.
"""

from __future__ import annotations


class LinuxAlsaCapture:
    """ALSA PCM capture backend (8 kHz int16 LE mono). Stage 6 will wire
    this up to ``alsaaudio.PCM(alsaaudio.PCM_CAPTURE)`` against the modem's
    ALSA device path.
    """

    def __init__(self) -> None:
        raise NotImplementedError(
            "LinuxAlsaCapture is a stage 6 deliverable; tests inject mocks "
            "via the AudioPipe constructor today."
        )

    async def read_chunk(self) -> bytes:  # pragma: no cover  - unreachable
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover
        return None


class LinuxAlsaPlayback:
    """ALSA PCM playback backend (8 kHz int16 LE mono). Stage 6 deliverable.
    """

    def __init__(self) -> None:
        raise NotImplementedError(
            "LinuxAlsaPlayback is a stage 6 deliverable; tests inject mocks "
            "via the AudioPipe constructor today."
        )

    async def write_chunk(self, pcm: bytes) -> None:  # pragma: no cover
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover
        return None
