"""Windows WASAPI backend tests — exercise the wrapper without hitting
PortAudio / a real audio device.

Pattern matches ``tests/macos/test_coreaudio_pipe.py``: inject a fake
stream via ``_set_stream_for_test`` so the tests run on Linux CI and
macOS dev hosts as well as Windows runners. The real WASAPI latency
baseline (``≤ 50 ms``) runs against hardware during D1 PoC week 1
(windows-client-core tasks 2.1 / 2.4).
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from isales_telephony.modem_controller.audio.windows_wasapi import (
    DEFAULT_BLOCKSIZE,
    DEFAULT_DEVICE_KEYWORDS,
    WASAPI_MODE_ENV,
    WASAPI_MODE_EXCLUSIVE,
    WindowsWASAPICapture,
    WindowsWASAPIPlayback,
    _is_exclusive_mode,
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
        self.next_read_payload = b"\x00\x00" * DEFAULT_BLOCKSIZE
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


# -------------------------------------------------------------- capture


@pytest.mark.asyncio(loop_scope="session")
async def test_capture_read_chunk_returns_bytes() -> None:
    cap = WindowsWASAPICapture(device=0, blocksize=DEFAULT_BLOCKSIZE)
    stream = _FakeStream()
    cap._set_stream_for_test(stream)

    chunk = await cap.read_chunk()
    assert isinstance(chunk, bytes)
    assert len(chunk) == DEFAULT_BLOCKSIZE * 2
    assert stream.read_calls == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_capture_close_is_idempotent() -> None:
    cap = WindowsWASAPICapture(device=0)
    stream = _FakeStream()
    cap._set_stream_for_test(stream)
    await cap.close()
    await cap.close()
    assert stream.closed is True


@pytest.mark.asyncio(loop_scope="session")
async def test_capture_close_without_open_is_safe() -> None:
    cap = WindowsWASAPICapture(device=0)
    await cap.close()


@pytest.mark.asyncio(loop_scope="session")
async def test_capture_logs_overflow_and_counts_it() -> None:
    """PortAudio overflow flag MUST surface as a counter increment but
    SHALL NOT drop the chunk — AudioPipe's jitter buffer absorbs it.
    """
    cap = WindowsWASAPICapture(device=0, blocksize=DEFAULT_BLOCKSIZE)
    stream = _FakeStream()
    stream.read_overflowed = True
    cap._set_stream_for_test(stream)

    chunk = await cap.read_chunk()
    assert len(chunk) == DEFAULT_BLOCKSIZE * 2
    assert cap.overflow_count == 1

    await cap.read_chunk()
    assert cap.overflow_count == 2


@pytest.mark.asyncio(loop_scope="session")
async def test_capture_bubbles_unexpected_exceptions() -> None:
    """Hardware-disconnect mid-read MUST propagate so orchestrator can
    surface a HardwareAlert."""
    cap = WindowsWASAPICapture(device=0, blocksize=DEFAULT_BLOCKSIZE)
    stream = _FakeStream()
    stream.read_should_raise = RuntimeError("modem detached")
    cap._set_stream_for_test(stream)

    with pytest.raises(RuntimeError, match="modem detached"):
        await cap.read_chunk()


# -------------------------------------------------------------- playback


@pytest.mark.asyncio(loop_scope="session")
async def test_playback_write_chunk() -> None:
    pb = WindowsWASAPIPlayback(device=0, blocksize=DEFAULT_BLOCKSIZE)
    stream = _FakeStream()
    pb._set_stream_for_test(stream)

    payload = b"\x12\x34" * DEFAULT_BLOCKSIZE
    await pb.write_chunk(payload)
    assert stream.write_calls == 1
    assert stream.written == [payload]


@pytest.mark.asyncio(loop_scope="session")
async def test_playback_close_after_writes() -> None:
    pb = WindowsWASAPIPlayback(device=0)
    stream = _FakeStream()
    pb._set_stream_for_test(stream)
    await pb.write_chunk(b"\x00\x00" * DEFAULT_BLOCKSIZE)
    await pb.close()
    assert stream.stopped is True
    assert stream.closed is True


# -------------------------------------------------------------- dispatch


def test_dispatch_picks_windows_classes(monkeypatch: pytest.MonkeyPatch) -> None:
    """audio.__init__ dispatch must return WindowsWASAPI* on win32."""
    from isales_telephony.modem_controller.audio import (
        get_capture_class,
        get_playback_class,
    )

    monkeypatch.setattr(sys, "platform", "win32")
    assert get_capture_class() is WindowsWASAPICapture
    assert get_playback_class() is WindowsWASAPIPlayback


def test_default_device_keywords_includes_usb_audio_class_and_brands() -> None:
    """USB Audio Class + at least two GSM modem brands must be matchable."""
    kw_lower = [k.lower() for k in DEFAULT_DEVICE_KEYWORDS]
    assert "usb audio device" in kw_lower
    brand_count = sum(
        1 for k in kw_lower if k in {"quectel", "huawei", "simcom", "zte"}
    )
    assert brand_count >= 2


# -------------------------------------------------------------- exclusive mode env


def test_exclusive_mode_env_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(WASAPI_MODE_ENV, raising=False)
    assert _is_exclusive_mode() is False


def test_exclusive_mode_env_on_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WASAPI_MODE_ENV, WASAPI_MODE_EXCLUSIVE)
    assert _is_exclusive_mode() is True


def test_exclusive_mode_env_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WASAPI_MODE_ENV, "Exclusive")
    assert _is_exclusive_mode() is True


def test_exclusive_mode_env_ignores_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(WASAPI_MODE_ENV, "shared")
    assert _is_exclusive_mode() is False
    monkeypatch.setenv(WASAPI_MODE_ENV, "1")
    assert _is_exclusive_mode() is False


# -------------------------------------------------------------- protocol contract


def test_capture_satisfies_capture_backend_protocol() -> None:
    cap = WindowsWASAPICapture(device=0)
    assert callable(cap.read_chunk)
    assert callable(cap.close)
    coro = cap.close()
    assert asyncio.iscoroutine(coro)
    coro.close()


def test_playback_satisfies_playback_backend_protocol() -> None:
    pb = WindowsWASAPIPlayback(device=0)
    assert callable(pb.write_chunk)
    assert callable(pb.close)
    coro = pb.close()
    assert asyncio.iscoroutine(coro)
    coro.close()
