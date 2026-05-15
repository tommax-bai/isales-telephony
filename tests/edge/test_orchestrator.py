"""Tests for :class:`EdgeOrchestrator`.

Spec: arch-cloud-edge-split task 7.1 + 7.3 — Cloud2Edge.DialCommand →
AT dial → DialAck → on connected: bridge join + audio pumps → on
remote_hangup: CallEvent + teardown. CancelCommand → AT hangup →
remote_hangup path. Single in-process dispatcher (no Unix socket).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from isales_common.proto import cloud_edge_pb2 as pb
from isales_common.transport.cloud_edge import CloudEdgeClient, CloudMessageCallback

from isales_telephony.audio_bridge.session import MacosRtcSession
from isales_telephony.edge.orchestrator import EdgeOrchestrator
from isales_telephony.modem_controller.at_client import ATEvent

# ---------- Fakes -----------------------------------------------------------


class _FakeGrpcClient(CloudEdgeClient):
    """In-memory CloudEdgeClient. Records ``send`` calls; pushes
    inbound frames via :meth:`push_from_cloud`. Always 'connected'."""

    def __init__(self) -> None:
        self.sent: list[pb.Edge2Cloud] = []
        self._callback: CloudMessageCallback | None = None

    async def start(self, endpoint: str, token: str) -> None:  # pragma: no cover
        return

    async def stop(self) -> None:  # pragma: no cover
        return

    async def send(
        self, message: pb.Edge2Cloud, *, critical: bool = False
    ) -> None:
        self.sent.append(message)

    def on_cloud_message(self, callback: CloudMessageCallback) -> None:
        self._callback = callback

    @property
    def is_connected(self) -> bool:
        return True

    async def push_from_cloud(self, msg: pb.Cloud2Edge) -> None:
        assert self._callback is not None, "callback not registered"
        await self._callback(msg)


class _ScriptedATClient:
    """Plays back a scripted ATEvent sequence per dial call.

    Each ``dial(number)`` pops the next script entry; ``hangup(call_id)``
    appends a synthetic ``remote_hangup(manual_hangup)`` to the live
    queue (simulating URC after AT+H succeeds).
    """

    def __init__(self, scripts: list[list[ATEvent]]) -> None:
        self._scripts = scripts
        self.dials: list[str] = []
        self.hangups: list[str] = []
        self._queues: dict[str, asyncio.Queue[ATEvent | None]] = {}
        self._call_counter = 0

    async def dial(self, number: str) -> tuple[str, AsyncIterator[ATEvent]]:
        self.dials.append(number)
        if not self._scripts:
            raise RuntimeError("script exhausted")
        events = self._scripts.pop(0)
        self._call_counter += 1
        call_id = f"modem-{self._call_counter}"
        q: asyncio.Queue[ATEvent | None] = asyncio.Queue()
        self._queues[call_id] = q
        for ev in events:
            # Re-tag the scripted event with the modem-side call_id
            # the orchestrator just received.
            await q.put(
                ATEvent(event=ev.event, call_id=call_id, cause=ev.cause)
            )
        return call_id, self._drain(call_id)

    async def _drain(self, call_id: str) -> AsyncIterator[ATEvent]:
        q = self._queues[call_id]
        while True:
            ev = await q.get()
            if ev is None:
                return
            yield ev
            if ev.event == "remote_hangup":
                return

    async def hangup(self, call_id: str) -> None:
        self.hangups.append(call_id)
        q = self._queues.get(call_id)
        if q is not None:
            await q.put(
                ATEvent(event="remote_hangup", call_id=call_id, cause="manual_hangup")
            )


class _NullCapture:
    """Yields zeros forever (until cancelled). Mimics a real modem mic
    that has no EOF — the orchestrator must tear it down on call end."""

    async def read_chunk(self) -> bytes:
        await asyncio.sleep(0.05)
        return b"\x00" * 320

    async def close(self) -> None:
        return


class _NullPlayback:
    def __init__(self) -> None:
        self.received_bytes = 0

    async def write_chunk(self, pcm: bytes) -> None:
        self.received_bytes += len(pcm)

    async def close(self) -> None:
        return


# ---------- Helpers ---------------------------------------------------------


def _dial_command(*, call_id: str = "c1", number: str = "+15551234567") -> pb.Cloud2Edge:
    return pb.Cloud2Edge(
        dial=pb.DialCommand(
            call_id=call_id,
            device_id=42,
            number=number,
            caller_id="+15550000000",
            rtc_channel=call_id,
            rtc_token="rtc-token",
            rtc_uid_edge=f"edge-{call_id}",
            rtc_uid_engine=f"engine-{call_id}",
        )
    )


def _cancel_command(call_id: str = "c1") -> pb.Cloud2Edge:
    return pb.Cloud2Edge(cancel=pb.CancelCommand(call_id=call_id, reason="boss_hangup"))


def _kinds(frames: list[pb.Edge2Cloud]) -> list[str]:
    out: list[str] = []
    for f in frames:
        kind = f.WhichOneof("payload")
        if kind == "call_event":
            out.append(f"call_event:{f.call_event.WhichOneof('kind')}")
        else:
            out.append(kind or "<none>")
    return out


def _build_orchestrator(
    at_scripts: list[list[ATEvent]],
    *,
    rtc_factory=None,
) -> tuple[EdgeOrchestrator, _FakeGrpcClient, _ScriptedATClient, _NullPlayback]:
    grpc = _FakeGrpcClient()
    at_client = _ScriptedATClient(at_scripts)
    capture = _NullCapture()
    playback = _NullPlayback()
    orch = EdgeOrchestrator(
        grpc_client=grpc,
        at_client=at_client,  # type: ignore[arg-type]  # Protocol-compat
        capture=capture,
        playback=playback,
        rtc_session_factory=rtc_factory or MacosRtcSession,
    )
    return orch, grpc, at_client, playback


# ---------- Tests -----------------------------------------------------------


@pytest.mark.asyncio
async def test_dial_then_connected_then_remote_hangup() -> None:
    """Happy path: dial → connected → remote_hangup; orchestrator
    emits DialAck + CallEvent(connected) + CallEvent(remote_hangup)
    and tears down the per-call context."""
    script = [
        [
            ATEvent(event="connected", call_id=""),
            ATEvent(event="remote_hangup", call_id="", cause="normal_clearing"),
        ]
    ]
    orch, grpc, at, _ = _build_orchestrator(script)
    await orch.start()
    await grpc.push_from_cloud(_dial_command())

    # The AT script delivers events synchronously into a queue; the
    # orchestrator's event pump consumes them on the next loop tick.
    for _ in range(50):
        if "call_event:remote_hangup" in _kinds(grpc.sent):
            break
        await asyncio.sleep(0.01)

    kinds = _kinds(grpc.sent)
    assert kinds == [
        "dial_ack",
        "call_event:connected",
        "call_event:remote_hangup",
    ], kinds
    # remote_hangup must carry the canonical proto enum.
    hangup = grpc.sent[2].call_event.remote_hangup
    assert hangup.cause == pb.HangupCause.HANGUP_CAUSE_NORMAL_CLEARING
    assert hangup.vendor_raw == "normal_clearing"
    # Call slot is released.
    assert orch.active_call_ids == ()
    await orch.stop()


@pytest.mark.asyncio
async def test_dial_no_answer_skips_audio_bridge() -> None:
    """Dial returns remote_hangup without connected. Audio bridge
    must NOT have been joined (spec: no audio path for dropped calls)."""
    script = [
        [ATEvent(event="remote_hangup", call_id="", cause="no_answer")]
    ]
    orch, grpc, at, playback = _build_orchestrator(script)
    await orch.start()
    await grpc.push_from_cloud(_dial_command())

    for _ in range(50):
        if "call_event:remote_hangup" in _kinds(grpc.sent):
            break
        await asyncio.sleep(0.01)

    kinds = _kinds(grpc.sent)
    assert kinds == ["dial_ack", "call_event:remote_hangup"]
    hangup = grpc.sent[1].call_event.remote_hangup
    assert hangup.cause == pb.HangupCause.HANGUP_CAUSE_NO_ANSWER
    # No audio was ever written.
    assert playback.received_bytes == 0
    await orch.stop()


@pytest.mark.asyncio
async def test_cancel_command_triggers_hangup() -> None:
    """CancelCommand for an active call calls AT hangup; subsequent
    URC remote_hangup tears the call down."""
    # Script: emit connected, then wait for canceller (no more events
    # pushed; the ScriptedATClient's hangup() injects remote_hangup).
    script = [[ATEvent(event="connected", call_id="")]]
    orch, grpc, at, _ = _build_orchestrator(script)
    await orch.start()
    await grpc.push_from_cloud(_dial_command(call_id="c1"))

    for _ in range(50):
        if "call_event:connected" in _kinds(grpc.sent):
            break
        await asyncio.sleep(0.01)
    assert "call_event:connected" in _kinds(grpc.sent)
    assert orch.active_call_ids == ("c1",)

    await grpc.push_from_cloud(_cancel_command("c1"))
    for _ in range(50):
        if "call_event:remote_hangup" in _kinds(grpc.sent):
            break
        await asyncio.sleep(0.01)

    assert len(at.hangups) == 1
    assert orch.active_call_ids == ()
    await orch.stop()


@pytest.mark.asyncio
async def test_cancel_for_unknown_call_is_silent_noop() -> None:
    """CancelCommand for a call we don't know about is ignored
    silently — the spec says cancel loss is acceptable."""
    orch, grpc, at, _ = _build_orchestrator([])
    await orch.start()
    await grpc.push_from_cloud(_cancel_command("ghost"))
    await asyncio.sleep(0.01)
    assert at.hangups == []
    assert grpc.sent == []
    await orch.stop()


@pytest.mark.asyncio
async def test_duplicate_dial_acks_again() -> None:
    """Duplicate DialCommand (cloud retry after dropped ACK) gets
    another DialAck without spawning a second AT dial."""
    # Long-lived call so the duplicate arrives while still active.
    script = [[ATEvent(event="connected", call_id="")]]
    orch, grpc, at, _ = _build_orchestrator(script)
    await orch.start()
    await grpc.push_from_cloud(_dial_command(call_id="c1"))
    for _ in range(50):
        if orch.active_call_ids == ("c1",):
            break
        await asyncio.sleep(0.01)
    await grpc.push_from_cloud(_dial_command(call_id="c1"))
    await asyncio.sleep(0.01)
    # Two DialAcks, one AT dial.
    dial_acks = [f for f in grpc.sent if f.WhichOneof("payload") == "dial_ack"]
    assert len(dial_acks) == 2
    assert len(at.dials) == 1
    assert dial_acks[1].dial_ack.reason == "duplicate"
    await orch.stop()


@pytest.mark.asyncio
async def test_dial_at_failure_negative_ack() -> None:
    """AT dial raising (e.g. modem busy) yields DialAck(accepted=false)
    and never registers a call context."""

    class _BrokenAT:
        dials: list[str] = []
        hangups: list[str] = []

        async def dial(self, number: str):  # type: ignore[no-untyped-def]
            self.dials.append(number)
            raise RuntimeError("modem busy")

        async def hangup(self, call_id: str) -> None:  # pragma: no cover
            self.hangups.append(call_id)

    grpc = _FakeGrpcClient()
    at = _BrokenAT()
    orch = EdgeOrchestrator(
        grpc_client=grpc,
        at_client=at,  # type: ignore[arg-type]
        capture=_NullCapture(),
        playback=_NullPlayback(),
    )
    await orch.start()
    await grpc.push_from_cloud(_dial_command())
    await asyncio.sleep(0.01)
    assert len(grpc.sent) == 1
    ack = grpc.sent[0].dial_ack
    assert ack.accepted is False
    assert "at_dial" in ack.reason
    assert orch.active_call_ids == ()
    await orch.stop()
