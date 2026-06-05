"""Recorder tests: stereo wav layout + rolling prune + Redis enqueue contract."""

from __future__ import annotations

import asyncio
import json
import os
import wave
from pathlib import Path

import pytest

from isales_telephony.modem_controller.recorder import (
    DiskFullError,
    Recorder,
    RedisRecordingQueue,
    file_sha256,
)


def _record_one(rec: Recorder, call_id: str, mtime: float) -> Path:
    """begin → append → finalize one call, then stamp a deterministic mtime
    so prune's newest-first ordering is testable regardless of FS mtime
    granularity."""
    rec.begin_call(call_id)
    rec.append_user(call_id, b"\x10\x00" * 800)
    rec.append_ai(call_id, b"\x00\x10" * 800)
    path = rec.finalize(call_id)
    assert path is not None
    os.utime(path, (mtime, mtime))
    return path


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


def test_prune_keeps_only_newest_n(tmp_path: Path) -> None:
    rec = Recorder(tmp_path)
    # 12 calls, mtimes 100..111 (call-11 newest).
    for i in range(12):
        _record_one(rec, f"call-{i}", mtime=100.0 + i)
        rec.prune(keep=10)
    remaining = sorted(p.stem for p in tmp_path.glob("*.wav"))
    # The two oldest (call-0, call-1) were pruned; newest 10 survive.
    assert "call-0" not in remaining
    assert "call-1" not in remaining
    assert len(remaining) == 10
    assert "call-11" in remaining and "call-2" in remaining


def test_prune_deletes_strictly_oldest_by_mtime(tmp_path: Path) -> None:
    rec = Recorder(tmp_path)
    # Write out of call-id order but with explicit mtimes so the oldest
    # by mtime (not by name) is the one pruned.
    _record_one(rec, "zzz", mtime=100.0)  # oldest
    _record_one(rec, "aaa", mtime=200.0)
    _record_one(rec, "mmm", mtime=300.0)
    rec.prune(keep=2)
    remaining = {p.stem for p in tmp_path.glob("*.wav")}
    assert remaining == {"aaa", "mmm"}  # "zzz" pruned despite alphabetical first


def test_prune_noop_when_under_limit(tmp_path: Path) -> None:
    rec = Recorder(tmp_path)
    _record_one(rec, "call-a", mtime=100.0)
    _record_one(rec, "call-b", mtime=101.0)
    rec.prune(keep=10)
    assert len({p for p in tmp_path.glob("*.wav")}) == 2


def test_prune_keep_zero_is_noop_does_not_wipe(tmp_path: Path) -> None:
    # keep<=0 must NOT delete everything — disabling recording is the
    # caller's job (skip begin_call), not prune's.
    rec = Recorder(tmp_path)
    _record_one(rec, "call-a", mtime=100.0)
    rec.prune(keep=0)
    assert len(list(tmp_path.glob("*.wav"))) == 1


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
