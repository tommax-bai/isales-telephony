"""Unit tests for :class:`WindowsRtcSession`.

Uses a fake ``aliyun_artc_pywrap`` module substituted into ``sys.modules``
before importing the session module, so these tests run on any platform
(no real ARTC SDK / Windows runtime needed).

Spec contract: openspec/changes/windows-artc-pybind11/specs/device-hardware/
§ Requirement: Windows 边缘 ARTC SDK 通过 pybind11 binding 接入.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from types import ModuleType, SimpleNamespace
from typing import Callable

import pytest


# ---------------------------------------------------------------------------
# Fake binding module
# ---------------------------------------------------------------------------


class _FakeFrameRingBuffer:
    def __init__(self, capacity_frames: int = 64) -> None:
        self.capacity = capacity_frames
        self._frames: list[SimpleNamespace] = []
        self._dropped = 0

    def push(self, frame: SimpleNamespace) -> None:
        if len(self._frames) >= self.capacity:
            self._frames.pop(0)
            self._dropped += 1
        self._frames.append(frame)

    def drain(self, max_n: int = 8) -> list[SimpleNamespace]:
        n = min(max_n, len(self._frames))
        out = self._frames[:n]
        self._frames = self._frames[n:]
        return out

    def dropped(self) -> int:
        return self._dropped

    def size_approx(self) -> int:
        return len(self._frames)

    def clear(self) -> None:
        self._frames.clear()


class _FakeAudioObserver:
    def __init__(self, buffer: _FakeFrameRingBuffer, expected_remote_uid: str = "") -> None:
        self.buffer = buffer
        self.expected_remote_uid = expected_remote_uid


class _FakeEngineListener:
    def __init__(self) -> None:
        self.on_join: Callable[[int, str, str, int], None] | None = None
        self.on_leave: Callable[[int], None] | None = None
        self.on_connection_lost: Callable[[], None] | None = None
        self.on_error: Callable[[int, str], None] | None = None

    def set_on_join(self, cb): self.on_join = cb
    def set_on_leave(self, cb): self.on_leave = cb
    def set_on_connection_lost(self, cb): self.on_connection_lost = cb
    def set_on_error(self, cb): self.on_error = cb
    def clear_callbacks(self):
        self.on_join = self.on_leave = self.on_connection_lost = self.on_error = None


class _FakeAliyunArtcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


class _FakeEngineHandle:
    """Records every call so tests can assert the SDK was driven correctly."""

    def __init__(self) -> None:
        self._alive = False
        self.created_with_extras: str | None = None
        self.listener: _FakeEngineListener | None = None
        self.observer: _FakeAudioObserver | None = None
        self.stream_id: int | None = None
        self.added_streams: list[tuple[int, int]] = []
        self.pushed: list[bytes] = []
        self.joined_channel: tuple[str, str, str, str] | None = None
        self.leave_calls = 0
        self.destroy_calls = 0
        self.unregister_calls = 0
        self.push_raises: BaseException | None = None
        # auto_join_result: int — schedule the on_join callback after join_channel
        self.auto_join_result: int | None = 0

    def create(self, extras: str = "") -> None:
        if self._alive:
            raise _FakeAliyunArtcError(-1, "already created")
        self._alive = True
        self.created_with_extras = extras

    def is_alive(self) -> bool: return self._alive

    def set_event_listener(self, listener: _FakeEngineListener) -> None:
        assert self._alive, "engine destroyed"
        self.listener = listener

    def register_audio_observer(self, observer: _FakeAudioObserver) -> None:
        assert self._alive, "engine destroyed"
        self.observer = observer

    def unregister_audio_observer(self) -> None:
        self.observer = None
        self.unregister_calls += 1

    def add_external_audio_stream(self, channels: int = 1, sample_rate: int = 16000) -> int:
        assert self._alive
        self.stream_id = (self.stream_id or 0) + 1
        self.added_streams.append((channels, sample_rate))
        return self.stream_id

    def push_external_audio(self, *, stream_id: int, pcm: bytes,
                            sample_rate: int = 16000, channels: int = 1,
                            bytes_per_sample: int = 2) -> None:
        if self.push_raises:
            raise self.push_raises
        self.pushed.append(bytes(pcm))

    def remove_external_audio_stream(self, stream_id: int) -> None:
        pass

    def join_channel(self, token: str, channel: str, user_id: str,
                     user_name: str = "") -> None:
        assert self._alive
        self.joined_channel = (token, channel, user_id, user_name)
        if self.auto_join_result is not None and self.listener is not None:
            # Fire the on_join callback synchronously to mimic the SDK's
            # post-join event delivery. Real SDK fires from a worker
            # thread; we test the threadsafe path separately.
            cb = self.listener.on_join
            if cb is not None:
                cb(self.auto_join_result, channel, user_id, 42)

    def leave_channel(self) -> None:
        assert self._alive
        self.leave_calls += 1

    def destroy(self) -> None:
        self.destroy_calls += 1
        self._alive = False


def _make_fake_module() -> ModuleType:
    mod = ModuleType("aliyun_artc_pywrap")
    mod.EngineHandle = _FakeEngineHandle
    mod.EngineListener = _FakeEngineListener
    mod.AudioObserver = _FakeAudioObserver
    mod.FrameRingBuffer = _FakeFrameRingBuffer
    mod.AliyunArtcError = _FakeAliyunArtcError
    mod.__version__ = "0.0.0-test"
    return mod


@pytest.fixture()
def fake_artc(monkeypatch: pytest.MonkeyPatch):
    """Substitute the binding + reload the session module."""
    fake = _make_fake_module()
    monkeypatch.setitem(sys.modules, "aliyun_artc_pywrap", fake)

    # Drop cached import so the next import picks up the fake.
    sys.modules.pop("isales_telephony.audio_bridge.windows_rtc_session", None)
    from isales_telephony.audio_bridge import windows_rtc_session as wrs

    return fake, wrs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_join_success(fake_artc):
    fake, wrs = fake_artc
    sess = wrs.WindowsRtcSession()
    await sess.join("ch-1", "tok", "edge-1")
    assert sess.is_joined is True
    # SDK was actually driven.
    handles = [obj for obj in fake.__dict__.values()]
    # Engine handle was created via class; we can introspect through the session.
    assert sess._engine is not None
    assert sess._engine.joined_channel == ("tok", "ch-1", "edge-1", "edge-1")
    assert sess._engine.added_streams == [(1, 16000)]
    await sess.leave()


@pytest.mark.asyncio
async def test_join_returns_nonzero_raises(fake_artc):
    fake, wrs = fake_artc
    sess = wrs.WindowsRtcSession()
    # Capture engine to set auto_join_result before calling join.
    # We need to inject auto_join_result onto whatever engine the session
    # constructs. Patch the class to pre-set the field.
    orig_create = fake.EngineHandle.create

    def create_with_fail(self, extras=""):
        orig_create(self, extras)
        self.auto_join_result = 0x01030202  # AliEngineErrorJoinChannelFailed

    fake.EngineHandle.create = create_with_fail
    try:
        with pytest.raises(wrs.RtcError):
            await sess.join("ch-1", "tok", "edge-1")
    finally:
        fake.EngineHandle.create = orig_create
    assert sess.is_joined is False


@pytest.mark.asyncio
async def test_join_timeout(fake_artc, monkeypatch):
    fake, wrs = fake_artc
    sess = wrs.WindowsRtcSession()
    orig_create = fake.EngineHandle.create

    def create_no_callback(self, extras=""):
        orig_create(self, extras)
        self.auto_join_result = None  # never fire on_join

    fake.EngineHandle.create = create_no_callback
    # Capture the real wait_for before patching, so the replacement
    # doesn't recurse into itself.
    real_wait_for = asyncio.wait_for
    monkeypatch.setattr(
        wrs.asyncio, "wait_for",
        lambda fut, timeout: real_wait_for(fut, 0.05),
    )
    try:
        with pytest.raises(wrs.RtcError):
            await sess.join("ch-1", "tok", "edge-1")
    finally:
        fake.EngineHandle.create = orig_create
    assert sess.is_joined is False


@pytest.mark.asyncio
async def test_leave_idempotent(fake_artc):
    fake, wrs = fake_artc
    sess = wrs.WindowsRtcSession()
    await sess.join("ch", "tok", "edge")
    engine = sess._engine
    assert engine is not None
    await sess.leave()
    await sess.leave()  # idempotent — no exception
    assert engine.destroy_calls == 1
    assert sess.is_joined is False


@pytest.mark.asyncio
async def test_push_audio_propagates_error_as_rtc_error(fake_artc):
    fake, wrs = fake_artc
    sess = wrs.WindowsRtcSession()
    await sess.join("ch", "tok", "edge")
    sess._engine.push_raises = fake.AliyunArtcError(-7, "push backpressure")
    with pytest.raises(wrs.RtcError):
        await sess.push_audio(b"\x00\x00" * 160, timestamp_ms=0)
    await sess.leave()


@pytest.mark.asyncio
async def test_audio_frames_drains_ring_buffer(fake_artc):
    fake, wrs = fake_artc
    sess = wrs.WindowsRtcSession()
    await sess.join("ch", "tok", "edge")

    # Pretend the C++ side observed 3 frames.
    buf = sess._buffer
    assert buf is not None
    for i in range(3):
        buf.push(SimpleNamespace(
            pcm=bytes([i, i, i, i]),
            sample_rate=16000,
            channels=1,
            bytes_per_sample=2,
            num_samples=2,
            remote_uid="engine-1",
            remote_capture_ms=i,
        ))

    received: list[wrs.PcmFrame] = []

    async def consume():
        async for frame in sess.audio_frames():
            received.append(frame)
            if len(received) == 3:
                break

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(consumer, timeout=1.0)
    await sess.leave()

    assert len(received) == 3
    assert received[0].sender_uid == "engine-1"
    assert received[0].timestamp_ms == 0
    assert received[2].pcm == bytes([2, 2, 2, 2])


@pytest.mark.asyncio
async def test_push_audio_before_join_raises_not_joined(fake_artc):
    fake, wrs = fake_artc
    sess = wrs.WindowsRtcSession()
    with pytest.raises(wrs.RtcNotJoined):
        await sess.push_audio(b"\x00\x00", timestamp_ms=0)


@pytest.mark.asyncio
async def test_audio_frames_before_join_raises_not_joined(fake_artc):
    fake, wrs = fake_artc
    sess = wrs.WindowsRtcSession()
    with pytest.raises(wrs.RtcNotJoined):
        async for _ in sess.audio_frames():
            pass


@pytest.mark.asyncio
async def test_drainer_stops_on_leave(fake_artc):
    fake, wrs = fake_artc
    sess = wrs.WindowsRtcSession()
    await sess.join("ch", "tok", "edge")
    drainer = sess._drainer_task
    assert drainer is not None
    await sess.leave()
    # drainer must be done after leave.
    assert drainer.cancelled() or drainer.done()


def test_get_default_rtc_session_class_macos(monkeypatch, fake_artc):
    """Loading the Windows fake binding must not break the darwin route.

    Per macos-artc-pyobjc-binding the darwin path now returns either the
    real PyObjC binding (when ``[macos-artc]`` extras + the framework are
    available) or the mock loopback as fallback. Explicit success /
    fallback assertions live in tests/audio_bridge/test_init_routing.py;
    here we only assert the windows binding's import surface does not
    interfere with darwin routing.
    """
    fake, wrs = fake_artc
    from isales_telephony import audio_bridge

    monkeypatch.setattr(audio_bridge.sys, "platform", "darwin", raising=False)
    cls = audio_bridge.get_default_rtc_session_class()
    assert cls.__name__ in {"MacosArtcPyObjCSession", "MacosRtcSession"}


def test_get_default_rtc_session_class_linux_raises(monkeypatch, fake_artc):
    from isales_telephony import audio_bridge

    monkeypatch.setattr(audio_bridge.sys, "platform", "linux", raising=False)
    with pytest.raises(NotImplementedError):
        audio_bridge.get_default_rtc_session_class()


def test_get_default_rtc_session_class_windows(monkeypatch, fake_artc):
    from isales_telephony import audio_bridge

    monkeypatch.setattr(audio_bridge.sys, "platform", "win32", raising=False)
    cls = audio_bridge.get_default_rtc_session_class()
    assert cls.__name__ == "WindowsRtcSession"
