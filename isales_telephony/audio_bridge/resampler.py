"""int16 mono PCM rate conversion using scipy.signal.resample_poly.

Spec: device-hardware § Requirement: audio-bridge 重采样 — modem 物理
通道 8 kHz ↔ RTC wire 16 kHz, "重采样质量 MUST 满足通话语音可懂度"
(GSM telephony tier).

Python 3.13+ removed :mod:`audioop` (PEP 594). scipy is already in the
isales-telephony dependency tree (``numpy>=1.26`` + ``scipy>=1.12``),
so we lean on :func:`scipy.signal.resample_poly` (polyphase FIR) for
both directions.

Performance: polyphase FIR for 8 kHz↔16 kHz on 20 ms frames is well
under 1 ms per frame on Apple Silicon. Real-time audio (50 Hz frame
cadence) is comfortably ahead.
"""

from __future__ import annotations

from math import gcd
from typing import Final

import numpy as np
from scipy import signal

PCM_DTYPE: Final = np.int16
PCM_SAMPLE_BYTES: Final = 2


class Resampler:
    """Stateless int16 mono PCM resampler.

    "Stateless" because polyphase FIR with default settings re-windows
    each call. For real-time audio at 8↔16 kHz with 20 ms frames the
    per-frame seam artefact is inaudible — but a future version might
    pin a stateful overlap-save FIR if perceptual quality requires it.

    Args:
        src_rate: input sample rate (Hz). Must be > 0.
        dst_rate: output sample rate (Hz). Must be > 0.

    Raises:
        ValueError: if either rate is non-positive.
    """

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        if src_rate <= 0 or dst_rate <= 0:
            raise ValueError("sample rates must be positive")
        self._src_rate = src_rate
        self._dst_rate = dst_rate
        # Reduce up/down to their coprime form so resample_poly's FIR
        # design uses the smallest filter possible.
        g = gcd(src_rate, dst_rate)
        self._up = dst_rate // g
        self._down = src_rate // g

    @property
    def src_rate(self) -> int:
        return self._src_rate

    @property
    def dst_rate(self) -> int:
        return self._dst_rate

    def resample(self, pcm: bytes) -> bytes:
        """Resample a single PCM chunk.

        Args:
            pcm: int16 little-endian mono PCM. Length MUST be even.

        Returns: int16 little-endian mono PCM at ``dst_rate``.

        Raises:
            ValueError: if ``pcm`` length is not an even byte count.
        """
        if len(pcm) == 0:
            return b""
        if len(pcm) % PCM_SAMPLE_BYTES:
            raise ValueError(
                f"pcm length must be a multiple of {PCM_SAMPLE_BYTES} bytes "
                f"(got {len(pcm)})",
            )
        if self._src_rate == self._dst_rate:
            return pcm
        samples = np.frombuffer(pcm, dtype=PCM_DTYPE)
        # resample_poly works in float; cast back to int16 with
        # explicit clipping to keep the conversion lossless at the
        # edges (the polyphase FIR can overshoot by a sample-fraction
        # near transients).
        out_float = signal.resample_poly(
            samples.astype(np.float32),
            up=self._up,
            down=self._down,
        )
        out_int = np.clip(
            np.round(out_float),
            np.iinfo(PCM_DTYPE).min,
            np.iinfo(PCM_DTYPE).max,
        ).astype(PCM_DTYPE)
        return bytes(out_int.tobytes())
