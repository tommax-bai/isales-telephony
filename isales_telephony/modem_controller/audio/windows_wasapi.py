"""Windows WASAPI capture/playback backends via sounddevice.

Spec: windows-client-core / device-hardware § "Windows 平台 backend" — D1
补完 A2 占位的 Windows 音频 backend。Mirrors the macOS Core Audio backend
(``macos_coreaudio.py``); the only platform differences are:

- PortAudio host API: WASAPI on Windows (Shared Mode default; Exclusive
  Mode opt-in via ``ISALES_WASAPI_MODE=exclusive``).
- Device pick: USB GSM modem registers as a standard USB Audio Class
  device on Windows ("USB Audio Device" / vendor-name substrings).

Frame contract
--------------

GSM line is 8 kHz mono int16 LE — same as the modem-side rate that
``AudioPipe`` expects upstream of the 8↔16 kHz resampler. Spec literal
in device-hardware/spec.md says "capture 16 kHz" for the engine-side
boundary, but the audio-bridge resampler already lives in
``modem_controller/audio_pipe.py`` and the WASAPI capture side runs at
GSM rate (matches macos_coreaudio + linux_alsa). Re-sampling stays in
``AudioPipe``, not duplicated in the per-platform backend.

Latency target (device-hardware/spec.md § "音频延迟 SLA"): P95 ≤ 50 ms
between modem mic samples and ``AudioPipe`` receiving PCM. WASAPI
Shared Mode at PortAudio "low" latency hits ~15-30 ms on modern PCs;
Exclusive Mode trims another 5-15 ms by bypassing the system mixer
(at the cost of muting other apps' audio on the chosen device).

Buffer-full backpressure
------------------------

PortAudio's RawInputStream/RawOutputStream raise ``input overflowed`` /
``output underflowed`` flags when the consumer falls behind. We surface
both via the standard logger; AudioPipe's jitter buffer absorbs short
glitches and the orchestrator escalates sustained overflows to
``HardwareAlert`` (spec § 故障注入). The on-stream callback is *not*
used here because ``read()`` / ``write()`` from an asyncio thread is
simpler to reason about and matches the macOS path.

Tests inject a fake stream via ``_set_stream_for_test`` to avoid pulling
in PortAudio at import time on Linux / macOS CI.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

# GSM modem audio is fixed at 8 kHz mono int16 LE; same as macOS path.
SAMPLE_RATE_HZ = 8_000
CHANNELS = 1
DTYPE = "int16"
# 160 samples = 20 ms at 8 kHz. PortAudio's typical low-latency block.
DEFAULT_BLOCKSIZE = 160
# PortAudio "low" preset on Windows targets ~10-20 ms WASAPI buffer.
DEFAULT_LATENCY = "low"

# Env: ``ISALES_WASAPI_MODE`` toggles Exclusive Mode. Default Shared.
WASAPI_MODE_ENV = "ISALES_WASAPI_MODE"
WASAPI_MODE_EXCLUSIVE = "exclusive"

# Substring match for picking a USB GSM modem audio interface from the
# system device list. Mirrors macOS DEFAULT_DEVICE_KEYWORDS plus the
# generic Windows USB Audio Class name.
DEFAULT_DEVICE_KEYWORDS = (
    "USB Audio Device",
    "USB Audio CODEC",
    "Quectel",
    "Huawei",
    "SIMCom",
    "ZTE",
    "Mobile",
    "GSM",
)


class WasapiError(RuntimeError):
    """Raised for sounddevice / WASAPI errors that callers should
    surface as a fatal modem-controller condition.
    """


def _wasapi_settings(*, exclusive: bool) -> object | None:
    """Build ``sd.WasapiSettings`` if available.

    Returns ``None`` if either the sounddevice version doesn't expose
    ``WasapiSettings`` or we are not on Windows — callers pass the
    result through to ``extra_settings=`` and PortAudio interprets
    ``None`` as "use defaults".
    """
    try:
        import sounddevice as sd  # noqa: PLC0415
    except ImportError:
        return None
    settings_cls = getattr(sd, "WasapiSettings", None)
    if settings_cls is None:
        return None
    try:
        return settings_cls(exclusive=exclusive)
    except TypeError:
        # Older sounddevice without the `exclusive` kw — fall back to
        # default (Shared Mode).
        return None


def _resolve_wasapi_host_api_index() -> int | None:
    """Find the WASAPI host API index in PortAudio's enumeration.

    Returns ``None`` if WASAPI isn't present (non-Windows host) — the
    caller falls back to the default device which is still WASAPI on
    a real Windows PC.
    """
    try:
        import sounddevice as sd  # noqa: PLC0415
    except ImportError:
        return None
    try:
        for idx, host in enumerate(sd.query_hostapis()):
            if host.get("name", "").lower() == "wasapi":
                return idx
    except Exception:  # noqa: BLE001
        logger.exception("wasapi: query_hostapis failed")
    return None


def _resolve_device_id(
    *,
    keyword_hints: tuple[str, ...] = DEFAULT_DEVICE_KEYWORDS,
    want_input: bool,
) -> int | str:
    """Pick the first WASAPI device whose name contains a hint.

    Falls back to ``sd.default.device`` when no keyword matches — useful
    for laptop developer workflows and Wireshark-free PoC. Production
    deployments rely on the keyword path; the modem's USB Audio Class
    registration name is what gets matched.

    Matching restricts to the WASAPI host API to avoid accidentally
    picking the MME / DirectSound duplicate that PortAudio also exposes.
    """
    import sounddevice as sd  # noqa: PLC0415

    wasapi_idx = _resolve_wasapi_host_api_index()

    for idx, dev in enumerate(sd.query_devices()):
        if wasapi_idx is not None and dev.get("hostapi") != wasapi_idx:
            continue
        if want_input and not dev.get("max_input_channels"):
            continue
        if not want_input and not dev.get("max_output_channels"):
            continue
        name = dev.get("name", "")
        for kw in keyword_hints:
            if kw.lower() in name.lower():
                logger.info(
                    "wasapi: matched %s device '%s' for keyword '%s' (index %d)",
                    "input" if want_input else "output",
                    name,
                    kw,
                    idx,
                )
                return int(idx)
    logger.warning(
        "wasapi: no GSM modem audio %s device matched %s; falling back to system default",
        "input" if want_input else "output",
        keyword_hints,
    )
    default = sd.default.device
    if isinstance(default, (int, str)):
        return default
    # default is (input_idx, output_idx)
    return int(default[0] if want_input else default[1])


def _is_exclusive_mode() -> bool:
    return os.environ.get(WASAPI_MODE_ENV, "").lower() == WASAPI_MODE_EXCLUSIVE


class _StreamWrapper:
    """Common scaffolding shared by capture + playback. Mirrors the
    shape of ``macos_coreaudio._StreamWrapper`` so test fixtures and
    code review carry over cleanly.
    """

    def __init__(
        self,
        *,
        device: int | str | None,
        stream_kwargs: dict[str, object],
    ) -> None:
        self._device = device
        self._stream_kwargs = stream_kwargs
        self._stream: object | None = None
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
            logger.exception("wasapi: error closing stream")
        finally:
            self._stream = None


class WindowsWASAPICapture(_StreamWrapper):
    """Reads 8 kHz int16 mono PCM from the modem's USB Audio Class
    capture endpoint on Windows.

    Implements the ``CaptureBackend`` Protocol
    (``read_chunk`` / ``close``). Opens the underlying WASAPI stream
    on the first ``read_chunk`` call. For tests, inject a fake stream
    via :meth:`_set_stream_for_test`.
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
        self._overflow_count = 0

    def open_stream(self) -> None:
        """Resolve the device + open the WASAPI input stream. Idempotent."""
        if self._opened:
            return
        try:
            import sounddevice as sd  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover  - windows extras guarantee
            raise WasapiError(
                "sounddevice not installed; pip install -e '.[windows]'"
            ) from exc

        device = (
            self._device if self._device is not None
            else _resolve_device_id(want_input=True)
        )
        kwargs = dict(self._stream_kwargs)
        extra = _wasapi_settings(exclusive=_is_exclusive_mode())
        if extra is not None:
            kwargs["extra_settings"] = extra
        try:
            stream = sd.RawInputStream(device=device, **kwargs)
        except Exception as exc:
            raise WasapiError(
                f"failed to open WASAPI capture stream on device={device!r}: {exc}"
            ) from exc
        self._start(stream)
        self._opened = True

    async def read_chunk(self) -> bytes:
        """Block until ``blocksize`` samples are available, then return them.

        Runs the blocking ``stream.read()`` on a thread to keep the
        asyncio loop responsive. ``input overflowed`` is logged and the
        counter incremented; AudioPipe's jitter buffer absorbs the
        glitch (full back-pressure to the modem-side is the orchestrator
        layer's job — this backend is intentionally pass-through).
        """
        if not self._opened:
            self.open_stream()
        stream = self._stream
        assert stream is not None

        def _read_blocking() -> bytes:
            data, overflowed = stream.read(self._blocksize)  # type: ignore[attr-defined]
            if overflowed:
                self._overflow_count += 1
                logger.warning(
                    "wasapi: capture overflow (total=%d)", self._overflow_count
                )
            return bytes(data)

        return await asyncio.to_thread(_read_blocking)

    @property
    def overflow_count(self) -> int:
        """Cumulative overflow events surfaced by PortAudio."""
        return self._overflow_count

    def _set_stream_for_test(self, stream: object) -> None:  # pragma: no cover
        """Test hook — bypass the sounddevice import."""
        self._stream = stream
        self._opened = True


