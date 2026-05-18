"""Tests for :class:`EdgeOrchestrator` dev-no-modem path.

Spec: openspec/changes/macos-artc-pyobjc-binding/specs/device-hardware/
§ Requirement: macOS dev-no-modem orchestrator 路径.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from isales_common.audio.rtc import PcmFrame, RtcSession
from isales_common.proto import cloud_edge_pb2 as pb
from isales_common.transport.cloud_edge import CloudEdgeClient, CloudMessageCallback

from isales_telephony.edge.orchestrator import DEV_TERMINATE_CAUSE, EdgeOrchestrator


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeGrpcClient(CloudEdgeClient):
    def __init__(self) -> None:
        self.sent: list[pb.Edge2Cloud] = []
        self._callback: CloudMessageCallback | None = None

    async def start(self, endpoint: str, token: str) -> None:  # pragma: no cover
        return

    async def stop(self) -> None:  # pragma: no cover
        return

    async def send(self, message: pb.Edge2Cloud, *, critical: bool = False) -> None:
        self.sent.append(message)

    def on_cloud_message(self, callback: CloudMessageCallback) -> None:
        self._callback = callback

    @property
    def is_connected(self) -> bool:
        return True

    async def push_from_cloud(self, msg: pb.Cloud2Edge) -> None:
        assert self._callback is not None, "callback not registered"
        await self._callback(msg)


class _FakeRtcSession(RtcSession):
    """Records join / leave calls. Never produces audio frames."""

    def __init__(self) -> None:
        self.joined: tuple[str, str, str] | None = None
        self.leave_calls = 0
        self.join_raises: BaseException | None = None
        self._is_joined = False

    @property
    def is_joined(self) -> bool:
        return self._is_joined

    async def join(
        self,
        channel: str,
        token: str,
        uid: str,
        *,
        send_sample_rate: int = 16000,
        send_channels: int = 1,
    ) -> None:
        if self.join_raises:
            raise self.join_raises
        self.joined = (channel, token, uid)
        self._is_joined = True

    async def leave(self) -> None:
        self.leave_calls += 1
        self._is_joined = False

    async def audio_frames(self) -> AsyncIterator[PcmFrame]:  # pragma: no cover
        if False:
            yield  # type: ignore[unreachable]

    async def push_audio(self, pcm: bytes, *, timestamp_ms: int) -> None:  # pragma: no cover
        return


class _RtcFactory:
    """Captures every produced session for inspection."""

    def __init__(self) -> None:
        self.sessions: list[_FakeRtcSession] = []
        self.next_session_raises: BaseException | None = None

    def __call__(self) -> _FakeRtcSession:
        sess = _FakeRtcSession()
        if self.next_session_raises is not None:
            sess.join_raises = self.next_session_raises
            self.next_session_raises = None
        self.sessions.append(sess)
        return sess


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dial(call_id: str = "call-1") -> pb.Cloud2Edge:
    return pb.Cloud2Edge(
        dial=pb.DialCommand(
            call_id=call_id,
            number="+8612300000000",
            rtc_channel=f"rtc-{call_id}",
            rtc_token="rtc-tok",
            rtc_uid_edge=f"edge-{call_id}",
            rtc_uid_engine=f"engine-{call_id}",
        )
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_init_requires_modem_backends_in_production_mode():
    grpc = _FakeGrpcClient()
    with pytest.raises(ValueError, match="dev_no_modem=True"):
        EdgeOrchestrator(grpc_client=grpc)


def test_init_dev_mode_skips_modem_requirement():
    grpc = _FakeGrpcClient()
    orch = EdgeOrchestrator(
        grpc_client=grpc,
        rtc_session_factory=_RtcFactory(),
        dev_no_modem=True,
        dev_channel="demo-mac-01",
        dev_uid="mac-dev-01",
        dev_peer_uid="engine",
    )
    assert orch._dev_no_modem is True
    assert orch._at is None


@pytest.mark.asyncio
async def test_dial_in_dev_mode_skips_at_and_joins_rtc():
    grpc = _FakeGrpcClient()
    factory = _RtcFactory()
    orch = EdgeOrchestrator(
        grpc_client=grpc, rtc_session_factory=factory, dev_no_modem=True,
    )
    await orch.start()

    await grpc.push_from_cloud(_make_dial("call-1"))

    # DialAck + CallEvent(connected) emitted.
    kinds = [m.WhichOneof("payload") for m in grpc.sent]
    assert kinds == ["dial_ack", "call_event"]
    assert grpc.sent[0].dial_ack.accepted is True
    assert grpc.sent[1].call_event.WhichOneof("kind") == "connected"

    # Exactly one RtcSession created and joined with the cloud-supplied
    # rtc_channel / rtc_token / rtc_uid_edge.
    assert len(factory.sessions) == 1
    assert factory.sessions[0].joined == ("rtc-call-1", "rtc-tok", "edge-call-1")

    # Active call tracked.
    assert orch.active_call_ids == ("call-1",)

    await orch.stop()
    assert factory.sessions[0].leave_calls == 1


@pytest.mark.asyncio
async def test_dial_failed_rtc_join_emits_hangup():
    grpc = _FakeGrpcClient()
    factory = _RtcFactory()
    factory.next_session_raises = RuntimeError("network down")
    orch = EdgeOrchestrator(
        grpc_client=grpc, rtc_session_factory=factory, dev_no_modem=True,
    )
    await orch.start()
    await grpc.push_from_cloud(_make_dial("call-1"))

    kinds = [m.WhichOneof("payload") for m in grpc.sent]
    # dial_ack + connected (still emitted before the failed join) +
    # remote_hangup.
    assert kinds == ["dial_ack", "call_event", "call_event"]
    hangup = grpc.sent[-1].call_event
    assert hangup.WhichOneof("kind") == "remote_hangup"
    assert hangup.remote_hangup.vendor_raw == "network_out_of_order"
    assert orch.active_call_ids == ()
    await orch.stop()


@pytest.mark.asyncio
async def test_cancel_in_dev_mode_emits_manual_hangup_and_leaves():
    grpc = _FakeGrpcClient()
    factory = _RtcFactory()
    orch = EdgeOrchestrator(
        grpc_client=grpc, rtc_session_factory=factory, dev_no_modem=True,
    )
    await orch.start()
    await grpc.push_from_cloud(_make_dial("call-1"))
    await grpc.push_from_cloud(
        pb.Cloud2Edge(cancel=pb.CancelCommand(call_id="call-1")),
    )

    hangup_events = [
        m for m in grpc.sent
        if m.WhichOneof("payload") == "call_event"
        and m.call_event.WhichOneof("kind") == "remote_hangup"
    ]
    assert len(hangup_events) == 1
    assert hangup_events[0].call_event.remote_hangup.vendor_raw == "manual_hangup"
    assert factory.sessions[0].leave_calls == 1
    assert orch.active_call_ids == ()
    await orch.stop()


@pytest.mark.asyncio
async def test_dev_terminate_emits_remote_hangup_and_leaves():
    grpc = _FakeGrpcClient()
    factory = _RtcFactory()
    orch = EdgeOrchestrator(
        grpc_client=grpc, rtc_session_factory=factory, dev_no_modem=True,
    )
    await orch.start()
    await grpc.push_from_cloud(_make_dial("call-1"))

    sent_before = len(grpc.sent)
    await orch.dev_terminate_active_calls()

    new = grpc.sent[sent_before:]
    assert len(new) == 1
    ev = new[0].call_event
    assert ev.WhichOneof("kind") == "remote_hangup"
    assert ev.remote_hangup.vendor_raw == DEV_TERMINATE_CAUSE
    assert factory.sessions[0].leave_calls == 1
    assert orch.active_call_ids == ()
    await orch.stop()


@pytest.mark.asyncio
async def test_stop_in_dev_mode_tears_down_active_sessions():
    grpc = _FakeGrpcClient()
    factory = _RtcFactory()
    orch = EdgeOrchestrator(
        grpc_client=grpc, rtc_session_factory=factory, dev_no_modem=True,
    )
    await orch.start()
    await grpc.push_from_cloud(_make_dial("call-1"))
    assert factory.sessions[0].leave_calls == 0
    await orch.stop()
    assert factory.sessions[0].leave_calls == 1
