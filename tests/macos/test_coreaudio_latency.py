"""Core Audio end-to-end latency baseline.

Per impl-deploy-macos design Decision #6: end-to-end audio path latency
MUST be ≤ 200 ms; exceeding this aborts merge.

This test runs against a real PortAudio loopback device when available
(macOS only, with sounddevice installed and an output→input loopback
configured — typically via BlackHole / Loopback.app or with a USB modem
plugged in for the PR #12 hardware verification).

When no loopback is available the test is **skipped, not failed**. The
absolute pass/fail gate moves to PR #12 where the operator runs this on
the Mac mini against a real GSM modem audio interface.

Invocation:
    LOOPBACK_DEVICE_NAME='BlackHole 2ch' pytest tests/macos/test_coreaudio_latency.py

Latency measurement:
1. Capture and playback streams open on the same loopback device.
2. Write a known marker tone (1 kHz sine, 50 ms) at t=0.
3. Read continuously; locate the first sample whose absolute amplitude
   exceeds a noise threshold (50% of the marker amplitude).
4. ``latency = t_observed - t_written``. Asserts p95 over N=10 trials.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

LATENCY_BUDGET_MS = 200.0
TRIALS = 10
LOOPBACK_DEVICE_NAME_ENV = "ISALES_LOOPBACK_DEVICE_NAME"

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="Core Audio latency baseline is macOS-only",
)


def _loopback_device_index() -> int | None:
    """Locate a loopback-capable PortAudio device.

    Strategy:
    1. If ``ISALES_LOOPBACK_DEVICE_NAME`` env is set, match it exactly.
    2. Otherwise look for common loopback drivers (BlackHole, Loopback).
    3. Return None if nothing matches → caller skips.
    """
    try:
        import sounddevice as sd
    except ImportError:  # pragma: no cover  - macos extras guarantee
        return None

    target = os.environ.get(LOOPBACK_DEVICE_NAME_ENV)
    devices = sd.query_devices()
    candidates = []
    if target:
        for idx, dev in enumerate(devices):
            if dev["name"] == target:
                return idx
        return None

    for idx, dev in enumerate(devices):
        name = dev["name"].lower()
        is_loopback_brand = "blackhole" in name or "loopback" in name
        is_full_duplex = dev["max_input_channels"] > 0 and dev["max_output_channels"] > 0
        if is_loopback_brand and is_full_duplex:
            candidates.append(idx)
    if candidates:
        return candidates[0]
    return None


@pytest.mark.asyncio(loop_scope="session")
async def test_end_to_end_latency_within_budget() -> None:  # pragma: no cover (hardware)
    """Latency baseline ≤ 200 ms p95 over 10 trials.

    Runs only when a loopback device is detectable. Skipped on CI / dev
    machines without a configured loopback.
    """
    import numpy as np

    loopback = _loopback_device_index()
    if loopback is None:
        pytest.skip(
            "no loopback device detected; set ISALES_LOOPBACK_DEVICE_NAME or install BlackHole"
        )

    from isales_telephony.modem_controller.audio.macos_coreaudio import (
        SAMPLE_RATE_HZ,
        MacOSCoreAudioCapture,
        MacOSCoreAudioPlayback,
    )

    # 50 ms 1 kHz sine wave (int16, mono). Amplitude 50% to leave headroom.
    duration_s = 0.05
    samples_n = int(SAMPLE_RATE_HZ * duration_s)
    t = np.arange(samples_n) / SAMPLE_RATE_HZ
    marker = (np.sin(2 * np.pi * 1000 * t) * 0.5 * 32767).astype(np.int16)
    marker_bytes = marker.tobytes()

    capture = MacOSCoreAudioCapture(device=loopback, blocksize=160)
    playback = MacOSCoreAudioPlayback(device=loopback, blocksize=160)

    latencies_ms: list[float] = []
    capture.open_stream()
    playback.open_stream()
    try:
        # Drain initial silence
        for _ in range(5):
            await capture.read_chunk()

        for _ in range(TRIALS):
            t_write = time.perf_counter()
            await playback.write_chunk(marker_bytes)

            # Read until we see the marker (50% of peak amplitude).
            threshold = int(0.25 * 32767)
            seen = False
            samples_read = 0
            max_chunks = int(0.5 / 0.02)  # 500 ms ceiling
            for _ in range(max_chunks):
                chunk = await capture.read_chunk()
                samples_read += len(chunk) // 2
                if np.frombuffer(chunk, dtype=np.int16).max() >= threshold:
                    t_observed = time.perf_counter()
                    seen = True
                    break
            assert seen, "marker tone not observed within 500 ms"
            latency_ms = (t_observed - t_write) * 1000
            latencies_ms.append(latency_ms)

            # Drain any queued audio between trials.
            for _ in range(3):
                await capture.read_chunk()

    finally:
        await capture.close()
        await playback.close()

    p50 = sorted(latencies_ms)[len(latencies_ms) // 2]
    p95 = sorted(latencies_ms)[int(len(latencies_ms) * 0.95)]
    print(f"\ncore_audio latency: p50={p50:.1f}ms p95={p95:.1f}ms over {TRIALS} trials")

    assert p95 <= LATENCY_BUDGET_MS, (
        f"latency p95={p95:.1f}ms exceeds budget {LATENCY_BUDGET_MS}ms; "
        "blocks merge per impl-deploy-macos design Decision #6. "
        "Try lower blocksize (80) or fall back to direct AudioUnit API."
    )


def test_loopback_device_resolution_finds_known_drivers() -> None:
    """Smoke check the device resolution helper itself — does NOT require
    a loopback device, just verifies the function runs without import errors.
    """
    # Don't assert on result; just ensure the call doesn't crash and either
    # returns int or None.
    result = _loopback_device_index()
    assert result is None or isinstance(result, int)


# Sanity placeholder so the file always has at least one runnable test on
# macOS even when no loopback hardware is available — catches the case
# where the test module fails to import (e.g. sounddevice broken).
def test_module_imports_cleanly() -> None:
    from isales_telephony.modem_controller.audio import macos_coreaudio  # noqa: PLC0415

    assert macos_coreaudio.SAMPLE_RATE_HZ == 8_000


# Suppress the unused-import warning in coverage tools.
_ = asyncio
