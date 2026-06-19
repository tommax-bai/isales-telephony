"""audio_bridge unit + integration tests.

Spec: arch-cloud-edge-split / device-hardware § Requirement: audio-
bridge 组件 — covers each scenario (重采样 / 反压 / 上下行环形 buffer
容量 / peer uid 过滤) end-to-end.

Test strategy:

- :class:`PcmRingBuffer` tested in isolation (overflow / close /
  get-on-closed semantics).
- :class:`Resampler` tested in isolation against analytically-known
  ratios + edge cases.
- :class:`MacosRtcSession` tested via pairing (channel-shared
  registry) + forced backpressure hook.
- :class:`AudioBridge` integration: two bridges joined to the same
  channel (one "edge", one "engine" stand-in), assert that audio
  flowing through modem rings ends up on the other side at the
  expected rate, after resampling.
"""

from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import numpy as np
import pytest
from isales_common.audio.rtc import RtcError, RtcNotJoined

from isales_telephony.audio_bridge import (
    AudioBridge,
    MacosRtcSession,
    MacosRtcSessionConfig,
    PcmRingBuffer,
    Resampler,
)
from isales_telephony.modem_controller.recorder import Recorder

# ==========================================================================
# PcmRingBuffer
# ==========================================================================


@pytest.mark.asyncio
async def test_ring_buffer_round_trip() -> None:
    ring = PcmRingBuffer(capacity_bytes=1024, name="t")
    await ring.put(b"hello")
    await ring.put(b"world")
    assert await ring.get() == b"hello"
    assert await ring.get() == b"world"


@pytest.mark.asyncio
async def test_ring_buffer_overflow_drops_oldest() -> None:
    ring = PcmRingBuffer(capacity_bytes=10, name="t")
    await ring.put(b"AAAA")
    await ring.put(b"BBBB")
    # Third chunk forces eviction of "AAAA".
    await ring.put(b"CCCC")
    assert ring.dropped_chunks == 1
    assert ring.dropped_bytes == 4
    assert await ring.get() == b"BBBB"
    assert await ring.get() == b"CCCC"


@pytest.mark.asyncio
async def test_ring_buffer_oversize_chunk_logged_and_dropped() -> None:
    ring = PcmRingBuffer(capacity_bytes=4, name="t")
    await ring.put(b"too-big")
    assert len(ring) == 0
    assert ring.dropped_chunks == 1
    assert ring.dropped_bytes == 7


@pytest.mark.asyncio
async def test_ring_buffer_close_unblocks_get() -> None:
    ring = PcmRingBuffer(capacity_bytes=64, name="t")

    async def consumer() -> int:
        n = 0
        try:
            while True:
                await ring.get()
                n += 1
        except StopAsyncIteration:
            return n

    task = asyncio.create_task(consumer())
    await ring.put(b"AB")
    await asyncio.sleep(0)  # let consumer run once
    await ring.close()
    n = await asyncio.wait_for(task, timeout=1.0)
    assert n == 1


def test_ring_buffer_invalid_capacity() -> None:
    with pytest.raises(ValueError, match="capacity_bytes"):
        PcmRingBuffer(capacity_bytes=0, name="t")


# ==========================================================================
# Resampler
# ==========================================================================


def test_resampler_invalid_rates() -> None:
    with pytest.raises(ValueError, match="positive"):
        Resampler(0, 8000)
    with pytest.raises(ValueError, match="positive"):
        Resampler(8000, -1)


def test_resampler_passthrough_when_rates_match() -> None:
    r = Resampler(16000, 16000)
    sample = (np.zeros(160, dtype=np.int16)).tobytes()
    assert r.resample(sample) == sample


def test_resampler_8k_to_16k_doubles_sample_count() -> None:
    """8 kHz → 16 kHz: a 20 ms frame (160 samples = 320 bytes) becomes
    40 samples worth at 16 kHz = wait, no: 20 ms @ 16 kHz = 320 samples
    = 640 bytes. So a 320-byte 8 kHz frame → 640-byte 16 kHz frame.
    """
    r = Resampler(8000, 16000)
    # 20 ms of silence @ 8 kHz mono int16 = 160 samples = 320 bytes.
    in_bytes = (np.zeros(160, dtype=np.int16)).tobytes()
    out = r.resample(in_bytes)
    # 20 ms @ 16 kHz mono int16 = 320 samples = 640 bytes.
    assert len(out) == 640


