"""Unit tests for the PyObjC bridge wiring in ``macos_artc_pyobjc``.

Focus: framework loader path resolution, error reporting, and the
Cocoa-thread → asyncio cross-thread bridge. The session-level tests
live in ``test_macos_artc_pyobjc_session.py``.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# _resolve_framework_path: arg > env > default
# ---------------------------------------------------------------------------


def test_resolve_path_explicit_arg_wins(fresh_bridge, monkeypatch):
    _, _, bridge = fresh_bridge
    monkeypatch.setenv(bridge.FRAMEWORK_PATH_ENV, "/from/env")
    out = bridge._resolve_framework_path("/from/arg")
    assert out == Path("/from/arg")


def test_resolve_path_env_used_when_no_arg(fresh_bridge, monkeypatch):
    _, _, bridge = fresh_bridge
    monkeypatch.setenv(bridge.FRAMEWORK_PATH_ENV, "/from/env")
    assert bridge._resolve_framework_path(None) == Path("/from/env")


def test_resolve_path_default(fresh_bridge, monkeypatch):
    _, _, bridge = fresh_bridge
    monkeypatch.delenv(bridge.FRAMEWORK_PATH_ENV, raising=False)
    assert bridge._resolve_framework_path(None) == bridge.DEFAULT_FRAMEWORK_PATH


# ---------------------------------------------------------------------------
# _load_framework failure modes
# ---------------------------------------------------------------------------


def test_load_framework_missing_path_raises(fresh_bridge):
    _, _, bridge = fresh_bridge
    with pytest.raises(bridge.RtcError, match="not found"):
        bridge._load_framework("/no/such/path/AliRTCSdk.framework")


def test_load_framework_not_a_framework_dir(fresh_bridge, tmp_path: Path):
    _, _, bridge = fresh_bridge
    plain = tmp_path / "not_a_framework"
    plain.mkdir()
    with pytest.raises(bridge.RtcError, match="not a .framework"):
        bridge._load_framework(plain)


def test_load_framework_binary_missing(fresh_bridge, tmp_path: Path):
    _, _, bridge = fresh_bridge
    fw = tmp_path / "AliRTCSdk.framework"
    fw.mkdir()  # framework directory but no binary inside
    with pytest.raises(bridge.RtcError, match="binary missing"):
        bridge._load_framework(fw)


def test_load_framework_loadbundle_failure(
    fresh_bridge, tmp_path: Path,
):
    fake_objc, _, bridge = fresh_bridge

    # Build a valid-looking framework on disk.
    fw = tmp_path / "AliRTCSdk.framework"
    fw.mkdir()
    (fw / "AliRTCSdk").write_bytes(b"\x00" * 8)

    def boom(*_a, **_kw):
        raise RuntimeError("linker said no")

    fake_objc.loadBundle = boom
    with pytest.raises(bridge.RtcError, match="objc.loadBundle failed"):
        bridge._load_framework(fw)


def test_load_framework_class_lookup_missing(
    fresh_bridge, tmp_path: Path,
):
    fake_objc, _, bridge = fresh_bridge

    fw = tmp_path / "AliRTCSdk.framework"
    fw.mkdir()
    (fw / "AliRTCSdk").write_bytes(b"\x00" * 8)

    def load_without_engine(_name, module_globals, bundle_path):
        # Intentionally don't inject AliRtcEngine.
        module_globals.pop("AliRtcEngine", None)

    fake_objc.loadBundle = load_without_engine
    with pytest.raises(bridge.RtcError, match="AliRtcEngine"):
        bridge._load_framework(fw)


def test_load_framework_happy_path(fresh_bridge, tmp_path: Path):
    fake_objc, _, bridge = fresh_bridge

    fw = tmp_path / "AliRTCSdk.framework"
    fw.mkdir()
    (fw / "AliRTCSdk").write_bytes(b"\x00" * 8)

    sentinel = type("FakeEngine", (), {})

    def load_with_engine(_name, module_globals, bundle_path):
        module_globals["AliRtcEngine"] = sentinel

    fake_objc.loadBundle = load_with_engine
    assert bridge._load_framework(fw) is sentinel


# ---------------------------------------------------------------------------
# Delegate cross-thread bridge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delegate_join_result_dispatches_to_asyncio(fresh_bridge):
    """Selector fired from a worker thread must land on the asyncio loop."""
    _, _, bridge = fresh_bridge

    # Build a session-like stub with the bits the delegate touches.
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[tuple[int, str, int]] = loop.create_future()

    class _SessionStub:
        def __init__(self) -> None:
            self._loop = loop

        def _on_join_result(self, result: int, channel: str, elapsed: int) -> None:
            if not fut.done():
                fut.set_result((result, channel, elapsed))

    session = _SessionStub()
    delegate = bridge._AliRtcAudioDelegate.alloc().initWithSession_(session)
    assert delegate is not None

    def fire_from_thread():
        delegate.onJoinChannelResult_channel_elapsed_(0, "ch-1", 42)

    threading.Thread(target=fire_from_thread, daemon=True).start()
    result = await asyncio.wait_for(fut, timeout=1.0)
    assert result == (0, "ch-1", 42)


@pytest.mark.asyncio
async def test_delegate_audio_frame_extracts_primitives(fresh_bridge):
    """``onSubscribeAudioFrame_`` must copy PCM bytes + metadata as primitives."""
    _, _, bridge = fresh_bridge

    loop = asyncio.get_running_loop()
    received: list[tuple[str, bytes, int, int, int]] = []
    event = asyncio.Event()

    class _SessionStub:
        def __init__(self) -> None:
            self._loop = loop

        def _on_audio_frame(
            self,
            sender_uid: str,
            pcm: bytes,
            sample_rate: int,
            channels: int,
            timestamp_ms: int,
        ) -> None:
            received.append((sender_uid, pcm, sample_rate, channels, timestamp_ms))
            event.set()

    session = _SessionStub()
    delegate = bridge._AliRtcAudioDelegate.alloc().initWithSession_(session)
    assert delegate is not None

    # Fake AliRtcAudioFrame: methods returning the SDK fields.
    class _FakeAudioFrame:
        def data(self) -> bytes:  # noqa: PLR6301
            return b"\x01\x02\x03\x04"

        def samplesPerSec(self) -> int:  # noqa: PLR6301, N802
            return 16000

        def channels(self) -> int:  # noqa: PLR6301
            return 1

        def timestamp(self) -> int:  # noqa: PLR6301
            return 1234

        def remoteUid(self) -> str:  # noqa: PLR6301, N802
            return "engine-001"

    threading.Thread(
        target=lambda: delegate.onSubscribeAudioFrame_(_FakeAudioFrame()),
        daemon=True,
    ).start()

    await asyncio.wait_for(event.wait(), timeout=1.0)
    assert received == [("engine-001", b"\x01\x02\x03\x04", 16000, 1, 1234)]


@pytest.mark.asyncio
async def test_delegate_safe_when_loop_is_none(fresh_bridge):
    """Pre-loop wiring (e.g. selector fires before join())  must not blow up."""
    _, _, bridge = fresh_bridge

    class _SessionStub:
        _loop = None  # not yet bound

        def _on_error(self, code: int) -> None:  # noqa: PLR6301
            raise AssertionError("should not be invoked when loop is None")

    delegate = bridge._AliRtcAudioDelegate.alloc().initWithSession_(_SessionStub())
    # Should silently no-op.
    delegate.onError_(42)


def test_safe_call_extracts_method_value(fresh_bridge):
    _, _, bridge = fresh_bridge

    class _Obj:
        def channels(self) -> int:  # noqa: PLR6301
            return 2

    assert bridge._safe_call(_Obj(), "channels", 99) == 2


def test_safe_call_returns_default_on_missing(fresh_bridge):
    _, _, bridge = fresh_bridge

    class _Obj:
        pass

    assert bridge._safe_call(_Obj(), "nonexistent", 7) == 7