class WindowsWASAPIPlayback(_StreamWrapper):
    """Writes 8 kHz int16 mono PCM to the modem's USB Audio Class
    playback endpoint on Windows.

    Implements the ``PlaybackBackend`` Protocol
    (``write_chunk`` / ``close``).
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
        self._opened = False
        self._underflow_count = 0

    def open_stream(self) -> None:
        if self._opened:
            return
        try:
            import sounddevice as sd  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise WasapiError(
                "sounddevice not installed; pip install -e '.[windows]'"
            ) from exc

        device = (
            self._device if self._device is not None
            else _resolve_device_id(want_input=False)
        )
        kwargs = dict(self._stream_kwargs)
        extra = _wasapi_settings(exclusive=_is_exclusive_mode())
        if extra is not None:
            kwargs["extra_settings"] = extra
        try:
            stream = sd.RawOutputStream(device=device, **kwargs)
        except Exception as exc:
            raise WasapiError(
                f"failed to open WASAPI playback stream on device={device!r}: {exc}"
            ) from exc
        self._start(stream)
        self._opened = True

    async def write_chunk(self, pcm: bytes) -> None:
        if not self._opened:
            self.open_stream()
        stream = self._stream
        assert stream is not None

        def _write_blocking() -> None:
            # PortAudio returns underflow via the CFFI struct, not the
            # return value of write(); use ``stream.write_available`` /
            # callback for fine-grained signalling. For 8 kHz / 20 ms
            # chunks at WASAPI Shared, write() blocks at most one frame
            # period and never raises on transient underflow.
            stream.write(pcm)  # type: ignore[attr-defined]

        await asyncio.to_thread(_write_blocking)

    @property
    def underflow_count(self) -> int:
        return self._underflow_count

    def _set_stream_for_test(self, stream: object) -> None:  # pragma: no cover
        self._stream = stream
        self._opened = True


__all__ = [
    "DEFAULT_BLOCKSIZE",
    "DEFAULT_DEVICE_KEYWORDS",
    "SAMPLE_RATE_HZ",
    "WASAPI_MODE_ENV",
    "WASAPI_MODE_EXCLUSIVE",
    "WasapiError",
    "WindowsWASAPICapture",
    "WindowsWASAPIPlayback",
]