def test_resampler_16k_to_8k_halves_sample_count() -> None:
    r = Resampler(16000, 8000)
    in_bytes = (np.zeros(320, dtype=np.int16)).tobytes()  # 20 ms @ 16k
    out = r.resample(in_bytes)
    assert len(out) == 320  # 20 ms @ 8k


def test_resampler_empty_input_returns_empty() -> None:
    r = Resampler(8000, 16000)
    assert r.resample(b"") == b""


def test_resampler_odd_byte_count_raises() -> None:
    r = Resampler(8000, 16000)
    with pytest.raises(ValueError, match="multiple of 2"):
        r.resample(b"\x01\x02\x03")  # 3 bytes — not aligned to int16


def test_resampler_preserves_low_frequency_signal() -> None:
    """A 1 kHz tone resampled 8k → 16k → 8k should round-trip close to
    the original. Polyphase FIR introduces edge artefacts, so compare
    the steady-state middle window only."""
    r_up = Resampler(8000, 16000)
    r_down = Resampler(16000, 8000)
    t = np.arange(800, dtype=np.float32) / 8000.0  # 100 ms
    tone = (np.sin(2 * np.pi * 1000.0 * t) * 0.5 * 32767).astype(np.int16)
    round_trip = r_down.resample(r_up.resample(tone.tobytes()))
    rt_arr = np.frombuffer(round_trip, dtype=np.int16)
    # Trim 80 samples (10 ms) off each end to dodge transient artefacts.
    a = tone[80:-80].astype(np.float32)
    b = rt_arr[80:-80].astype(np.float32)
    # RMS error should be a small fraction of the signal RMS.
    err = float(np.sqrt(np.mean((a - b) ** 2)))
    sig = float(np.sqrt(np.mean(a**2)))
    assert err / sig < 0.05, f"RMS error {err / sig:.3f} too high"


# ==========================================================================
# MacosRtcSession
# ==========================================================================


@pytest.fixture(autouse=True)
def _clear_macos_channel_registry() -> None:
    """Stray channel registrations across tests confuse pairing
    assertions. Clear before AND after every test (PASS-through fixture)."""
    MacosRtcSession._channel_registry.clear()  # noqa: SLF001
    yield
    MacosRtcSession._channel_registry.clear()  # noqa: SLF001


@pytest.mark.asyncio
async def test_macos_session_join_leave() -> None:
    s = MacosRtcSession()
    assert s.is_joined is False
    await s.join(channel="c", token="t", uid="u")
    assert s.is_joined is True
    await s.leave()
    assert s.is_joined is False


@pytest.mark.asyncio
async def test_macos_session_double_join_raises() -> None:
    s = MacosRtcSession()
    await s.join(channel="c", token="t", uid="u")
    with pytest.raises(RtcError):
        await s.join(channel="c", token="t", uid="u")


@pytest.mark.asyncio
async def test_macos_session_push_before_join_raises() -> None:
    s = MacosRtcSession()
    with pytest.raises(RtcNotJoined):
        await s.push_audio(b"\x00\x00", timestamp_ms=0)


@pytest.mark.asyncio
async def test_macos_session_paired_delivery() -> None:
    """Two sessions on the same channel cross-deliver PCM frames."""
    a = MacosRtcSession()
    b = MacosRtcSession()
    await a.join(channel="c", token="t", uid="A")
    await b.join(channel="c", token="t", uid="B")

    payload = b"\xab\xcd" * 160
    await a.push_audio(payload, timestamp_ms=10)

    iterator = b.audio_frames()
    frame = await asyncio.wait_for(anext(iterator), timeout=1.0)
    assert frame.sender_uid == "A"
    assert frame.pcm == payload
    assert frame.timestamp_ms == 10

    await a.leave()
    await b.leave()


