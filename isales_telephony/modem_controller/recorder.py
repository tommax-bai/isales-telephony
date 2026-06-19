"""Per-call stereo PCM recorder.

Spec: transcript § Requirement: 录音存储 (edge-local-call-recording) —
edge keeps the newest N recordings on local disk; no OSS upload in v1.0.

Design (edge-local-call-recording): write a stereo 16 kHz wav file locally
with channel layout L=user (upstream from modem) / R=AI (TTS output before
downsampling). v1.0 keeps recordings edge-local only — on hangup the
orchestrator finalizes the wav, then prunes the recordings dir to the
newest ``MAX_RECORDINGS`` files. OSS upload + ``call_record.recording_url``
write-back + frontend ``ts`` seek are v1.x (deferred); ``RedisRecordingQueue``
below is kept for that future path but is NOT called in v1.0.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import shutil
import struct
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import structlog

from isales_telephony.modem_controller.audio_pipe import ENGINE_SAMPLE_RATE

logger = structlog.get_logger(__name__)


class DiskFullError(Exception):
    """Recording space below the configured floor."""


@dataclass(slots=True)
class _CallTracks:
    user_chunks: list[bytes] = field(default_factory=list)
    ai_chunks: list[bytes] = field(default_factory=list)


class Recorder:
    """Buffer per-call stereo PCM and finalise to a wav on hangup."""

    def __init__(self, output_dir: Path, *, min_free_gb: int = 1) -> None:
        self._dir = output_dir
        self._min_free_bytes = min_free_gb * (1 << 30)
        self._tracks: dict[str, _CallTracks] = {}
        self._dir.mkdir(parents=True, exist_ok=True)

    def begin_call(self, call_id: str) -> None:
        """Reserve in-memory buffers for a new call.

        Raises DiskFullError if recording dir has < min_free_gb free.
        """

        free = shutil.disk_usage(self._dir).free
        if free < self._min_free_bytes:
            raise DiskFullError(f"only {free} bytes free, need {self._min_free_bytes}")
        self._tracks[call_id] = _CallTracks()

    def append_user(self, call_id: str, pcm_16k: bytes) -> None:
        track = self._tracks.get(call_id)
        if track is None:
            return
        track.user_chunks.append(pcm_16k)

    def append_ai(self, call_id: str, pcm_16k: bytes) -> None:
        track = self._tracks.get(call_id)
        if track is None:
            return
        track.ai_chunks.append(pcm_16k)

    def finalize(self, call_id: str) -> Path | None:
        """Flush buffers to a stereo 16 kHz wav and return its path."""

        track = self._tracks.pop(call_id, None)
        if track is None:
            return None
        path = self._dir / f"{call_id}.wav"
        _write_stereo_wav(path, track.user_chunks, track.ai_chunks)
        logger.info("recording_finalized", call_id=call_id, path=str(path))
        return path

    def prune(self, keep: int) -> None:
        """Delete oldest ``*.wav`` so at most ``keep`` newest files remain.

        Rolling retention for the edge-local recording path: callers invoke
        this right after :meth:`finalize` so disk usage stays bounded to the
        most recent ``keep`` calls. Ordering is by file mtime (newest kept).
        ``keep <= 0`` is a no-op — disabling recording is the caller's job
        (skip :meth:`begin_call`), not this method's, so a misconfigured
        ``keep=0`` MUST NOT wipe the whole directory.
        """

        if keep <= 0:
            return
        wavs = sorted(
            self._dir.glob("*.wav"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in wavs[keep:]:
            with contextlib.suppress(OSError):
                old.unlink()


def _write_stereo_wav(path: Path, left_chunks: list[bytes], right_chunks: list[bytes]) -> None:
    left = b"".join(left_chunks)
    right = b"".join(right_chunks)
    # Pad the shorter track with silence so frame counts match.
    if len(left) < len(right):
        left += b"\x00" * (len(right) - len(left))
    elif len(right) < len(left):
        right += b"\x00" * (len(left) - len(right))
    left_arr = np.frombuffer(left, dtype="<i2")
    right_arr = np.frombuffer(right, dtype="<i2")
    interleaved = np.empty((left_arr.size + right_arr.size,), dtype="<i2")
    interleaved[0::2] = left_arr
    interleaved[1::2] = right_arr
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(ENGINE_SAMPLE_RATE)
        wav.writeframes(interleaved.tobytes())


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class RedisRecordingQueue:
    """Push finalised recordings onto the worker's upload list.

    v1.x-reserved (edge-local-call-recording): the v1.0 recording path keeps
    files edge-local and rolling — it does NOT call this class. Kept in place
    for the future OSS-upload path (worker consumes ``worker:recording-upload``,
    uploads to OSS, writes ``call_record.recording_url``). Removal trigger: if
    the OSS path is cut from the roadmap, delete this class.

    The redis client is supplied by the caller so the same fakeredis-backed
    instance used elsewhere can be reused; production code injects a real
    redis.asyncio.Redis.
    """

    def __init__(self, redis: object, queue_name: str) -> None:
        self._redis = redis
        self._queue = queue_name

    async def enqueue(self, *, call_id: str, local_path: Path) -> None:
        payload = json.dumps(
            {
                "call_id": call_id,
                "local_path": str(local_path),
                "sha256": file_sha256(local_path),
                "size_bytes": local_path.stat().st_size,
            },
            ensure_ascii=False,
        )
        push = getattr(self._redis, "rpush", None)
        if push is None:
            raise RuntimeError("redis client missing rpush")
        result = push(self._queue, payload)
        if asyncio.iscoroutine(result):
            await result


__all__ = [
    "DiskFullError",
    "Recorder",
    "RedisRecordingQueue",
    "file_sha256",
]


# ---- Spec parity checks (compile-time-ish) ------------------------------
#
# A 5-minute call buffered fully in RAM ≈ 5 * 60 * 16000 * 2 bytes = 9.6 MB
# per channel × 2 channels = ~19 MB. Acceptable for v1's ≤ 8 concurrent
# calls (max ~150 MB peak); v2 may stream chunks straight to disk if call
# durations grow. Spec § PCM 音频管道 doesn't constrain this.
del struct  # struct import was reserved for future RIFF tweaks; not needed yet
