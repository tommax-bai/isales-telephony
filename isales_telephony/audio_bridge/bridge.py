"""AudioBridge — glue between modem PCM rings and an RTC session.

Spec: arch-cloud-edge-split / device-hardware § Requirement: audio-
bridge 组件 (full scenario list) — owns:

- The upstream pump: modem PCM @ ``modem_rate`` → resample to
  ``rtc_rate`` → :meth:`RtcSession.push_audio`. Pauses on
  :class:`RtcPushBackpressure` (the upstream modem ring keeps
  accepting writes; on prolonged stall the ring drops oldest per its
  contract).
- The downstream pump: :meth:`RtcSession.audio_frames` filtered by
  the bound ``peer_uid`` → resample from ``rtc_rate`` to
  ``modem_rate`` → modem-downstream ring.
- Per-call lifecycle: :meth:`join` opens the RTC session + starts both
  pumps; :meth:`leave` tears down cleanly (sentinel both rings via
  RtcSession.leave's audio_frames termination; cancel the upstream
  pump; await both).

The bridge is *per-call*: one instance per active call, constructed
with the four moving parts injected (RTC session, two PCM rings, peer
uid + rates). Concurrent calls = multiple bridge instances.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

import numpy as np

from isales_common.audio.rtc import (
    RtcNotJoined,
    RtcPushBackpressure,
    RtcSession,
)

from isales_telephony.audio_bridge.resampler import Resampler
from isales_telephony.audio_bridge.ring_buffer import PcmRingBuffer

logger = logging.getLogger(__name__)


@dataclass
class _Stats:
    """Per-bridge counters — useful for D2 hardware-observability
    and end-to-end test assertions."""

    upstream_chunks: int = 0
    upstream_bytes: int = 0
    downstream_chunks: int = 0
    downstream_bytes: int = 0
    upstream_backpressure_events: int = 0
    downstream_uid_filtered: int = 0


class AudioBridge:
    """Per-call audio path glue.

    Args:
        rtc_session: the :class:`RtcSession` instance the bridge will
            drive. Caller owns construction (so tests inject mocks /
            in-memory; production uses :class:`MacosRtcSession` or a
            future Windows-native impl).
        modem_upstream: ring buffer modem-controller writes mic PCM
            into. Bridge reads from this end.
        modem_downstream: ring buffer modem-controller reads playback
            PCM from. Bridge writes into this end.
        peer_uid: the uid of the remote RTC participant whose frames
            we forward to the modem. Frames from other uids are
            counted and dropped (defence-in-depth — spec says only
            edge + engine share a channel).
        modem_rate: modem-side PCM rate in Hz (8000 for GSM).
        rtc_rate: RTC wire PCM rate in Hz (16000 for the iSales
            convention, configurable per call to match the engine).

    Construction is decoupled from connecting — call :meth:`join` to
    actually open the RTC session and start the pumps.
    """

    def __init__(
        self,
        *,
        rtc_session: RtcSession,
        modem_upstream: PcmRingBuffer,
        modem_downstream: PcmRingBuffer,
        peer_uid: str,
        modem_rate: int = 8000,
        rtc_rate: int = 16000,
    ) -> None:
        if not peer_uid:
            raise ValueError("peer_uid must be non-empty")
        self._rtc = rtc_session
        self._modem_upstream = modem_upstream
        self._modem_downstream = modem_downstream
        self._peer_uid = peer_uid
        self._modem_rate = modem_rate
        self._rtc_rate = rtc_rate
        self._up_resampler = Resampler(modem_rate, rtc_rate)
        self._down_resampler = Resampler(rtc_rate, modem_rate)
        self._upstream_task: asyncio.Task[None] | None = None
        self._downstream_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self.stats = _Stats()

    @property
    def is_active(self) -> bool:
        return self._rtc.is_joined and self._upstream_task is not None

    @property
    def rtc_session(self) -> RtcSession:
        """The bound RTC session. Exposed for ops introspection only —
        callers SHOULD NOT join / leave it directly; use the bridge's
        lifecycle methods."""
        return self._rtc

    # ----- lifecycle -----------------------------------------------------

    async def join(
        self,
        *,
        channel: str,
        token: str,
        uid: str,
    ) -> None:
        """Open the RTC session and start both pump tasks.

        Args:
            channel: RTC channel id. iSales convention: call_id.
            token: short-lived RTC token (signed cloud-side via
                :class:`RtcTokenIssuer`; arrives via
                :class:`Cloud2Edge.DialCommand.rtc_token`).
            uid: this side's uid (typically ``f"edge-{call_id}"``).

        Idempotent only via the underlying ``RtcSession.join``
        contract — calling twice on the same bridge raises
        :class:`RtcError` from the inner SDK wrapper.
        """
        await self._rtc.join(
            channel=channel,
            token=token,
            uid=uid,
            send_sample_rate=self._rtc_rate,
        )
        self._stop_event.clear()
        self._upstream_task = asyncio.create_task(
            self._upstream_loop(),
            name=f"audio_bridge_upstream_{channel}",
        )
        self._downstream_task = asyncio.create_task(
            self._downstream_loop(),
            name=f"audio_bridge_downstream_{channel}",
        )
        logger.info(
            "audio_bridge_joined",
            extra={"channel": channel, "uid": uid, "peer_uid": self._peer_uid},
        )

    async def leave(self) -> None:
        """Tear down: leave the RTC session (which sentinels the
        inbound iterator on the downstream pump), cancel the upstream
        pump, await both. Idempotent.
        """
        self._stop_event.set()
        # Leaving the RTC session terminates audio_frames() → the
        # downstream pump exits naturally. The upstream pump checks
        # _stop_event and cancellation in its loop.
        if self._rtc.is_joined:
            with contextlib.suppress(Exception):
                await self._rtc.leave()
        for task in (self._upstream_task, self._downstream_task):
            if task is None or task.done():
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._upstream_task = None
        self._downstream_task = None
        logger.info("audio_bridge_left", extra={"peer_uid": self._peer_uid})

    # ----- pumps ---------------------------------------------------------

    async def _upstream_loop(self) -> None:
        """modem mic → resample → RTC push, with backpressure pause."""
        try:
            while not self._stop_event.is_set():
                try:
                    chunk = await self._modem_upstream.get()
                except StopAsyncIteration:
                    return
                if not chunk:
                    continue
                resampled = self._up_resampler.resample(chunk)
                pushed = False
                while not pushed:
                    try:
                        # The RtcSession contract says push_audio
                        # *awaits* drain rather than raising — but the
                        # spec also lets concrete impls raise
                        # RtcPushBackpressure if they can't await
                        # drain. Handle both shapes.
                        await self._rtc.push_audio(
                            resampled,
                            timestamp_ms=_now_ms(),
                        )
                        pushed = True
                    except RtcPushBackpressure:
                        self.stats.upstream_backpressure_events += 1
                        # The upstream modem ring keeps absorbing frames
                        # at real-time cadence; oldest dropped per its
                        # contract. Yield a tick so the SDK can drain.
                        await asyncio.sleep(0.02)
                    except RtcNotJoined:
                        # leave() ran underneath us — exit cleanly.
                        return
                self.stats.upstream_chunks += 1
                self.stats.upstream_bytes += len(resampled)
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception("audio_bridge_upstream_unexpected_error")

    async def _downstream_loop(self) -> None:
        """RTC inbound → filter by peer uid → resample → modem playback."""
        _ds_count = 0
        try:
            async for frame in self._rtc.audio_frames():
                _ds_count += 1
                if _ds_count <= 5 or _ds_count % 200 == 0:
                    logger.info(
                        "downstream_frame n=%d uid=%s peer=%s pcm=%d rate=%d",
                        _ds_count,
                        frame.sender_uid,
                        self._peer_uid,
                        len(frame.pcm),
                        frame.sample_rate,
                    )
                # Accept frame if uid matches OR if uid is empty (POSITION_PLAYBACK
                # does not provide uid — in a 2-user channel all remote frames are
                # from the engine peer).
                if frame.sender_uid and frame.sender_uid != self._peer_uid:
                    self.stats.downstream_uid_filtered += 1
                    continue
                # Resample RTC frame → 8kHz mono for the SIM7600 audio port.
                # DingRTC frames are typically 48kHz stereo, but the rate MUST
                # come from frame.sample_rate (SSOT) — NOT a hardcoded 48kHz/6
                # decimation — so any source rate (incl. the 16kHz test path)
                # converts correctly and a DingRTC rate change can't silently
                # break uplink/downlink.
                pcm_arr = np.frombuffer(frame.pcm, dtype=np.int16)
                # Step 1: stereo → mono (average L+R channels)
                if frame.channels == 2:
                    mono = ((pcm_arr[0::2].astype(np.int32) + pcm_arr[1::2].astype(np.int32)) >> 1).astype(np.int16)
                else:
                    mono = pcm_arr
                # Step 2: resample frame.sample_rate → 8kHz
                src_rate = frame.sample_rate or 8000
                if src_rate == 8000:
                    decimated = mono
                elif src_rate % 8000 == 0:
                    decimated = mono[:: src_rate // 8000]
                else:  # non-integer ratio → linear interpolation
                    n_out = int(len(mono) * 8000 / src_rate)
                    decimated = np.interp(
                        np.arange(n_out, dtype=np.float64) * src_rate / 8000.0,
                        np.arange(len(mono), dtype=np.float64),
                        mono.astype(np.float64),
                    ).astype(np.int16)
                resampled = decimated.astype(np.int16).tobytes()
                if _ds_count <= 5:
                    logger.info(
                        "downstream_resampled ch=%d in=%d mono=%d out=%d",
                        frame.channels,
                        len(frame.pcm),
                        len(mono) * 2,
                        len(resampled),
                    )
                try:
                    await self._modem_downstream.put(resampled)
                except RuntimeError:
                    # Ring was closed externally — exit cleanly.
                    return
                self.stats.downstream_chunks += 1
                self.stats.downstream_bytes += len(resampled)
        except asyncio.CancelledError:
            return
        except RtcNotJoined:
            return
        except Exception:  # noqa: BLE001
            logger.exception("audio_bridge_downstream_unexpected_error")


def _now_ms() -> int:
    """Monotonic milliseconds for outbound PCM timestamping.

    Module-level so tests can monkeypatch a deterministic clock if
    they want to assert frame timestamps.
    """
    import time

    return int(time.monotonic() * 1000)


# Re-export at module scope so tests can import the helper.
__all__ = ["AudioBridge", "_Stats", "_now_ms"]


# Keep the otherwise-unused import for type narrowing in static checks.
_ = field
