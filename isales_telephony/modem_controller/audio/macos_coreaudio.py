"""macOS Core Audio capture/playback backends via sounddevice.

``sounddevice`` is the cross-platform PortAudio binding; on macOS it
auto-routes to Core Audio (HAL → AudioUnit). Both backends implement the
``CaptureBackend`` / ``PlaybackBackend`` Protocols defined in
:mod:`isales_telephony.modem_controller.audio_pipe`.

Stream parameters
-----------------

GSM line is 8 kHz mono int16 LE — same as the modem-side rate the
``AudioPipe`` orchestrator expects. We use ``blocksize=160`` (20 ms at
8 kHz) for low-latency operation. PortAudio + Core Audio also accept
``blocksize=80`` (10 ms) on Apple Silicon if 200 ms end-to-end latency
proves hard to hit at 20 ms.

Latency budget (impl-deploy-macos design Decision #6 — exceeding 200 ms
end-to-end blocks merge):

    write() → PortAudio output ringbuffer    ~10 ms
    Core Audio HAL → speaker / modem        ~10–30 ms
    modem GSM uplink to PSTN                ~30–60 ms (out of our control)
    PSTN echo / loopback                    ~30 ms (test harness)
    GSM downlink to modem capture           ~30–60 ms (out of our control)
    Core Audio HAL → PortAudio input        ~10–30 ms
    PortAudio ringbuffer → read()           ~10 ms
    --------------------------------------------
    end-to-end                               ~130–230 ms p50

The 200 ms target is achievable on Apple Silicon with low-latency
PortAudio mode. If a specific Mac mini / Mac Studio configuration
exceeds it, see ``tests/macos/test_coreaudio_latency.py`` for the
diagnostic — and follow the PR #4.7 fallback to AudioUnit direct API.

Device selection
----------------

``sd.query_devices()`` returns a list of {name, max_input_channels,
max_output_channels, default_samplerate}. We pick a device by substring
match on the modem-side keyword (``"USB Audio CODEC"``, ``"Quectel"``,
``"Huawei"``, ``"SIMCom"``). Tests inject a fake device id.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover  - typing only
    pass

logger = logging.getLogger(__name__)

# GSM modem audio is fixed at 8 kHz mono int16 LE.
SAMPLE_RATE_HZ = 8_000
CHANNELS = 1
DTYPE = "int16"
# 160 samples = 20 ms at 8 kHz. PortAudio's typical low-latency block.
DEFAULT_BLOCKSIZE = 160
# PortAudio "low" preset on macOS targets ~10 ms hardware buffer.
DEFAULT_LATENCY = "low"

# Substring match for picking a USB GSM modem audio interface from the
# system device list. Operators can override via env var when their modem
# shows up under a custom name.
DEFAULT_DEVICE_KEYWORDS = (
    "USB Audio CODEC",  # generic USB-Audio class device
    "Quectel",
    "Huawei",
    "SIMCom",
    "ZTE",
)


class CoreAudioError(RuntimeError):
    """Raised for sounddevice / Core Audio errors that callers should
    surface as a fatal modem-controller condition.
    """


def _resolve_device_id(
    keyword_hints: tuple[str, ...] = DEFAULT_DEVICE_KEYWORDS,
) -> int | str:
    """Pick the first input/output device whose name contains a hint.

    Returns ``sd.default.device`` (i.e. system default) if no keyword
    matches — this is fine for unit / loopback tests on a developer's
    Mac. For real GSM modem dial production, the keyword match should
    succeed; if it doesn't, the operator sets a custom keyword in env.
    """
    import sounddevice as sd  # noqa: PLC0415  (lazy import — macOS extras)

    for idx, dev in enumerate(sd.query_devices()):
        name = dev.get("name", "")
        for kw in keyword_hints:
            if kw.lower() in name.lower():
                logger.info(
                    "core_audio: matched device '%s' for keyword '%s' (index %d)",
                    name,
                    kw,
                    idx,
                )
                return int(idx)
    logger.warning(
        "core_audio: no GSM modem audio device matched %s; falling back to system default",
        keyword_hints,
    )
    default = sd.default.device
    # sd.default.device is a tuple (input, output) or a single index;
    # PortAudio accepts either as device= for a duplex stream pair.
    return default if isinstance(default, (int, str)) else int(default[0])


class _StreamWrapper:
    """Common scaffolding shared by capture + playback. Holds the
    PortAudio ``RawStream`` plus an asyncio loop reference for the
    threadsafe data path.
    """

    def __init__(
        self,
        *,
        device: int | str | None,
        stream_kwargs: dict[str, object],
    ) -> None:
        self._device = device
        self._stream_kwargs = stream_kwargs
        self._stream: object | None = None  # sd.RawInputStream / RawOutputStream
        self._loop: asyncio.AbstractEventLoop | None = None

    def _start(self, stream: object) -> None:
        self._stream = stream
        self._loop = asyncio.get_running_loop()
        stream.start()  # type: ignore[attr-defined]

    async def close(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            stream.stop()  # type: ignore[attr-defined]
            stream.close()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001  - tear-down must never raise
            logger.exception("core_audio: error closing stream")
        finally:
            self._stream = None


class MacOSCoreAudioCapture(_StreamWrapper):
    """Reads 8 kHz int16 mono PCM from the modem's USB audio interface.

    Implements the ``CaptureBackend`` Protocol (``read_chunk`` / ``close``).
    Open the underlying stream via :meth:`open_stream` (called once, before
    the first ``read_chunk``). For tests, inject a fake stream via
    :meth:`_set_stream_for_test`.
    """

    def __init__(
        self,
        *,
        device: int | str | None = None,
        blocksize: int = DEFAULT_BLOCKSIZE,
    ) -> None:
        super().__init__(
            device=device,
            stream_kwargs={
                "samplerate": SAMPLE_RATE_HZ,
                "channels": CHANNELS,
                "dtype": DTYPE,
                "blocksize": blocksize,
                "latency": DEFAULT_LATENCY,
            },
        )
        self._blocksize = blocksize
        self._chunk_bytes = blocksize * 2  # int16 = 2 bytes
        self._opened = False

    def open_stream(self) -> None:
        """Resolve the device + open the input stream. Idempotent."""
        if self._opened:
            return
        try:
            import sounddevice as sd  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover  - macos extras guarantee
            raise CoreAudioError(
                "sounddevice not installed; pip install -e '.[macos]'"
            ) from exc

        device = self._device if self._device is not None else _resolve_device_id()
        try:
            stream = sd.RawInputStream(device=device, **self._stream_kwargs)
        except Exception as exc:
            raise CoreAudioError(
                f"failed to open Core Audio capture stream on device={device!r}: {exc}"
            ) from exc
        self._start(stream)
        self._opened = True

    async def read_chunk(self) -> bytes:
        """Block until ``blocksize`` samples are available, then return them.

        Runs the blocking ``stream.read()`` on a thread to keep the asyncio
        loop responsive.
        """
        if not self._opened:
            self.open_stream()
        stream = self._stream
        assert stream is not None

        def _read_blocking() -> bytes:
            data, overflowed = stream.read(self._blocksize)  # type: ignore[attr-defined]
            if overflowed:
                logger.warning("core_audio: capture overflow")
            # sd.RawInputStream returns a CFFI buffer; cast to bytes.
            return bytes(data)

        return await asyncio.to_thread(_read_blocking)

    def _set_stream_for_test(self, stream: object) -> None:  # pragma: no cover
        """Test hook — bypass the sounddevice import."""
        self._stream = stream
        self._opened = True


class MacOSCoreAudioPlayback(_StreamWrapper):
    """Writes 8 kHz int16 mono PCM to the modem's USB audio interface."""

    def __init__(
        self,
        *,
        device: int | str | None = None,
        blocksize: int = DEFAULT_BLOCKSIZE,
    ) -> None:
        super().__init__(
            device=device,
            stream_kwargs={
                "samplerate": SAMPLE_RATE_HZ,
                "channels": CHANNELS,
                "dtype": DTYPE,
                "blocksize": blocksize,
                "latency": DEFAULT_LATENCY,
            },
        )
        self._blocksize = blocksize
        self._opened = False

    def open_stream(self) -> None:
        if self._opened:
            return
        try:
            import sounddevice as sd  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise CoreAudioError(
                "sounddevice not installed; pip install -e '.[macos]'"
            ) from exc

        device = self._device if self._device is not None else _resolve_device_id()
        try:
            stream = sd.RawOutputStream(device=device, **self._stream_kwargs)
        except Exception as exc:
            raise CoreAudioError(
                f"failed to open Core Audio playback stream on device={device!r}: {exc}"
            ) from exc
        self._start(stream)
        self._opened = True

    async def write_chunk(self, pcm: bytes) -> None:
        if not self._opened:
            self.open_stream()
        stream = self._stream
        assert stream is not None

        def _write_blocking() -> None:
            stream.write(pcm)  # type: ignore[attr-defined]

        await asyncio.to_thread(_write_blocking)

    def _set_stream_for_test(self, stream: object) -> None:  # pragma: no cover
        self._stream = stream
        self._opened = True


__all__ = [
    "CoreAudioError",
    "DEFAULT_BLOCKSIZE",
    "DEFAULT_DEVICE_KEYWORDS",
    "MacOSCoreAudioCapture",
    "MacOSCoreAudioPlayback",
    "SAMPLE_RATE_HZ",
]
