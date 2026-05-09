"""macOS Core Audio backend tests — exercise the wrapper without
hitting real audio hardware.

The latency baseline test (≤ 200 ms end-to-end) lives in
``test_coreaudio_latency.py`` and runs against a real loopback device on
a developer's Mac during PR #4 acceptance + on the Mac mini in PR #12.
This module covers the structural / lifecycle wrapper logic so it can
run in CI without an audio device.
"""

from __future__ import annotations

import asyncio

import pytest

from isales_telephony.modem_controller.audio.macos_coreaudio import (
    DEFAULT_DEVICE_KEYWORDS,
    MacOSCoreAudioCapture,
    MacOSCoreAudioPlayback,
)


class _FakeStream:
    """Fake sd.RawInputStream / RawOutputStream that records calls."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.closed = False
        self.read_calls = 0
        self.write_calls = 0
        self.written: list[bytes] = []
        self.next_read_payload = b"\x00\x00" * 160  # 160 samples of silence
        self.read_overflowed = False
        self.read_should_raise: Exception | None = None

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True

    def read(self, n: int) -> tuple[bytes, bool]:
        if self.read_should_raise is not None:
            exc = self.read_should_raise
            self.read_should_raise = None
            raise exc
        self.read_calls += 1
        return self.next_read_payload[: n * 2], self.read_overflowed

    def write(self, pcm: bytes) -> None:
        self.write_calls += 1
        self.written.append(pcm)


@pytest.mark.asyncio(loop_scope="session")
async def test_capture_read_chunk_returns_bytes() -> None:
    cap = MacOSCoreAudioCapture(device=0, blocksize=160)
    stream = _FakeStream()
    cap._set_stream_for_test(stream)

    chunk = await cap.read_chunk()
    assert isinstance(chunk, bytes)
    assert len(chunk) == 160 * 2  # 160 samples × int16
    assert stream.read_calls == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_capture_close_is_idempotent() -> None:
    cap = MacOSCoreAudioCapture(device=0)
    stream = _FakeStream()
    cap._set_stream_for_test(stream)
    await cap.close()
    await cap.close()  # second close MUST NOT raise
    assert stream.closed is True


@pytest.mark.asyncio(loop_scope="session")
async def test_capture_close_without_open_is_safe() -> None:
    """Tear-down must work even if open_stream() was never called."""
    cap = MacOSCoreAudioCapture(device=0)
    await cap.close()  # no exception


@pytest.mark.asyncio(loop_scope="session")
async def test_capture_logs_overflow_but_returns_data() -> None:
    cap = MacOSCoreAudioCapture(device=0, blocksize=160)
    stream = _FakeStream()
    stream.read_overflowed = True
    cap._set_stream_for_test(stream)
    chunk = await cap.read_chunk()
    assert len(chunk) == 320  # still returns the data despite overflow flag


@pytest.mark.asyncio(loop_scope="session")
async def test_playback_write_chunk() -> None:
    pb = MacOSCoreAudioPlayback(device=0, blocksize=160)
    stream = _FakeStream()
    pb._set_stream_for_test(stream)

    payload = b"\x12\x34" * 160
    await pb.write_chunk(payload)
    assert stream.write_calls == 1
    assert stream.written == [payload]


@pytest.mark.asyncio(loop_scope="session")
async def test_playback_close_after_writes() -> None:
    pb = MacOSCoreAudioPlayback(device=0)
    stream = _FakeStream()
    pb._set_stream_for_test(stream)
    await pb.write_chunk(b"\x00\x00" * 160)
    await pb.close()
    assert stream.stopped is True
    assert stream.closed is True


def test_dispatch_picks_macos_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end dispatch via audio.__init__ — confirm darwin path returns
    the right concrete classes, mirroring the USB watcher dispatch test.
    """
    import sys

    from isales_telephony.modem_controller.audio import (
        get_capture_class,
        get_playback_class,
    )

    monkeypatch.setattr(sys, "platform", "darwin")
    assert get_capture_class() is MacOSCoreAudioCapture
    assert get_playback_class() is MacOSCoreAudioPlayback


def test_default_device_keywords_includes_common_modem_brands() -> None:
    """The keyword list MUST include at least USB Audio CODEC + 2 brand
    names so a vanilla Quectel / Huawei modem auto-matches.
    """
    keywords_lower = [k.lower() for k in DEFAULT_DEVICE_KEYWORDS]
    assert "usb audio codec" in keywords_lower
    brand_count = sum(
        1 for k in keywords_lower if k in {"quectel", "huawei", "simcom", "zte"}
    )
    assert brand_count >= 2


@pytest.mark.asyncio(loop_scope="session")
async def test_capture_bubbles_unexpected_exceptions() -> None:
    """Unexpected exceptions from the underlying stream MUST propagate so
    modem-controller can surface a fatal device error to engine.
    """
    cap = MacOSCoreAudioCapture(device=0, blocksize=160)
    stream = _FakeStream()
    stream.read_should_raise = RuntimeError("device disconnected mid-read")
    cap._set_stream_for_test(stream)

    with pytest.raises(RuntimeError, match="device disconnected"):
        await cap.read_chunk()


# ---------- contract: implements the Protocol expected by AudioPipe ----------


def test_capture_satisfies_capture_backend_protocol() -> None:
    """MacOSCoreAudioCapture must structurally satisfy CaptureBackend.

    This is structural typing (Protocol) — runtime check is via
    ``isinstance`` only when the Protocol is decorated ``@runtime_checkable``.
    Even without that, asserting the methods exist with the right shape
    catches accidental rename / signature drift.
    """
    cap = MacOSCoreAudioCapture(device=0)
    assert callable(cap.read_chunk)
    assert callable(cap.close)
    # Shape: read_chunk returns awaitable[bytes]; close returns awaitable[None]
    coro = cap.close()
    assert asyncio.iscoroutine(coro)
    coro.close()  # don't await — we don't want side effects


def test_playback_satisfies_playback_backend_protocol() -> None:
    pb = MacOSCoreAudioPlayback(device=0)
    assert callable(pb.write_chunk)
    assert callable(pb.close)
    coro = pb.close()
    assert asyncio.iscoroutine(coro)
    coro.close()