@pytest.mark.asyncio
async def test_macos_session_forced_backpressure_raises() -> None:
    s = MacosRtcSession()
    await s.join(channel="c", token="t", uid="u")
    s.force_backpressure_on_next_push = True
    from isales_common.audio.rtc import RtcPushBackpressure

    with pytest.raises(RtcPushBackpressure):
        await s.push_audio(b"\x00\x00", timestamp_ms=0)
    # Auto-resets after raising.
    assert s.force_backpressure_on_next_push is False
    await s.leave()


@pytest.mark.asyncio
async def test_macos_session_capacity_triggers_backpressure_event() -> None:
    cfg = MacosRtcSessionConfig(outbound_capacity=3)
    s = MacosRtcSession(config=cfg)
    await s.join(channel="c", token="t", uid="u")
    assert s.backpressure_events == 0
    for i in range(3):
        await s.push_audio(b"\x00\x00", timestamp_ms=i)
    # The third push hits capacity and signals.
    assert s.backpressure_events == 1
    await s.leave()


# ==========================================================================
# AudioBridge (integration)
# ==========================================================================


def _silence(ms: int, rate: int) -> bytes:
    """`ms` milliseconds of int16 mono silence at the given rate."""
    return (np.zeros(rate * ms // 1000, dtype=np.int16)).tobytes()


def _tone(ms: int, rate: int, freq: int = 1000) -> bytes:
    t = np.arange(rate * ms // 1000, dtype=np.float32) / rate
    return ((np.sin(2 * np.pi * freq * t) * 0.4 * 32767).astype(np.int16)).tobytes()


@pytest.mark.asyncio
async def test_bridge_upstream_modem_pcm_reaches_paired_peer() -> None:
    """End-to-end: write 8 kHz PCM into the bridge's upstream modem
    ring; a paired RTC session (acting as the cloud engine) receives
    it as 16 kHz PCM."""
    edge_session = MacosRtcSession()
    engine_session = MacosRtcSession()
    modem_up = PcmRingBuffer(capacity_bytes=8 * 1024, name="up")
    modem_down = PcmRingBuffer(capacity_bytes=8 * 1024, name="down")
    bridge = AudioBridge(
        rtc_session=edge_session,
        modem_upstream=modem_up,
        modem_downstream=modem_down,
        peer_uid="engine-c1",
    )

    await bridge.join(channel="c1", token="t", uid="edge-c1")
    await engine_session.join(channel="c1", token="t", uid="engine-c1")

    # Write a 20 ms 8 kHz tone into the upstream ring; the bridge
    # should resample to 16 kHz and push to the RTC session, where the
    # paired engine_session subscribes.
    modem_chunk = _tone(20, rate=8000)
    await modem_up.put(modem_chunk)

    iterator = engine_session.audio_frames()
    frame = await asyncio.wait_for(anext(iterator), timeout=1.0)
    assert frame.sender_uid == "edge-c1"
    # Frame is at 16 kHz now (bridge resampled).
    assert frame.sample_rate == 16000
    assert len(frame.pcm) == len(modem_chunk) * 2  # 8 kHz → 16 kHz

    await bridge.leave()
    await engine_session.leave()


@pytest.mark.asyncio
async def test_bridge_downstream_rtc_pcm_reaches_modem_ring() -> None:
    """Reverse direction: paired RTC session pushes 16 kHz PCM; the
    bridge resamples to 8 kHz and writes into the modem downstream ring."""
    edge_session = MacosRtcSession()
    engine_session = MacosRtcSession()
    modem_up = PcmRingBuffer(capacity_bytes=8 * 1024, name="up")
    modem_down = PcmRingBuffer(capacity_bytes=8 * 1024, name="down")
    bridge = AudioBridge(
        rtc_session=edge_session,
        modem_upstream=modem_up,
        modem_downstream=modem_down,
        peer_uid="engine-c1",
    )

    await bridge.join(channel="c1", token="t", uid="edge-c1")
    await engine_session.join(channel="c1", token="t", uid="engine-c1")

    cloud_chunk = _tone(20, rate=16000)  # 20 ms @ 16 kHz, 640 bytes
    await engine_session.push_audio(cloud_chunk, timestamp_ms=0)

    out = await asyncio.wait_for(modem_down.get(), timeout=1.0)
    # 8 kHz output = half the byte count.
    assert len(out) == 320

    await bridge.leave()
    await engine_session.leave()


@pytest.mark.asyncio
async def test_bridge_filters_frames_from_unexpected_uid() -> None:
    """A third session joining the same channel must not have its
    frames forwarded to the modem (defence-in-depth)."""
    edge_session = MacosRtcSession()
    engine_session = MacosRtcSession()
    third_session = MacosRtcSession()
    modem_up = PcmRingBuffer(capacity_bytes=4096, name="up")
    modem_down = PcmRingBuffer(capacity_bytes=4096, name="down")
    bridge = AudioBridge(
        rtc_session=edge_session,
        modem_upstream=modem_up,
        modem_downstream=modem_down,
        peer_uid="engine-c1",
    )

    await bridge.join(channel="c1", token="t", uid="edge-c1")
    await engine_session.join(channel="c1", token="t", uid="engine-c1")
    await third_session.join(channel="c1", token="t", uid="malicious")

    # Third pushes — should be filtered.
    await third_session.push_audio(_tone(20, rate=16000), timestamp_ms=0)
    # Engine pushes — should be forwarded.
    await engine_session.push_audio(_tone(20, rate=16000), timestamp_ms=0)

    out = await asyncio.wait_for(modem_down.get(), timeout=1.0)
    assert len(out) == 320
    assert bridge.stats.downstream_uid_filtered == 1
    assert bridge.stats.downstream_chunks == 1

    await bridge.leave()
    await engine_session.leave()
    await third_session.leave()


# ==========================================================================
# AudioBridge × Recorder (edge-local-call-recording)
# ==========================================================================


@pytest.mark.asyncio
async def test_bridge_records_both_channels_to_stereo_wav(tmp_path: Path) -> None:
    """The bridge taps user upstream (left) and AI downstream (right) into a
    Recorder; finalize yields a 16 kHz stereo wav with energy on both
    channels. Mirrors the orchestrator contract: it calls begin_call /
    finalize / prune; the bridge only appends."""
    recorder = Recorder(tmp_path)
    recorder.begin_call("call-rec")  # orchestrator does this on connect

    edge_session = MacosRtcSession()
    engine_session = MacosRtcSession()
    modem_up = PcmRingBuffer(capacity_bytes=8 * 1024, name="up")
    modem_down = PcmRingBuffer(capacity_bytes=8 * 1024, name="down")
    bridge = AudioBridge(
        rtc_session=edge_session,
        modem_upstream=modem_up,
        modem_downstream=modem_down,
        peer_uid="engine-c1",
        recorder=recorder,
        record_call_id="call-rec",
    )
    await bridge.join(channel="c1", token="t", uid="edge-c1")
    await engine_session.join(channel="c1", token="t", uid="engine-c1")

    # User upstream: 8 kHz tone → bridge resamples to 16 kHz → left channel.
    await modem_up.put(_tone(20, rate=8000))
    # AI downstream: engine pushes 16 kHz tone → bridge taps frame.pcm → right.
    await engine_session.push_audio(_tone(20, rate=16000), timestamp_ms=0)

    # Drain both directions so the pumps have certainly run their taps.
    up_iter = engine_session.audio_frames()
    await asyncio.wait_for(anext(up_iter), timeout=1.0)
    await asyncio.wait_for(modem_down.get(), timeout=1.0)
    await asyncio.sleep(0.05)

    await bridge.leave()
    await engine_session.leave()

    # Orchestrator-side finalize + prune.
    path = recorder.finalize("call-rec")
    recorder.prune(keep=10)
    assert path is not None and path.exists()
    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 2
        assert wav.getframerate() == 16000
        assert wav.getnframes() > 0
        frames = wav.readframes(wav.getnframes())
    arr = np.frombuffer(frames, dtype="<i2")
    left = arr[0::2].astype(np.float64)
    right = arr[1::2].astype(np.float64)
    # Both directions captured → both channels carry energy.
    assert np.sqrt(np.mean(left**2)) > 0
    assert np.sqrt(np.mean(right**2)) > 0


@pytest.mark.asyncio
async def test_bridge_without_recorder_writes_nothing(tmp_path: Path) -> None:
    """recorder=None → the bridge never touches the recordings dir."""
    edge_session = MacosRtcSession()
    engine_session = MacosRtcSession()
    modem_up = PcmRingBuffer(capacity_bytes=8 * 1024, name="up")
    modem_down = PcmRingBuffer(capacity_bytes=8 * 1024, name="down")
    bridge = AudioBridge(
        rtc_session=edge_session,
        modem_upstream=modem_up,
        modem_downstream=modem_down,
        peer_uid="engine-c1",
    )
    await bridge.join(channel="c1", token="t", uid="edge-c1")
    await engine_session.join(channel="c1", token="t", uid="engine-c1")
    await modem_up.put(_tone(20, rate=8000))
    await engine_session.push_audio(_tone(20, rate=16000), timestamp_ms=0)
    up_iter = engine_session.audio_frames()
    await asyncio.wait_for(anext(up_iter), timeout=1.0)
    await asyncio.wait_for(modem_down.get(), timeout=1.0)
    await bridge.leave()
    await engine_session.leave()
    assert list(tmp_path.glob("*.wav")) == []


@pytest.mark.asyncio
async def test_bridge_recovers_from_forced_backpressure() -> None:
    """When the RTC session signals push backpressure, the upstream
    pump retries (the bridge stats record the event) and ultimately
    delivers."""
    edge_session = MacosRtcSession()
    engine_session = MacosRtcSession()
    modem_up = PcmRingBuffer(capacity_bytes=16384, name="up")
    modem_down = PcmRingBuffer(capacity_bytes=16384, name="down")
    bridge = AudioBridge(
        rtc_session=edge_session,
        modem_upstream=modem_up,
        modem_downstream=modem_down,
        peer_uid="engine-c1",
    )

    await bridge.join(channel="c1", token="t", uid="edge-c1")
    await engine_session.join(channel="c1", token="t", uid="engine-c1")

    # Arm the backpressure raise on the next push, then write to the
    # modem ring — pump will hit backpressure, retry, succeed.
    edge_session.force_backpressure_on_next_push = True
    chunk = _tone(20, rate=8000)
    await modem_up.put(chunk)
    await modem_up.put(chunk)

    # The engine side should still receive *both* frames.
    iterator = engine_session.audio_frames()
    f1 = await asyncio.wait_for(anext(iterator), timeout=1.0)
    f2 = await asyncio.wait_for(anext(iterator), timeout=1.0)
    assert len(f1.pcm) == 640
    assert len(f2.pcm) == 640
    assert bridge.stats.upstream_backpressure_events >= 1

    await bridge.leave()
    await engine_session.leave()


@pytest.mark.asyncio
async def test_bridge_leave_terminates_both_pumps() -> None:
    edge_session = MacosRtcSession()
    modem_up = PcmRingBuffer(capacity_bytes=4096, name="up")
    modem_down = PcmRingBuffer(capacity_bytes=4096, name="down")
    bridge = AudioBridge(
        rtc_session=edge_session,
        modem_upstream=modem_up,
        modem_downstream=modem_down,
        peer_uid="engine-c1",
    )
    await bridge.join(channel="c1", token="t", uid="edge-c1")
    assert bridge.is_active is True
    await bridge.leave()
    assert bridge.is_active is False
    # leave() is idempotent.
    await bridge.leave()


def test_bridge_construction_requires_peer_uid() -> None:
    with pytest.raises(ValueError, match="peer_uid"):
        AudioBridge(
            rtc_session=MacosRtcSession(),
            modem_upstream=PcmRingBuffer(name="u"),
            modem_downstream=PcmRingBuffer(name="d"),
            peer_uid="",
        )
