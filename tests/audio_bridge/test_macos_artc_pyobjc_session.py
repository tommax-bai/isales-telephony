"""Unit tests for :class:`MacosArtcPyObjCSession`.

The session integrates the bridge with the :class:`RtcSession` ABC.
We mock the framework loader to return a fake ``AliRtcEngine`` class
whose behaviour each test controls.
"""

from __future__ import annotations

import asyncio

import pytest


# ---------------------------------------------------------------------------
# Fake AliRtcEngine
# ---------------------------------------------------------------------------


class _FakeEngine:
    """Records every SDK call. Tests set knobs on the class attributes."""

    #: result code delivered to onJoinChannelResult_. ``None`` = never fire.
    auto_join_result: int | None = 0
    #: synchronously raised by joinChannel_channel_uid_ when truthy.
    join_raises: BaseException | None = None
    #: synchronously raised by pushExternalAudioFrameRawData_...
    push_raises: BaseException | None = None

    def __init__(self) -> None:
        self.delegate = None
        self.external_audio_enabled = False
        self.joined: tuple[str, str, str] | None = None
        self.leave_calls = 0
        self.destroy_calls = 0
        self.pushed: list[bytes] = []
        # Pulled out of the bridge by tests that need to fire the
        # delegate from inside the engine after join_channel completes.
        self._owning_session: object | None = None

    # PyObjC selector parity.
    @classmethod
    def sharedInstance(cls) -> "_FakeEngine":  # noqa: N802
        return cls()

    def setRtcEngineDelegate_(self, delegate) -> None:  # noqa: N802
        self.delegate = delegate

    def setDelegate_(self, delegate) -> None:  # noqa: N802
        self.delegate = delegate

    def setExternalAudioSource_(self, enabled: bool) -> None:  # noqa: N802
        self.external_audio_enabled = bool(enabled)

    def joinChannel_channel_uid_(  # noqa: N802
        self, token: str, channel: str, uid: str,
    ) -> None:
        if self.join_raises:
            raise self.join_raises
        self.joined = (token, channel, uid)
        if self.auto_join_result is not None and self.delegate is not None:
            # Fire on_join synchronously to mimic the SDK's
            # post-join event delivery on a worker thread. Real SDK
            # fires from a worker thread; cross-thread is exercised
            # in test_macos_artc_pyobjc_bridge.py.
            self.delegate.onJoinChannelResult_channel_elapsed_(
                self.auto_join_result, channel, 42,
            )

    def leaveChannel(self) -> None:  # noqa: N802
        self.leave_calls += 1

    def destroy(self) -> None:
        self.destroy_calls += 1

    def pushExternalAudioFrameRawData_sampleRate_channels_timestamp_(  # noqa: N802
        self, pcm: bytes, sample_rate: int, channels: int, timestamp_ms: int,
    ) -> None:
        if self.push_raises:
            raise self.push_raises
        self.pushed.append(bytes(pcm))


# ---------------------------------------------------------------------------
# Fixture: session with fake engine wired in via _load_framework patch
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory(fresh_bridge, monkeypatch):
    """Patch ``_load_framework`` to return the ``_FakeEngine`` class.

    Each test calls ``factory()`` to get a fresh session; the engine
    instance produced by ``sharedInstance()`` is reachable via
    ``session._engine`` after join.
    """
    _, _, bridge = fresh_bridge
    # Each fixture invocation resets _FakeEngine knobs.
    _FakeEngine.auto_join_result = 0
    _FakeEngine.join_raises = None
    _FakeEngine.push_raises = None

    monkeypatch.setattr(bridge, "_load_framework", lambda _p: _FakeEngine)

    def factory(**kwargs) -> "bridge.MacosArtcPyObjCSession":
        return bridge.MacosArtcPyObjCSession(**kwargs)

    return bridge, factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_join_success(session_factory):
    bridge, factory = session_factory
    sess = factory()
    await sess.join("ch-1", "tok", "edge-1")
    assert sess.is_joined is True
    assert sess._engine.joined == ("tok", "ch-1", "edge-1")
    assert sess._engine.external_audio_enabled is True
    await sess.leave()


@pytest.mark.asyncio
async def test_join_nonzero_result_raises(session_factory):
    bridge, factory = session_factory
    _FakeEngine.auto_join_result = 0x01030202
    sess = factory()
    with pytest.raises(bridge.RtcError):
        await sess.join("ch", "tok", "uid")
    assert sess.is_joined is False


@pytest.mark.asyncio
async def test_join_timeout(session_factory, monkeypatch):
    bridge, factory = session_factory
    _FakeEngine.auto_join_result = None  # never fire on_join

    real_wait_for = asyncio.wait_for
    monkeypatch.setattr(
        bridge.asyncio, "wait_for",
        lambda fut, timeout: real_wait_for(fut, 0.05),
    )

    sess = factory()
    with pytest.raises(bridge.RtcError):
        await sess.join("ch", "tok", "uid")
    assert sess.is_joined is False


