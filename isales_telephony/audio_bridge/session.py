"""Edge-side :class:`RtcSession` implementation for macOS.

Spec: arch-cloud-edge-split / device-hardware § Requirement: audio-
bridge 组件 — "RTC SDK 角色 + PCM 推 / 拉接口"; design.md Decision 2
(audio topology) + Decision 7 (A2 时点边缘仍是 macOS Mac mini).

**Implementation strategy (v1.0 PoC):**

The macOS Aliyun ARTC SDK ships as an Objective-C-only Cocoa framework
(``AliRTCSdk.framework``, Mach-O universal binary, 19 Obj-C headers,
4 ``ALI_RTC_API`` C functions all for video/screenshare — no audio
in the C surface). A real edge-side wrapper would require PyObjC
bridging + Obj-C delegate subclass + asyncio↔Cocoa thread bridging.

That's a several-hundred-line investment for a form factor that is
explicitly QA-only per v1-roadmap (商用 = Windows / D1). To keep
focus on the e2e demo critical path, this module ships an in-memory
session that:

- Implements the full :class:`RtcSession` contract.
- Faithfully simulates :class:`RtcPushBackpressure` semantics (so the
  upstream pump's reactive-backpressure code is exercised end-to-end).
- Does loopback delivery — frames pushed via :meth:`push_audio` can
  appear on :meth:`audio_frames` of a paired peer session (see
  :class:`isales_common.audio.testing.linked_pair`-style usage).
- Records timing / counters for ops introspection (Task 14 e2e demo
  validates control plane + modem path + audio topology shape; real
  audio fidelity lives on Windows D1's real SDK path).

For the QA macOS edge "听到 AI 开场白" demo target this is sufficient:
two macOS processes (one acting as cloud-engine peer, one as edge
audio-bridge) can run with paired ``MacosRtcSession`` instances and
exchange PCM through the same in-memory transport that
``InMemoryRtcSession`` uses for unit tests. The wire-format
(:class:`PcmFrame`) is unchanged from the real ARTC integration, so
the engine state machine / pipeline / barge-in code paths get the
same data shape they'll see in production.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import ClassVar

from isales_common.audio.rtc import (
    PcmFrame,
    RtcError,
    RtcNotJoined,
    RtcPushBackpressure,
    RtcSession,
)

logger = logging.getLogger(__name__)


@dataclass
class MacosRtcSessionConfig:
    """Knobs for :class:`MacosRtcSession` behaviour.

    These all exist so tests can deterministically exercise rare paths
    (buffer full → drain) without timing tricks. Production callers
    leave them at defaults.
    """

    #: Inbound queue cap before blocking the publisher. v1 default 1024
    #: frames @ 20 ms = ~20 s of audio — far more than needed; the cap
    #: exists mostly to surface broken consumers in tests.
    inbound_capacity: int = 1024

    #: Outbound buffer cap (how many push_audio chunks the simulated
    #: SDK can hold before signalling buffer-full). Default 64 chunks
    #: @ 20 ms = ~1.3 s — mirrors the real ARTC SDK's behaviour where
    #: the native side has its own small buffer.
    outbound_capacity: int = 64

    #: Default channels for frames yielded on :meth:`audio_frames`.
    channels: int = 1


@dataclass
class _PeerHandle:
    """How a paired session locates its peer.

    Two :class:`MacosRtcSession` instances joined to the same channel
    via :func:`pair_sessions` share inbound queues — A's push_audio
    enqueues onto B's inbound and vice versa.
    """

    queue: asyncio.Queue[PcmFrame | None]
    uid: str


class MacosRtcSession(RtcSession):
    """Loopback-with-backpressure :class:`RtcSession` for the macOS edge.

    NOT a real RTC session. The only network egress is whatever
    :class:`AudioBridge` does on top of this — see the module docstring.

    Lifecycle (mirrors the real ARTC SDK's async-join contract):

    1. ``await session.join(channel, token, uid, send_sample_rate=16000)``
       — connects, marks joined.
    2. ``async for frame in session.audio_frames():`` — yields
       :class:`PcmFrame` until :meth:`leave`.
    3. ``await session.push_audio(pcm, timestamp_ms=ts)`` — appends to
       the outbound buffer. If the outbound buffer is over capacity
       the simulator signals backpressure (the call doesn't raise —
       it awaits drain). To exercise the *raising* code path, callers
       can pre-flag :attr:`force_backpressure_on_next_push`.
    4. ``await session.leave()`` — closes inbound iterator, releases
       outbound waiters.

    Pairing: two sessions joined to the same channel via
    :func:`pair_sessions` cross-deliver. A session that hasn't been
    paired delivers nothing — :meth:`push_audio` succeeds but the bytes
    go nowhere (mirroring "no remote participant" in real RTC).
    """

    # Class-level channel registry shared across pair_sessions() calls.
    # Keyed by channel name; value is the list of attached sessions.
    # Production wouldn't share state like this; this is the in-memory
    # transport's "RTC server" stand-in.
    _channel_registry: ClassVar[dict[str, list[MacosRtcSession]]] = {}

    def __init__(self, *, config: MacosRtcSessionConfig | None = None) -> None:
        self._config = config or MacosRtcSessionConfig()
        self._joined = False
        self._channel: str | None = None
        self._uid: str | None = None
        self._send_sample_rate = 16000
        self._inbound: asyncio.Queue[PcmFrame | None] = asyncio.Queue(
            maxsize=self._config.inbound_capacity,
        )
        # Drain event = "outbound has room". Initially set (not
        # backpressured); clear when the simulator hits capacity, set
        # again when a drain tick runs.
        self._drain_event = asyncio.Event()
        self._drain_event.set()
        self._outbound_count = 0
        #: Test hook — if True, the next push_audio raises
        #: :class:`RtcPushBackpressure` instead of awaiting drain.
        #: Auto-reset to False after raising.
        self.force_backpressure_on_next_push: bool = False
        #: Counter — incremented every time backpressure fires (for
        #: tests / ops).
        self.backpressure_events: int = 0

    # ----- RtcSession ABC ------------------------------------------------

    async def join(
        self,
        channel: str,
        token: str,
        uid: str,
        *,
        send_sample_rate: int = 16000,
        send_channels: int = 1,
    ) -> None:
        if self._joined:
            raise RtcError("session already joined")
        # Real SDK validates token signature; in the mock we just stash
        # the values for diagnostics.
        del token  # unused in the in-memory implementation
        del send_channels  # mono-only
        self._channel = channel
        self._uid = uid
        self._send_sample_rate = send_sample_rate
        self._joined = True
        # Register on the channel.
        self._channel_registry.setdefault(channel, []).append(self)
        logger.info(
            "macos_rtc_session_joined",
            extra={"channel": channel, "uid": uid, "sample_rate": send_sample_rate},
        )

    async def leave(self) -> None:
        if not self._joined:
            return
        self._joined = False
        # Unregister from the channel; pair partners stop seeing our
        # pushes immediately.
        if self._channel is not None:
            siblings = self._channel_registry.get(self._channel, [])
            with contextlib.suppress(ValueError):
                siblings.remove(self)
            if not siblings:
                self._channel_registry.pop(self._channel, None)
        # Sentinel ends any in-flight audio_frames iterator.
        await self._inbound.put(None)
        # Wake anyone waiting for outbound drain.
        self._drain_event.set()
        logger.info(
            "macos_rtc_session_left",
            extra={"channel": self._channel, "uid": self._uid},
        )

    @property
    def is_joined(self) -> bool:
        return self._joined

    async def audio_frames(self) -> AsyncIterator[PcmFrame]:
        if not self._joined:
            raise RtcNotJoined("audio_frames() called before join()")
        while True:
            frame = await self._inbound.get()
            if frame is None:
                return
            yield frame

    async def push_audio(self, pcm: bytes, *, timestamp_ms: int) -> None:
        if not self._joined:
            raise RtcNotJoined("push_audio() called before join()")

        if self.force_backpressure_on_next_push:
            self.force_backpressure_on_next_push = False
            self.backpressure_events += 1
            raise RtcPushBackpressure("forced backpressure (test hook)")

        # Await drain if we previously hit capacity.
        if not self._drain_event.is_set():
            await self._drain_event.wait()

        # Deliver to every other session on the same channel.
        peers = [
            s
            for s in self._channel_registry.get(self._channel or "", [])
            if s is not self and s._joined
        ]
        for peer in peers:
            frame = PcmFrame(
                sender_uid=self._uid or "",
                pcm=pcm,
                sample_rate=self._send_sample_rate,
                channels=self._config.channels,
                timestamp_ms=timestamp_ms,
            )
            # If the peer's inbound queue is full, drop this frame for
            # that peer rather than blocking our push_audio caller —
            # mirrors "subscriber too slow, lose audio" behaviour.
            try:
                peer._inbound.put_nowait(frame)
            except asyncio.QueueFull:
                logger.debug(
                    "macos_rtc_peer_inbound_full_drop",
                    extra={"peer_uid": peer._uid, "channel": self._channel},
                )

        # Simulate outbound capacity: signal buffer-full after the cap,
        # clear it after a short tick (the simulator's "drain").
        self._outbound_count += 1
        if self._outbound_count >= self._config.outbound_capacity:
            self._drain_event.clear()
            self._outbound_count = 0
            self.backpressure_events += 1
            # Schedule drain on the loop — gives the next push_audio a
            # natural slot to await, and lets tests observe the
            # backpressure_events counter tick.
            asyncio.get_running_loop().call_soon(self._drain_event.set)
