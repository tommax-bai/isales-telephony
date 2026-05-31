"""Windows SerialPcm-over-COM capture/playback backends.

Spec: windows-client-core / device-hardware § "SerialPcm-over-COM 音频 backend"
+ "音频帧格式" + "USB GSM modem 音频设备路径" + "PCM 通道按 SIMCom AT
协议启停 (CPCMREG)". Replaces the WASAPI / sounddevice path that was
deleted in §4.11 of the same change.

Why this backend instead of WASAPI
----------------------------------

D1 PoC on SIM7600G-H (v1.0 main SKU) showed the modem does NOT register
any USB Audio Class endpoint on Windows 10/11 — ``Get-PnpDevice -Class
AudioEndpoint`` returns ∅ for the device. The audio interface (MI_04 on
the SIMCom composite descriptor) shows up as a regular ``Class=Ports``
serial COM port; PCM bytes travel as raw int16 LE little-endian frames
over pyserial reads/writes, framed only by the AT control plane.

Frame contract (matches macos_coreaudio + linux_alsa)
-----------------------------------------------------

- 8 kHz / 16-bit / LE / mono / 20 ms frames  → 160 samples = 320 bytes
- pyserial ``timeout`` of 20 ms aligns naturally with the modem's 8 kHz
  physical clock — ``read(320)`` blocks until the modem has accumulated
  one frame and then returns, providing per-frame back-pressure for
  free.
- Frame format is modem-fixed: ``AT+CPCMFMT=?`` returns ``ERROR`` on
  the SIM7600G-H firmware in test (``LE20B04SIM7600G22``); we do NOT
  attempt to configure it from the host.

Lifetime split with the orchestrator
------------------------------------

The backend opens the audio COM port once at process startup (in
:meth:`open_port`) and keeps the handle for the lifetime of the
edge process — there is no per-call open/close. PCM byte stream is
gated by AT+CPCMREG=1 / =0 issued from
:class:`EdgeOrchestrator._CallContext` (see Decision 8 in design.md).
Between calls the COM port is silent (modem returns 0 bytes on read
until CPCMREG=1 is issued again), which the pyserial ``timeout``
mechanism absorbs without raising.

The backend itself NEVER issues AT commands; that would couple the
audio data plane to the AT control plane and violate the protocol/Audio
backend Protocol surface.

Tests inject a fake ``Serial`` object via :meth:`_set_serial_for_test`
so we don't need a real COM port (or pyserial) at import time on
non-Windows CI.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# GSM modem audio is fixed at 8 kHz mono int16 LE; matches macOS / Linux paths.
SAMPLE_RATE_HZ = 8_000
CHANNELS = 1
DTYPE = "int16"
# 160 samples = 20 ms at 8 kHz. ``serial.read(320)`` returns one frame.
DEFAULT_BLOCKSIZE = 160
DEFAULT_CHUNK_BYTES = DEFAULT_BLOCKSIZE * 2  # int16 → 2 bytes/sample

# SIMCom-class modems run the MI_04 audio interface at the same baud as
# the AT channel. 115200 is the documented default for the 7xxx series;
# the modem ignores the value (USB serial bridge does not actually clock
# the line), but pyserial requires a numeric baudrate to open the port.
DEFAULT_BAUDRATE = 115_200

# pyserial read timeout in seconds. 20 ms is one frame at 8 kHz; any
# shorter and we'd churn the asyncio loop on partial reads; longer and
# CPCMREG=0 → underrun detection lags.
DEFAULT_READ_TIMEOUT_S = 0.020


class SerialPcmError(RuntimeError):
    """Raised when the audio COM port cannot be opened / used.

    Distinct from :class:`PcmEnableError` (raised by ``AtClient`` when
    AT+CPCMREG=1 fails): this is a backend-side failure (port missing,
    permission denied, ``pyserial`` missing).
    """


def _open_serial(
    serial_path: str,
    *,
    baudrate: int,
    timeout: float,
) -> object:
    """Open the audio COM port via pyserial.

    Lazy-imports ``serial`` so this module is importable on hosts
    without pyserial (CI on Linux / macOS); the actual call only
    happens when a backend's ``open_port`` runs on a real Windows host.
    """
    import serial  # noqa: PLC0415

    try:
        return serial.Serial(
            port=serial_path,
            baudrate=baudrate,
            timeout=timeout,
            write_timeout=0.5,  # 500ms write timeout to prevent infinite blocking
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            rtscts=False,
            dsrdtr=False,
        )
    except Exception as exc:  # noqa: BLE001 — surface uniformly
        raise SerialPcmError(
            f"failed to open audio COM port {serial_path!r}: {exc}"
        ) from exc


class _SerialPortHolder:
    """Owns the pyserial handle + the asyncio offload pattern.

    Shared by capture + playback so closing on either side is a no-op
    on the other — both can be constructed from the same
    ``audio_serial_path`` and share lifetime semantics with the
    orchestrator's single capture / playback pair.
    """

    def __init__(
        self,
        *,
        serial_path: str,
        baudrate: int,
        timeout: float,
    ) -> None:
        self._serial_path = serial_path
        self._baudrate = baudrate
        self._timeout = timeout
        self._serial: object | None = None
        self._opened = False
        self._closed = False
        # Whether this holder owns (and may close) its pyserial handle.
        # A holder that adopts another's handle (full-duplex share on the
        # one audio COM port) sets this False so it never closes a handle
        # it borrowed — the owner's close() is authoritative.
        self._owns = True

    def open_port(self) -> None:
        """Open the pyserial handle. Idempotent."""
        if self._opened or self._closed:
            return
        self._serial = _open_serial(
            self._serial_path,
            baudrate=self._baudrate,
            timeout=self._timeout,
        )
        self._opened = True

    def adopt_serial_from(self, other: "_SerialPortHolder") -> None:
        """Share ``other``'s already-open pyserial handle (full-duplex).

        Windows COM ports are exclusive: capture (read) and playback
        (write) on the SAME audio port (SIMCom MI_04, e.g. COM11) CANNOT
        each open their own handle — the second open fails with access
        denied. So the capture opens the port and the playback adopts the
        same handle; pyserial's Win32 backend uses independent overlapped
        structures for read vs write, so concurrent read+write on one
        handle is safe. The adopting side never closes the borrowed handle
        (``_owns = False``); the owner's ``close()`` is authoritative.

        Writing uplink PCM to the *Diagnostics* port (COM10) instead — to
        dodge this double-open — was empirically proven SILENT at the far
        end (2026-05-31); uplink MUST go to the Audio port. See
        ``deploy/edge/windows/STATE.md`` § "SIM7600 USB audio uplink".
        """
        if not other._opened or other._serial is None:
            raise SerialPcmError(
                "adopt_serial_from: source holder is not open; call "
                "open_port() on the capture side first"
            )
        self._serial = other._serial
        self._serial_path = other._serial_path
        self._opened = True
        self._owns = False

    async def close(self) -> None:
        """Close the pyserial handle. Idempotent + safe to call from
        either backend's ``close()`` (the other side will short-circuit).
        """
        if self._closed:
            return
        self._closed = True
        ser = self._serial
        self._serial = None
        if ser is None or not self._owns:
            return  # borrowed handle — owner closes it, we must not
        try:
            ser.close()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — tear-down must never raise
            logger.exception(
                "serial_pcm: error closing audio COM port %s", self._serial_path
            )

    def _set_serial_for_test(self, serial: object) -> None:  # pragma: no cover
        """Test hook — inject a fake Serial without pyserial."""
        self._serial = serial
        self._opened = True


def _load_wav_pcm_8k_mono(path: str) -> bytes:
    """Load a WAV as raw 8 kHz mono int16 PCM (diagnostic fake-capture source).

    Used ONLY by the ``ISALES_FAKE_CAPTURE_WAV`` test hook below.
    """
    import wave  # noqa: PLC0415

    with wave.open(path, "rb") as w:
        if (
            w.getframerate() != SAMPLE_RATE_HZ
            or w.getnchannels() != CHANNELS
            or w.getsampwidth() != 2
        ):
            raise SerialPcmError(
                f"fake capture WAV must be {SAMPLE_RATE_HZ}Hz mono int16; got "
                f"{w.getframerate()}Hz {w.getnchannels()}ch {w.getsampwidth() * 8}bit"
            )
        return w.readframes(w.getnframes())


class WindowsSerialPcmCapture(_SerialPortHolder):
    """Reads 8 kHz int16 mono PCM from the modem's audio COM port.

    Implements the ``CaptureBackend`` Protocol (``read_chunk`` / ``close``).

    Args:
        serial_path: Path of the audio COM port (e.g. ``"COM11"``),
            obtained from
            :class:`isales_telephony.modem_controller.at_probe.IdentifyResult.audio_serial_path`.
        chunk_bytes: Bytes per call to :meth:`read_chunk`. Defaults to
            ``320`` = 20 ms @ 8 kHz int16. The orchestrator's pump loop
            calls this in a tight ``while not stopped`` loop.
        baudrate / timeout: pyserial open parameters. Defaults match
            SIMCom 7xxx convention; override only in tests.
    """

    def __init__(
        self,
        serial_path: str,
        *,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        baudrate: int = DEFAULT_BAUDRATE,
        timeout: float = DEFAULT_READ_TIMEOUT_S,
    ) -> None:
        super().__init__(
            serial_path=serial_path,
            baudrate=baudrate,
            timeout=timeout,
        )
        self._chunk_bytes = chunk_bytes
        # Diagnostic hook (TEST-ONLY, remove once edge→RTC→engine upstream is
        # verified): when ISALES_FAKE_CAPTURE_WAV points at an 8 kHz mono int16
        # WAV, read_chunk yields frames from that file (looping, real-time
        # paced) INSTEAD of the modem mic — lets us prove the upstream path
        # delivers known audio to the engine without anyone speaking on the
        # phone. The modem handle still opens (playback adopts it), so AI voice
        # still reaches the far end.
        import os  # noqa: PLC0415

        self._fake_wav_pcm: bytes | None = None
        self._fake_wav_pos = 0
        _fake_path = os.environ.get("ISALES_FAKE_CAPTURE_WAV", "")
        if _fake_path:
            self._fake_wav_pcm = _load_wav_pcm_8k_mono(_fake_path)
            logger.warning(
                "serial_pcm: FAKE CAPTURE active — upstream fed from %s "
                "(%d bytes), NOT the modem mic",
                _fake_path,
                len(self._fake_wav_pcm),
            )

    async def read_chunk(self) -> bytes:
        """Block until ``chunk_bytes`` of PCM are available, then return.

        Runs the blocking ``serial.read()`` on a worker thread to keep
        the asyncio loop responsive. During ``AT+CPCMREG=0`` windows
        (between calls) the modem stops emitting bytes and ``read()``
        returns whatever the pyserial ``timeout`` accumulated — which
        is fine; callers loop again and the eventual CPCMREG=1 resumes
        the stream.

        Note: pyserial timeout returns b"" when no data arrives within
        the timeout window. This is NOT an EOF — just a transient gap.
        We retry up to ``_MAX_EMPTY_READS`` before signalling EOF.
        """
        if not self._opened:
            self.open_port()

        # TEST-ONLY fake-capture hook (see __init__): feed upstream from a WAV.
        if self._fake_wav_pcm is not None:
            await asyncio.sleep(0.02)  # pace at ~real-time (20 ms / frame)
            pcm = self._fake_wav_pcm
            n = self._chunk_bytes
            chunk = pcm[self._fake_wav_pos:self._fake_wav_pos + n]
            self._fake_wav_pos += n
            if len(chunk) < n:  # wrap around (loop the clip)
                self._fake_wav_pos = n - len(chunk)
                chunk = chunk + pcm[:self._fake_wav_pos]
            return chunk

        ser = self._serial
        assert ser is not None

        chunk_size = self._chunk_bytes

        def _read_blocking() -> bytes:
            return bytes(ser.read(chunk_size))  # type: ignore[attr-defined]

        # Retry on empty reads — CPCMREG=1 may need a few hundred ms
        # before the modem starts streaming PCM bytes.
        max_empty = 50  # 50 × 20 ms = 1 second grace period
        for _ in range(max_empty):
            data = await asyncio.to_thread(_read_blocking)
            if data:
                return data
        return b""  # genuine EOF after sustained silence


class WindowsSerialPcmPlayback(_SerialPortHolder):
    """Writes 8 kHz int16 mono PCM to the modem's audio COM port.

    Implements the ``PlaybackBackend`` Protocol (``write_chunk`` /
    ``close``). See :class:`WindowsSerialPcmCapture` for lifetime +
    AT+CPCMREG gating notes.
    """

    def __init__(
        self,
        serial_path: str,
        *,
        baudrate: int = DEFAULT_BAUDRATE,
        timeout: float = DEFAULT_READ_TIMEOUT_S,
    ) -> None:
        super().__init__(
            serial_path=serial_path,
            baudrate=baudrate,
            timeout=timeout,
        )

    async def write_chunk(self, pcm: bytes) -> None:
        """Write one PCM chunk to the audio COM port.

        pyserial ``write()`` returns the number of bytes accepted; on
        the modem's tiny USB-serial FIFO it normally accepts the full
        chunk in one go. Short writes (writes returning < len(pcm))
        are uncommon enough that we treat them as an error worth
        logging — the orchestrator's playback pump will continue
        feeding the next chunk, so audio glitches stay short.
        """
        if not self._opened:
            self.open_port()
        ser = self._serial
        assert ser is not None

        def _write_blocking() -> None:
            import serial  # noqa: PLC0415

            try:
                n = ser.write(pcm)  # type: ignore[attr-defined]
                if n is not None and n < len(pcm):
                    logger.warning(
                        "serial_pcm: short write %d/%d bytes on %s",
                        n,
                        len(pcm),
                        self._serial_path,
                    )
            except serial.SerialTimeoutException:
                logger.warning(
                    "serial_pcm: write_timeout %d bytes on %s",
                    len(pcm),
                    self._serial_path,
                )

        await asyncio.to_thread(_write_blocking)


__all__ = [
    "DEFAULT_BAUDRATE",
    "DEFAULT_BLOCKSIZE",
    "DEFAULT_CHUNK_BYTES",
    "DEFAULT_READ_TIMEOUT_S",
    "SAMPLE_RATE_HZ",
    "SerialPcmError",
    "WindowsSerialPcmCapture",
    "WindowsSerialPcmPlayback",
]
