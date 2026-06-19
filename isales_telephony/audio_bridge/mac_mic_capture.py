"""mac dev-no-modem 路径的 mic capture: sounddevice → bytes 给 DingRTC.

Spec: engine-rtc-dingrtc-migration § 8 follow-up (dev-no-modem mic 输入
完整 wire). 与 ``modem_controller/audio/macos_coreaudio.py`` 区别:

- ``MacOSCoreAudioCapture`` 是 modem 路径,8 kHz 写死, keyword-match USB
  modem 设备 (Quectel / SIMCom / Huawei / ZTE),供 GSM PCM 通道用.
- 本类是 mac dev-no-modem 路径,**system default mic** (MacBook 内建麦克风
  或外接 mic),sample_rate 参数化 (默认 16 kHz 对齐 DingRTC engine-side
  send_sample_rate 默认值),不 keyword 过滤.

Lifecycle 由调用方管理 (orchestrator ``_on_dial_dev_no_modem``):

    capture = MacMicCapture(sample_rate=16000)
    capture.open()
    try:
        while not stopped:
            chunk = await capture.read_chunk()
            await rtc_session.push_audio(chunk, timestamp_ms=ts)
            ts += 20
    finally:
        capture.stop()
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# 16 kHz 默认 — DingRTC ``RtcSession.join(send_sample_rate=)`` 默认值, engine
# 端 isales-engine/main.py rtc_factory 不 override. 320 samples = 20 ms.
DEFAULT_SAMPLE_RATE = 16_000
DEFAULT_BLOCKSIZE = 320

# DIAG-REMOVE-AFTER-MIC-DEBUG: 上行诊断钩子.
# 仿 isales_telephony/modem_controller/audio/windows_serial_pcm.py 的同名 env
# 钩子, 让 dev-no-modem 路径可以用一段已知 WAV 文件代替 mac mic 当 upstream PCM
# 源 — 消除"用户没说话 / 说话太轻"这种二次怀疑, 把 mic ↔ ASR 链路问题与"声学环境"
# 隔离开. WAV 文件必须是 16 kHz mono int16 (afconvert -d LEI16@16000 -c 1);
# 不匹配直接 RuntimeError(快速暴露格式错误, 不静默 fallback). 到末尾循环回放,
# 持续注入直到 daemon stop. mic ↔ ASR 闭环验证完毕后整段 + 调用点一并删除.
_FAKE_WAV_ENV = "ISALES_FAKE_CAPTURE_WAV"


def _load_fake_wav_pcm(path: str, sample_rate: int) -> bytes:  # DIAG-REMOVE-AFTER-MIC-DEBUG
    import wave  # noqa: PLC0415

    with wave.open(path, "rb") as w:
        ch = w.getnchannels()
        sw = w.getsampwidth()
        fr = w.getframerate()
        if ch != 1 or sw != 2 or fr != sample_rate:
            raise RuntimeError(
                f"ISALES_FAKE_CAPTURE_WAV {path!r}: expected "
                f"mono/int16/{sample_rate}Hz, got ch={ch} sw={sw} rate={fr}",
            )
        return w.readframes(w.getnframes())


class MacMicCapture:
    """Async capture from system default input device (mac mic).

    Lazy ``open()`` (or auto-open on first ``read_chunk()``); the underlying
    ``sd.RawInputStream`` blocks on ``read()`` which is dispatched to a
    thread via ``asyncio.to_thread`` so the asyncio loop stays responsive.
    """

    def __init__(
        self,
        *,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        blocksize: int = DEFAULT_BLOCKSIZE,
        device: int | str | None = None,
    ) -> None:
        self._sample_rate = sample_rate
        self._blocksize = blocksize
        self._device = device
        self._stream: Any | None = None
        self._opened = False
        # DIAG-REMOVE-AFTER-MIC-DEBUG: fake-WAV upstream injection
        self._fake_wav_path: str | None = os.environ.get(_FAKE_WAV_ENV) or None
        self._fake_pcm: bytes = b""
        self._fake_pos: int = 0

    @property
    def chunk_duration_ms(self) -> int:
        """20 ms at 16 kHz / 320 samples; useful for timestamp accounting."""
        return int(self._blocksize * 1000 / self._sample_rate)

    def open(self) -> None:
        """Open the input stream. Idempotent. Raises ``RuntimeError`` on failure."""
        if self._opened:
            return
        if self._fake_wav_path:  # DIAG-REMOVE-AFTER-MIC-DEBUG
            self._fake_pcm = _load_fake_wav_pcm(
                self._fake_wav_path, self._sample_rate,
            )
            self._opened = True
            logger.warning(
                "mac_mic_capture_fake_wav_loaded path=%s pcm_bytes=%s "
                "duration_s=%.2f — mic bypassed (TEST-ONLY hook)",
                self._fake_wav_path,
                len(self._fake_pcm),
                len(self._fake_pcm) / 2 / self._sample_rate,
            )
            return
        try:
            import sounddevice as sd  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover  - mac venv has sounddevice
            raise RuntimeError(
                "sounddevice not installed; pip install -e '.[macos]'",
            ) from exc
        try:
            stream = sd.RawInputStream(
                device=self._device,  # None = system default mic
                samplerate=self._sample_rate,
                channels=1,
                dtype="int16",
                blocksize=self._blocksize,
                latency="low",
            )
            stream.start()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"mac mic capture open failed: {exc}") from exc
        self._stream = stream
        self._opened = True
        logger.info(
            "mac_mic_capture_opened sample_rate=%s blocksize=%s device=%s",
            self._sample_rate, self._blocksize, self._device,
        )

    async def read_chunk(self) -> bytes:
        """Block until ``blocksize`` int16 samples are buffered, then return bytes."""
        if not self._opened:
            self.open()
        if self._fake_wav_path:  # DIAG-REMOVE-AFTER-MIC-DEBUG
            # 限速 = 真实回放 (blocksize/sample_rate 秒/帧), 防止瞬时把整段 WAV 推完.
            await asyncio.sleep(self._blocksize / self._sample_rate)
            bytes_per_chunk = self._blocksize * 2  # int16 = 2 bytes/sample
            # 单次播放 + 之后注入静音 (silence injection 让 vendor VAD 看到
            # utterance 结束 → 触发 final, 不只是 partial). 真实电话场景里讲完
            # 话有自然停顿,这里模拟它. 不 loop 整段 — loop 会让 vendor 把整个
            # 流当一个长 utterance 永远不 finalize.
            if self._fake_pos >= len(self._fake_pcm):
                return b"\x00" * bytes_per_chunk  # silence
            chunk = self._fake_pcm[self._fake_pos:self._fake_pos + bytes_per_chunk]
            self._fake_pos += bytes_per_chunk
            if len(chunk) < bytes_per_chunk:
                # 末尾 partial chunk, pad with silence
                chunk = chunk + b"\x00" * (bytes_per_chunk - len(chunk))
            return chunk
        stream = self._stream
        assert stream is not None

        def _read() -> bytes:
            data, overflowed = stream.read(self._blocksize)  # type: ignore[attr-defined]
            if overflowed:
                logger.warning("mac_mic_capture overflow")
            return bytes(data)

        return await asyncio.to_thread(_read)

    def stop(self) -> None:
        """Stop + close the input stream. Idempotent."""
        if self._fake_wav_path:  # DIAG-REMOVE-AFTER-MIC-DEBUG
            self._opened = False
            self._fake_pcm = b""
            self._fake_pos = 0
            logger.info("mac_mic_capture_fake_wav_closed")
            return
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception:  # noqa: BLE001
            logger.exception("mac_mic_capture stop failed")
        self._stream = None
        self._opened = False
        logger.info("mac_mic_capture_closed")
