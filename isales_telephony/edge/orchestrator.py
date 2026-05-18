"""EdgeOrchestrator: bridges Cloud2Edge gRPC traffic to AT + AudioBridge.

Spec: arch-cloud-edge-split / service-communication § Requirement: 云-边
控制面 — "Cloud2Edge.DialCommand → 边缘 AT 拨号 + audio-bridge 入会";
device-hardware § Requirement: audio-bridge 组件; design.md Decision 2 +
Decision 5 (engine session co-location, single in-process dispatcher).

One :class:`EdgeOrchestrator` per edge process. Lifecycle of one call:

1. ``Cloud2Edge.DialCommand`` arrives on the gRPC client callback.
2. The orchestrator constructs a :class:`_CallContext` (RTC session +
   bridge + ring buffers + capture/playback pumps + AT event pump).
3. AT ``dial(number)`` returns ``(call_id, event_iterator)``. We emit
   ``DialAck(accepted=true)`` immediately so the cloud's scheduler can
   retire its in-flight ``Dial`` timer (DialCommand is at-most-once).
4. The AT event iterator yields ``connected`` → emit
   ``CallEvent(connected)``. Audio-bridge join + pumps are started on
   ``connected``, not on dial, so the RTC channel only spins up for
   calls that actually go through.
5. AT yields ``remote_hangup`` (URC or manual hangup) → emit
   ``CallEvent(remote_hangup)`` with canonical :class:`HangupCause` →
   tear down audio-bridge + pumps → release the call slot.
6. ``Cloud2Edge.CancelCommand`` for an active call → call
   :meth:`ATClient.hangup`; the rest of the teardown happens via the
   resulting ``remote_hangup`` ATEvent (single termination path).

Concurrency model: a single-modem edge handles ONE call at a time.
This is enforced by the AT client (``SerialATClient`` raises on
concurrent dial) but the orchestrator also keeps a ``call_id → context``
dict so multiple dial commands (e.g. retry after transient grpc error)
don't trample each other.

Failure modes vs the spec:

- AT dial raises (modem busy / port closed) → emit
  ``DialAck(accepted=false, reason=...)``. No CallEvent.
- AT yields ``remote_hangup`` *before* ``connected`` (no_answer / busy)
  → emit ``CallEvent(remote_hangup)`` without ever joining RTC. This
  matches the spec's "dial → no_answer never hits the audio path".
- gRPC client disconnected at the moment we emit a non-critical event
  → :class:`CloudEdgeClient.send(critical=False)` buffers it; flushed
  on reconnect. DialAck is sent ``critical=False`` too (the spec says
  "edge gives up on the cloud, cloud retries on its own"), so a
  permanently-down stream just buffers ACKs.
- AT hangup from CancelCommand races with a spontaneous URC hangup →
  the canceller wins or the URC wins; either way ``_pump_at_events``
  exits after the first ``remote_hangup`` and tears down once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from google.protobuf.timestamp_pb2 import Timestamp  # type: ignore[import-untyped]
from isales_common.proto import cloud_edge_pb2 as pb
from isales_common.transport.cloud_edge import CloudEdgeClient

from isales_common.audio.rtc import RtcSession

from isales_telephony.audio_bridge.bridge import AudioBridge
from isales_telephony.audio_bridge.ring_buffer import PcmRingBuffer
from isales_telephony.audio_bridge.session import MacosRtcSession
from isales_telephony.edge.audio_io import run_capture_pump, run_playback_pump
from isales_telephony.modem_controller.at_client import (
    ATClient,
    ATEvent,
    PcmEnableError,
)
from isales_telephony.modem_controller.audio_pipe import (
    CaptureBackend,
    PlaybackBackend,
)


# AT command stage label for the HardwareAlert emitted when AT+CPCMREG=1
# fails on call connect. Spec § "PCM 通道按 SIMCom AT 协议启停 (CPCMREG)"
# requires a HardwareAlert; we reuse the existing ``ModemInitFailed`` oneof
# kind (no proto bump required) and carry the failure-point in ``stage``.
PCM_ENABLE_FAILED_STAGE = "CPCMREG_ENABLE"

logger = logging.getLogger(__name__)


# Cloud → enum mapping for canonical AT hangup_cause strings (see
# drivers.HANGUP_CAUSE_MAP). Anything not in here falls back to
# UNSPECIFIED so the cloud worker can still classify via vendor_raw.
# `user_hangup` / `manual_hangup` are application-side per
# isales_common.enums.HangupCause; report as normal_clearing so the
# cloud worker's retry classifier treats them as non-retryable.
_HANGUP_CAUSE_TO_PROTO: dict[str, int] = {
    "no_answer": int(pb.HangupCause.HANGUP_CAUSE_NO_ANSWER),
    "user_busy": int(pb.HangupCause.HANGUP_CAUSE_USER_BUSY),
    "network_out_of_order": int(pb.HangupCause.HANGUP_CAUSE_NETWORK_OUT_OF_ORDER),
    "normal_clearing": int(pb.HangupCause.HANGUP_CAUSE_NORMAL_CLEARING),
    "call_rejected": int(pb.HangupCause.HANGUP_CAUSE_CALL_REJECTED),
    "user_hangup": int(pb.HangupCause.HANGUP_CAUSE_NORMAL_CLEARING),
    "manual_hangup": int(pb.HangupCause.HANGUP_CAUSE_NORMAL_CLEARING),
}


RtcSessionFactory = Callable[[], RtcSession]


#: Vendor-raw label stamped on Edge2Cloud.remote_hangup when dev mode
#: tears down via SIGINT / SIGTERM. The cloud worker classifies this
#: as a non-retryable terminate (see macos-artc-pyobjc-binding spec).
DEV_TERMINATE_CAUSE = "dev_terminate"


def _now_ts() -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(datetime.now(tz=UTC))
    return ts


@dataclass
class _CallContext:
    """Bookkeeping for one active call. Owned by the orchestrator."""

    call_id: str
    modem_call_id: str
    at_client: ATClient | None = None
    bridge: AudioBridge | None = None
    upstream: PcmRingBuffer | None = None
    downstream: PcmRingBuffer | None = None
    tasks: list[asyncio.Task[None]] = field(default_factory=list)
    # dev-no-modem path stashes the RtcSession directly (no AudioBridge
    # wrapping it, because there is no modem PCM stream to bridge to).
    # teardown() calls leave() on it like it would on bridge.
    rtc_session: RtcSession | None = None
    # True iff AT+CPCMREG=1 succeeded for this call and AT+CPCMREG=0 has
    # not been sent yet. Gates teardown's cpcmreg_disable so we don't
    # spam ERRORs on calls that never reached connected.
    cpcmreg_enabled: bool = False
    # True after _handle_remote_hangup has emitted the CallEvent. Used
    # to dedupe when the cpcmreg-enable failure path hangs up first
    # (sending CallEvent + initiating modem hangup) and the AT event
    # pump then sees the resulting manual_hangup URC.
    terminated: bool = False

    async def teardown(self) -> None:
        """Stop pumps, leave RTC, send CPCMREG=0, close ring buffers.

        Spec § "PCM 通道按 SIMCom AT 协议启停 (CPCMREG)" requires
        ``AT+CPCMREG=0`` after ``audio_bridge.leave_rtc()`` so the modem
        releases its audio DSP state. Disable failure is logged inside
        :meth:`SerialATClient.cpcmreg_disable` (warning only) — teardown
        must complete regardless.

        Idempotent: safe to call multiple times.
        """
        for task in self.tasks:
            if not task.done():
                task.cancel()
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.tasks.clear()
        if self.bridge is not None:
            with contextlib.suppress(Exception):
                await self.bridge.leave()
            self.bridge = None
        # dev-no-modem: rtc_session is owned directly, not via AudioBridge.
        if self.rtc_session is not None:
            with contextlib.suppress(Exception):
                await self.rtc_session.leave()
            self.rtc_session = None
        if self.cpcmreg_enabled and self.at_client is not None:
            self.cpcmreg_enabled = False
            with contextlib.suppress(Exception):
                await self.at_client.cpcmreg_disable()
        for ring in (self.upstream, self.downstream):
            if ring is not None and not ring.is_closed:
                with contextlib.suppress(Exception):
                    await ring.close()
        self.upstream = None
        self.downstream = None


class EdgeOrchestrator:
    """Glue between the cloud-edge gRPC client and the local modem path.

    Args:
        grpc_client: connected :class:`CloudEdgeClient` — orchestrator
            registers its message callback during :meth:`start` and
            sends DialAck / CallEvent / HardwareAlert through this.
        at_client: :class:`ATClient` for the local modem. Single-modem
            invariant: only one active call at a time.
        capture: modem-side PCM capture backend (8 kHz int16 mono).
            Lifetime ≥ orchestrator lifetime; we open / close it once
            per process, NOT per call (HW init is slow).
        playback: modem-side PCM playback backend, same lifetime.
        rtc_session_factory: produces a fresh :class:`RtcSession` per
            call. Defaults to :class:`MacosRtcSession` ()-constructor;
            tests inject paired loopback sessions.
        ring_capacity_bytes: per-call ring buffer cap. Default 32 KiB
            (~1 s @ 16 kHz mono int16) — well above the 200 ms spec
            floor for jitter absorption.
    """

    def __init__(
        self,
        *,
        grpc_client: CloudEdgeClient,
        at_client: ATClient | None = None,
        capture: CaptureBackend | None = None,
        playback: PlaybackBackend | None = None,
        rtc_session_factory: RtcSessionFactory | None = None,
        ring_capacity_bytes: int = 32 * 1024,
        dev_no_modem: bool = False,
        dev_channel: str | None = None,
        dev_uid: str | None = None,
        dev_peer_uid: str | None = None,
    ) -> None:
        self._grpc = grpc_client
        self._at = at_client
        self._capture = capture
        self._playback = playback
        self._rtc_factory: RtcSessionFactory = (
            rtc_session_factory if rtc_session_factory is not None else MacosRtcSession
        )
        self._ring_capacity = ring_capacity_bytes
        self._calls: dict[str, _CallContext] = {}
        # Audio capture / playback are HW-scoped, not per-call. The
        # active call's pumps are wired to the active call's ring
        # buffers; between calls the pumps drain to a sentinel ring
        # that is closed (so the pump exits cleanly). Per call we
        # re-create the pump tasks rather than gating capture on a
        # "call active?" flag — simpler reasoning, same cost.
        self._started = False
        # dev-no-modem mode (macOS dev / QA, see
        # macos-artc-pyobjc-binding spec). When True, the orchestrator
        # bypasses the modem stack entirely: no SerialATClient, no
        # CPCMREG, no capture / playback pumps. DialCommand is routed
        # straight into rtc_session.join. dev_channel / dev_uid /
        # dev_peer_uid are informational labels logged for correlation.
        self._dev_no_modem = dev_no_modem
        self._dev_channel = dev_channel
        self._dev_uid = dev_uid
        self._dev_peer_uid = dev_peer_uid
        if not dev_no_modem:
            if at_client is None or capture is None or playback is None:
                raise ValueError(
                    "EdgeOrchestrator requires at_client / capture / playback "
                    "in production mode; pass dev_no_modem=True for the macOS "
                    "dev / QA path."
                )

    # ===== Lifecycle =====================================================

    async def start(self) -> None:
        """Register the gRPC callback. Must be called after the gRPC
        client's :meth:`start` returned (i.e. the bidi stream is up or
        in reconnect)."""
        if self._started:
            raise RuntimeError("orchestrator already started")
        self._grpc.on_cloud_message(self._on_cloud_message)
        self._started = True

    async def stop(self) -> None:
        """Tear down all active calls + release HW. Idempotent."""
        # Snapshot keys: teardown mutates the dict.
        for call_id in list(self._calls.keys()):
            ctx = self._calls.pop(call_id, None)
            if ctx is not None:
                await ctx.teardown()
        # dev_no_modem mode has no HW backends to close.
        if self._capture is not None:
            with contextlib.suppress(Exception):
                await self._capture.close()
        if self._playback is not None:
            with contextlib.suppress(Exception):
                await self._playback.close()
        self._started = False

    # ===== gRPC inbound ==================================================

    async def _on_cloud_message(self, msg: pb.Cloud2Edge) -> None:
        """Dispatch a Cloud2Edge frame to the matching handler.

        Unknown payload kinds are logged and dropped — proto3 lets the
        cloud add new oneof branches without breaking older edges, so
        this is by-design forward compat.
        """
        kind = msg.WhichOneof("payload")
        if kind == "dial":
            await self._on_dial(msg.dial)
        elif kind == "cancel":
            await self._on_cancel(msg.cancel)
        elif kind == "heartbeat":
            # Cloud-originated heartbeat is liveness-only; the gRPC
            # client's connect-loop already counts it for reconnect
            # backoff resets. No orchestrator action.
            return
        elif kind in {"config_update", "rtc_credentials", "remote_diag"}:
            logger.info("cloud2edge_ignored_in_a2", extra={"kind": kind})
        else:
            logger.warning("cloud2edge_unknown_kind", extra={"kind": kind})

    # ===== Dial flow =====================================================

    async def _on_dial(self, dial: pb.DialCommand) -> None:
        call_id = dial.call_id
        if call_id in self._calls:
            # Idempotent: cloud may have retried because DialAck got
            # dropped. ACK again so the cloud's retry timer retires.
            logger.info(
                "dial_command_duplicate", extra={"call_id": call_id}
            )
            await self._send_dial_ack(call_id, accepted=True, reason="duplicate")
            return

        if self._dev_no_modem:
            await self._on_dial_dev_no_modem(dial)
            return

        assert self._at is not None  # validated in __init__
        try:
            modem_call_id, at_events = await self._at.dial(dial.number)
        except Exception as exc:  # noqa: BLE001 — surface as ACK failure
            logger.exception(
                "at_dial_failed",
                extra={"call_id": call_id, "number_len": len(dial.number)},
            )
            await self._send_dial_ack(
                call_id, accepted=False, reason=f"at_dial: {exc.__class__.__name__}"
            )
            return

        ctx = _CallContext(
            call_id=call_id,
            modem_call_id=modem_call_id,
            at_client=self._at,
        )
        self._calls[call_id] = ctx
        await self._send_dial_ack(call_id, accepted=True, reason="")

        # The AT event pump owns RTC bring-up (on connected) and the
        # CallEvent emission for the rest of the call's lifetime.
        ctx.tasks.append(
            asyncio.create_task(
                self._pump_at_events(dial, ctx, at_events),
                name=f"at_event_pump_{call_id}",
            )
        )

    async def _pump_at_events(
        self,
        dial: pb.DialCommand,
        ctx: _CallContext,
        events: AsyncIterator[ATEvent],
    ) -> None:
        try:
            async for ev in events:
                if ev.event == "connected":
                    await self._handle_connected(dial, ctx)
                elif ev.event == "remote_hangup":
                    await self._handle_remote_hangup(ctx, ev.cause)
                    return
                else:
                    logger.debug(
                        "at_event_other",
                        extra={"call_id": ctx.call_id, "event": ev.event},
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception(
                "at_event_pump_unexpected_error",
                extra={"call_id": ctx.call_id},
            )
        finally:
            # If we exited without seeing remote_hangup (e.g. stream
            # ended after connected without a hangup URC), still tear
            # down so we don't leak the RTC session.
            if ctx.call_id in self._calls:
                self._calls.pop(ctx.call_id, None)
                await ctx.teardown()

    async def _handle_connected(
        self,
        dial: pb.DialCommand,
        ctx: _CallContext,
    ) -> None:
        # 1. CallEvent(connected) — emit before RTC join so the cloud's
        #    state machine sees "connected" right when AT says so, even
        #    if RTC bring-up needs a few hundred ms.
        await self._send_call_event_connected(ctx.call_id)

        # 2. Enable the SerialPcm audio byte stream (SIMCom modems only
        #    emit PCM after AT+CPCMREG=1). Spec § "PCM 通道按 SIMCom AT
        #    协议启停": SHALL succeed before audio_bridge.join_rtc;
        #    failure SHALL emit HardwareAlert + hang up.
        try:
            await self._at.cpcmreg_enable()
        except PcmEnableError as exc:
            logger.warning(
                "cpcmreg_enable_failed",
                extra={"call_id": ctx.call_id, "detail": exc.detail},
            )
            await self._send_hardware_alert_pcm_enable_failed(exc.detail)
            # Treat as a hardware-class hangup so the cloud worker
            # routes to retry-followup rather than no-answer.
            await self._handle_remote_hangup(ctx, cause="network_out_of_order")
            # Best-effort modem hangup so the line doesn't stay off-hook.
            with contextlib.suppress(Exception):
                await self._at.hangup(ctx.modem_call_id)
            return
        ctx.cpcmreg_enabled = True

        # 3. Build per-call audio path.
        upstream = PcmRingBuffer(
            capacity_bytes=self._ring_capacity,
            name=f"modem_upstream_{ctx.call_id}",
        )
        downstream = PcmRingBuffer(
            capacity_bytes=self._ring_capacity,
            name=f"modem_downstream_{ctx.call_id}",
        )
        bridge = AudioBridge(
            rtc_session=self._rtc_factory(),
            modem_upstream=upstream,
            modem_downstream=downstream,
            peer_uid=dial.rtc_uid_engine,
        )
        ctx.upstream = upstream
        ctx.downstream = downstream
        ctx.bridge = bridge

        # 4. Start RTC + pumps. Capture pump runs for the lifetime of
        #    the call; the orchestrator's single capture / playback
        #    backends are bound to this call's rings.
        try:
            await bridge.join(
                channel=dial.rtc_channel,
                token=dial.rtc_token,
                uid=dial.rtc_uid_edge,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "audio_bridge_join_failed", extra={"call_id": ctx.call_id}
            )
            # Treat as a hangup so the cloud knows the call is dead.
            await self._handle_remote_hangup(ctx, cause="network_out_of_order")
            return

        ctx.tasks.append(
            asyncio.create_task(
                run_capture_pump(self._capture, upstream),
                name=f"capture_pump_{ctx.call_id}",
            )
        )
        ctx.tasks.append(
            asyncio.create_task(
                run_playback_pump(downstream, self._playback),
                name=f"playback_pump_{ctx.call_id}",
            )
        )

    # ===== Dev-no-modem flow (macOS dev / QA, no GSM modem) =============

    async def _on_dial_dev_no_modem(self, dial: pb.DialCommand) -> None:
        """Dev-mode dial: skip AT, go straight to RTC join.

        Spec: macos-artc-pyobjc-binding device-hardware §
        "macOS dev-no-modem orchestrator 路径".

        - Synthetic ``modem_call_id="dev-fake"``.
        - DialAck(accepted=true) emitted immediately.
        - CallEvent(connected) emitted right after (mac mic / speaker
          are handled by ARTC SDK default audio device routing).
        - rtc_session.join() uses the cloud-supplied rtc_channel /
          rtc_token / rtc_uid_edge (same as the modem path).
        - No CPCMREG, no capture / playback pumps, no AudioBridge.
        """
        call_id = dial.call_id
        ctx = _CallContext(call_id=call_id, modem_call_id="dev-fake")
        self._calls[call_id] = ctx
        await self._send_dial_ack(call_id, accepted=True, reason="")

        logger.info(
            "dev_no_modem_dial_accepted",
            extra={
                "call_id": call_id,
                "dev_channel_label": self._dev_channel,
                "dev_uid_label": self._dev_uid,
                "rtc_channel": dial.rtc_channel,
                "rtc_uid_edge": dial.rtc_uid_edge,
            },
        )

        # Emit CallEvent(connected) before RTC join so the cloud's
        # state machine sees "connected" promptly (mirrors real path).
        await self._send_call_event_connected(call_id)

        rtc_session = self._rtc_factory()
        ctx.rtc_session = rtc_session
        try:
            await rtc_session.join(
                channel=dial.rtc_channel,
                token=dial.rtc_token,
                uid=dial.rtc_uid_edge,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "dev_no_modem_rtc_join_failed", extra={"call_id": call_id},
            )
            await self._handle_remote_hangup(ctx, cause="network_out_of_order")
            return

    async def dev_terminate_active_calls(self) -> None:
        """Emit remote_hangup{dev_terminate} for every in-flight call.

        Invoked by the dev daemon's SIGINT / SIGTERM handler before
        :meth:`stop`. The CallEvent is non-critical (best-effort);
        :meth:`stop` will tear down the RTC sessions regardless.
        """
        for call_id in list(self._calls.keys()):
            ctx = self._calls.get(call_id)
            if ctx is None or ctx.terminated:
                continue
            await self._handle_remote_hangup(ctx, cause=DEV_TERMINATE_CAUSE)

    # ===== Hangup =======================================================

    async def _handle_remote_hangup(
        self, ctx: _CallContext, cause: str | None
    ) -> None:
        if ctx.terminated:
            # cpcmreg-enable failure path already emitted CallEvent +
            # teardown; the manual_hangup URC from our subsequent
            # at.hangup() round-trips through the pump and lands here
            # a second time. Suppress the duplicate.
            return
        ctx.terminated = True
        await self._send_call_event_remote_hangup(ctx.call_id, cause)
        # Pop before teardown so duplicate events from cancel/at race
        # don't observe a half-torn context.
        self._calls.pop(ctx.call_id, None)
        await ctx.teardown()

    # ===== Cancel flow ===================================================

    async def _on_cancel(self, cancel: pb.CancelCommand) -> None:
        ctx = self._calls.get(cancel.call_id)
        if ctx is None:
            # Idempotent: call already ended, or never existed. Spec
            # says cloud accepts loss of CancelCommand — silently ignore.
            logger.info(
                "cancel_command_no_active_call",
                extra={"call_id": cancel.call_id},
            )
            return
        if self._dev_no_modem:
            # No modem to hang up; tear down the RTC session directly
            # and emit remote_hangup so the cloud retires the call.
            await self._handle_remote_hangup(ctx, cause="manual_hangup")
            return
        assert self._at is not None  # validated in __init__
        try:
            await self._at.hangup(ctx.modem_call_id)
        except Exception:  # noqa: BLE001 — AT hangup failure must not
            # block teardown; the URC pump still gets remote_hangup or
            # the next process restart sweeps the AT layer.
            logger.exception(
                "at_hangup_failed", extra={"call_id": cancel.call_id}
            )

    # ===== Edge → Cloud emitters =========================================

    async def _send_dial_ack(
        self, call_id: str, *, accepted: bool, reason: str
    ) -> None:
        msg = pb.Edge2Cloud(
            dial_ack=pb.DialAck(
                call_id=call_id,
                accepted=accepted,
                reason=reason,
                ts=_now_ts(),
            )
        )
        # DialAck is non-critical: if the cloud doesn't get it within
        # its retry window it will redial; the duplicate-detection
        # branch in _on_dial covers that.
        await self._grpc.send(msg, critical=False)

    async def _send_call_event_connected(self, call_id: str) -> None:
        msg = pb.Edge2Cloud(
            call_event=pb.CallEvent(
                call_id=call_id,
                ts=_now_ts(),
                connected=pb.Connected(),
            )
        )
        await self._grpc.send(msg, critical=False)

    async def _send_hardware_alert_pcm_enable_failed(self, detail: str) -> None:
        """Emit a HardwareAlert when ``AT+CPCMREG=1`` fails.

        Spec § "PCM 通道按 SIMCom AT 协议启停 (CPCMREG)" calls this kind
        ``"pcm_enable_failed"`` in prose; the wire-level proto reuses
        the existing ``ModemInitFailed`` oneof with
        ``stage=PCM_ENABLE_FAILED_STAGE`` to avoid bumping
        ``isales-common`` for D1. Cloud-side workers classify by
        ``stage``; the value here is documented in design.md Decision 8.
        """
        msg = pb.Edge2Cloud(
            hardware_alert=pb.HardwareAlert(
                ts=_now_ts(),
                modem_init_failed=pb.ModemInitFailed(
                    stage=PCM_ENABLE_FAILED_STAGE,
                    detail=detail,
                ),
            )
        )
        # HardwareAlert is critical: we want the cloud to record the
        # failure even if the gRPC stream is currently in reconnect.
        await self._grpc.send(msg, critical=True)

    async def _send_call_event_remote_hangup(
        self, call_id: str, cause: str | None
    ) -> None:
        hc_int = _HANGUP_CAUSE_TO_PROTO.get(
            cause or "", int(pb.HangupCause.HANGUP_CAUSE_UNSPECIFIED)
        )
        msg = pb.Edge2Cloud(
            call_event=pb.CallEvent(
                call_id=call_id,
                ts=_now_ts(),
                remote_hangup=pb.RemoteHangup(
                    cause=pb.HangupCause.ValueType(hc_int),
                    vendor_raw=cause or "",
                ),
            )
        )
        await self._grpc.send(msg, critical=False)

    # ===== Introspection (test hooks) ====================================

    @property
    def active_call_ids(self) -> tuple[str, ...]:
        return tuple(self._calls.keys())


__all__ = ["EdgeOrchestrator"]
