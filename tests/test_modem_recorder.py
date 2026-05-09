"""Recorder tests: stereo wav layout + Redis enqueue contract."""

from __future__ import annotations

import asyncio
import json
import wave
from pathlib import Path

import pytest

from isales_telephony.modem_controller.recorder import (
    DiskFullError,
    Recorder,
    RedisRecordingQueue,
    file_sha256,
)


def test_finalize_writes_stereo_16k_wav(tmp_path: Path) -> None:
    rec = Recorder(tmp_path)
    rec.begin_call("call-1")
    rec.append_user("call-1", b"\x10\x00" * 1600)  # 100 ms of left
    rec.append_ai("call-1", b"\x00\x10" * 1600)    # 100 ms of right
    out = rec.finalize("call-1")
    assert out is not None and out.exists()
    with wave.open(str(out), "rb") as wav:
        assert wav.getnchannels() == 2
        assert wav.getframerate() == 16000
        assert wav.getsampwidth() == 2
        # 100 ms of stereo audio = 1600 frames per channel.
        assert wav.getnframes() == 1600


def test_finalize_pads_shorter_track_with_silence(tmp_path: Path) -> None:
    rec = Recorder(tmp_path)
    rec.begin_call("call-2")
    rec.append_user("call-2", b"\x10\x00" * 800)   # 50 ms only
    rec.append_ai("call-2", b"\x00\x10" * 1600)    # 100 ms
    out = rec.finalize("call-2")
    assert out is not None
    with wave.open(str(out), "rb") as wav:
        # Longer side wins, padded silence on the shorter side.
        assert wav.getnframes() == 1600


def test_finalize_unknown_call_returns_none(tmp_path: Path) -> None:
    rec = Recorder(tmp_path)
    assert rec.finalize("never-began") is None


def test_begin_call_raises_when_disk_low(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(tmp_path, min_free_gb=999_999)
    with pytest.raises(DiskFullError):
        rec.begin_call("call-3")


def test_file_sha256_is_stable(tmp_path: Path) -> None:
    p = tmp_path / "x.bin"
    p.write_bytes(b"hello")
    assert file_sha256(p) == file_sha256(p)
    assert len(file_sha256(p)) == 64


@pytest.mark.asyncio
async def test_enqueue_pushes_recording_payload(tmp_path: Path) -> None:
    p = tmp_path / "rec.wav"
    p.write_bytes(b"\x00" * 16)

    pushed: list[tuple[str, str]] = []

    class FakeRedis:
        def rpush(self, key: str, value: str) -> int:
            pushed.append((key, value))
            return 1

    queue = RedisRecordingQueue(FakeRedis(), "worker:recording-upload")
    await queue.enqueue(call_id="call-77", local_path=p)
    assert len(pushed) == 1
    queue_name, payload = pushed[0]
    assert queue_name == "worker:recording-upload"
    decoded = json.loads(payload)
    assert decoded["call_id"] == "call-77"
    assert decoded["sha256"] == file_sha256(p)
    assert decoded["size_bytes"] == 16


@pytest.mark.asyncio
async def test_enqueue_handles_async_redis(tmp_path: Path) -> None:
    p = tmp_path / "rec.wav"
    p.write_bytes(b"\x00" * 16)
    seen: list[str] = []

    class AsyncRedis:
        async def rpush(self, key: str, value: str) -> int:
            seen.append(value)
            return 1

    queue = RedisRecordingQueue(AsyncRedis(), "worker:recording-upload")
    await queue.enqueue(call_id="call-async", local_path=p)
    assert seen and json.loads(seen[0])["call_id"] == "call-async"
