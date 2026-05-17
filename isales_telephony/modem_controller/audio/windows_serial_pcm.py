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

    async def close(self) -> None:
        """Close the pyserial handle. Idempotent + safe to call from
        either backend's ``close()`` (the other side will short-circuit).
        """
        if self._closed:
            return
        self._closed = True
        ser = self._serial
        self._serial = None
        if ser is None:
            return
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

    async def read_chunk(self) -> bytes:
        """Block until ``chunk_bytes`` of PCM are available, then return.

        Runs the blocking ``serial.read()`` on a worker thread to keep
        the asyncio loop responsive. During ``AT+CPCMREG=0`` windows
        (between calls) the modem stops emitting bytes and ``read()``
        returns whatever the pyserial ``timeout`` accumulated — which
        is fine; callers loop again and the eventual CPCMREG=1 resumes
        the stream.
        """
        if not self._opened:
            self.open_port()
        ser = self._serial
        assert ser is not None

        chunk_size = self._chunk_bytes

        def _read_blocking() -> bytes:
            return bytes(ser.read(chunk_size))  # type: ignore[attr-defined]

        return await asyncio.to_thread(_read_blocking)


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
            n = ser.write(pcm)  # type: ignore[attr-defined]
            if n is not None and n < len(pcm):
                logger.warning(
                    "serial_pcm: short write %d/%d bytes on %s",
                    n,
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