@pytest.mark.asyncio
async def test_join_sync_raise_propagates_as_rtc_error(session_factory):
    bridge, factory = session_factory
    _FakeEngine.join_raises = RuntimeError("auth missing")
    sess = factory()
    with pytest.raises(bridge.RtcError, match="joinChannel failed synchronously"):
        await sess.join("ch", "tok", "uid")
    assert sess.is_joined is False


@pytest.mark.asyncio
async def test_second_join_fails_fast(session_factory):
    bridge, factory = session_factory
    sess = factory()
    await sess.join("ch", "tok", "uid")
    with pytest.raises(bridge.RtcError, match="already joined"):
        await sess.join("ch", "tok", "uid")
    await sess.leave()


@pytest.mark.asyncio
async def test_leave_idempotent(session_factory):
    bridge, factory = session_factory
    sess = factory()
    await sess.join("ch", "tok", "uid")
    engine = sess._engine
    await sess.leave()
    await sess.leave()  # no exception
    assert engine.destroy_calls == 1
    assert sess.is_joined is False


@pytest.mark.asyncio
async def test_audio_frames_yields_inbound(session_factory):
    bridge, factory = session_factory
    sess = factory()
    await sess.join("ch", "tok", "uid")

    # Push frames through the delegate's asyncio-side dispatcher.
    sess._on_audio_frame("engine-1", b"\x10\x20", 16000, 1, 100)
    sess._on_audio_frame("engine-1", b"\x30\x40", 16000, 1, 120)

    collected: list[bridge.PcmFrame] = []

    async def consume():
        async for frame in sess.audio_frames():
            collected.append(frame)
            if len(collected) == 2:
                break

    await asyncio.wait_for(consume(), timeout=1.0)
    await sess.leave()

    assert [f.pcm for f in collected] == [b"\x10\x20", b"\x30\x40"]
    assert collected[0].sender_uid == "engine-1"
    assert collected[0].sample_rate == 16000
    assert collected[1].timestamp_ms == 120


@pytest.mark.asyncio
async def test_push_audio_success(session_factory):
    bridge, factory = session_factory
    sess = factory()
    await sess.join("ch", "tok", "uid")
    await sess.push_audio(b"\xaa\xbb", timestamp_ms=10)
    assert sess._engine.pushed == [b"\xaa\xbb"]
    await sess.leave()


@pytest.mark.asyncio
async def test_push_audio_buffer_full_maps_to_backpressure(session_factory):
    bridge, factory = session_factory
    sess = factory()
    await sess.join("ch", "tok", "uid")
    _FakeEngine.push_raises = RuntimeError("audio buffer full, retry later")
    with pytest.raises(bridge.RtcPushBackpressure):
        await sess.push_audio(b"\x00", timestamp_ms=0)
    await sess.leave()


@pytest.mark.asyncio
async def test_push_audio_other_error_maps_to_rtc_error(session_factory):
    bridge, factory = session_factory
    sess = factory()
    await sess.join("ch", "tok", "uid")
    _FakeEngine.push_raises = RuntimeError("device disconnected")
    with pytest.raises(bridge.RtcError):
        await sess.push_audio(b"\x00", timestamp_ms=0)
    await sess.leave()


@pytest.mark.asyncio
async def test_push_audio_before_join_raises_not_joined(session_factory):
    bridge, factory = session_factory
    sess = factory()
    with pytest.raises(bridge.RtcNotJoined):
        await sess.push_audio(b"\x00", timestamp_ms=0)


@pytest.mark.asyncio
async def test_audio_frames_before_join_raises_not_joined(session_factory):
    bridge, factory = session_factory
    sess = factory()
    with pytest.raises(bridge.RtcNotJoined):
        async for _ in sess.audio_frames():
            pass


@pytest.mark.asyncio
async def test_inbound_queue_drops_oldest_on_overflow(session_factory):
    bridge, factory = session_factory
    sess = factory(inbound_capacity=2)
    await sess.join("ch", "tok", "uid")
    # Queue maxsize = inbound_capacity * 2 = 4. Push 6 → 2 oldest drop.
    for i in range(6):
        sess._on_audio_frame("engine-1", bytes([i]), 16000, 1, i)

    received: list[bridge.PcmFrame] = []

    async def consume():
        async for frame in sess.audio_frames():
            received.append(frame)
            if len(received) == 4:
                break

    await asyncio.wait_for(consume(), timeout=1.0)
    await sess.leave()
    timestamps = [f.timestamp_ms for f in received]
    # The 4 most-recent timestamps should survive.
    assert timestamps == [2, 3, 4, 5]
