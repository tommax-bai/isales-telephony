"""Edge-client status model + cross-thread bus.

Spec: windows-client-core / deployment-topology § "Tray UX 二态".

D1 keeps the tray icon binary (green / red); three-state + per-alert-kind
notifications are D2 `hardware-observability` territory.

Design
------

The Windows edge process has three state producers that live on the
asyncio event loop:

- ``CloudEdgeGrpcClient`` — emits "connected" / "disconnected" transitions.
- ``modem-controller`` — emits per-modem ``idle`` / ``offline`` transitions.
- Activation flow — emits "no token yet" when the env file is missing,
  flipping to a normal state once the user pastes a token.

The tray icon (pystray, running in its own daemon thread) and any open
PySide6 diagnostic windows are state consumers. They MUST NOT poll
asyncio state directly — pystray's thread doesn't share an event loop
with asyncio, and Qt widgets must update on the Qt main thread.

``StateBus`` is the bridge: producers call :meth:`StateBus.set` from
asyncio tasks; subscribers (tray icon callback, Qt signal adaptors)
register via :meth:`subscribe` to get pushed updates. The bus is
thread-safe — it uses a plain ``threading.Lock`` (NOT asyncio.Lock) so
the pystray daemon thread can also publish (e.g. "user clicked Quit").

The state machine is intentionally simple — green / red — to mirror the
spec literal "二态". Adding "yellow / warning" requires a new value here
and is the D2 milestone, not a D1 follow-up.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import Enum

logger = logging.getLogger(__name__)


class TrayColor(Enum):
    """Binary tray icon state per D1 spec."""

    GREEN = "green"
    RED = "red"


class CloudLinkState(Enum):
    """gRPC cloud-edge connection state, reflected to the tray."""

    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    AWAITING_ACTIVATION = "awaiting_activation"  # no token yet
    AUTH_REJECTED = "auth_rejected"  # token recognised invalid by server


class ModemState(Enum):
    """Per-modem aggregate state. Mirrors A2 modem-controller states
    compressed to what the tray needs to know (full state machine still
    lives in modem-controller; here we only track "is at least one modem
    usable").
    """

    IDLE = "idle"
    BUSY = "busy"
    ERROR = "error"
    OFFLINE = "offline"


@dataclass(frozen=True, slots=True)
class ModemSummary:
    device_node: str
    state: ModemState


@dataclass(frozen=True, slots=True)
class EdgeStatus:
    """Snapshot of edge-client state used by all UI surfaces."""

    cloud_link: CloudLinkState = CloudLinkState.AWAITING_ACTIVATION
    modems: tuple[ModemSummary, ...] = ()
    recent_log_lines: tuple[str, ...] = field(default_factory=tuple)
    # Most recent activation-attempt error, set by activation dialog when
    # the gRPC handshake rejects the freshly-pasted token. ``None`` when
    # no error pending. Diagnostic window surfaces this verbatim.
    last_activation_error: str | None = None

    @property
    def tray_color(self) -> TrayColor:
        """Compute the binary tray colour from the snapshot.

        Spec: deployment-topology § Scenario "Tray UX 二态" — green when
        cloud-edge is up AND at least one modem is idle; red otherwise.
        """
        if self.cloud_link != CloudLinkState.CONNECTED:
            return TrayColor.RED
        if any(m.state == ModemState.IDLE for m in self.modems):
            return TrayColor.GREEN
        return TrayColor.RED

    @property
    def is_activated(self) -> bool:
        """Whether the client has a token (regardless of whether the
        server has accepted it yet).
        """
        return self.cloud_link != CloudLinkState.AWAITING_ACTIVATION


Subscriber = Callable[[EdgeStatus], None]


class StateBus:
    """Thread-safe publish/subscribe for :class:`EdgeStatus` snapshots.

    Producers (asyncio tasks / pystray thread) call :meth:`set` which
    atomically swaps the snapshot under a lock and fans out to all
    registered subscribers synchronously. Subscribers MUST NOT block
    or perform blocking IO inside their callback — the lock would
    serialise unrelated producers.

    The bus is intentionally not asyncio-aware; it's the lowest common
    denominator the pystray daemon thread and the Qt main thread can
    share. Asyncio-side adaptors live in :mod:`isales_telephony.ui.tray`.
    """

    def __init__(self, *, initial: EdgeStatus | None = None) -> None:
        self._lock = threading.Lock()
        self._status: EdgeStatus = initial or EdgeStatus()
        self._subscribers: list[Subscriber] = []

    @property
    def status(self) -> EdgeStatus:
        """Return the current snapshot. Safe to read from any thread."""
        with self._lock:
            return self._status

    def subscribe(self, subscriber: Subscriber) -> Callable[[], None]:
        """Register a callback. Returns an unsubscribe handle."""
        with self._lock:
            self._subscribers.append(subscriber)

        def _unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(subscriber)
                except ValueError:
                    pass

        return _unsubscribe

    def set(self, status: EdgeStatus) -> None:
        """Atomically replace the snapshot and fan out to subscribers.

        Subscriber exceptions are logged but do not propagate — a buggy
        widget MUST NOT take down the asyncio publisher.
        """
        with self._lock:
            self._status = status
            subs = list(self._subscribers)
        for sub in subs:
            try:
                sub(status)
            except Exception:  # noqa: BLE001
                logger.exception("state_bus: subscriber raised")

    def update(self, **changes: object) -> EdgeStatus:
        """Convenience for partial updates: replaces a few fields on
        the existing snapshot. Returns the new snapshot.
        """
        with self._lock:
            new_status = replace(self._status, **changes)  # type: ignore[arg-type]
            self._status = new_status
            subs = list(self._subscribers)
        for sub in subs:
            try:
                sub(new_status)
            except Exception:  # noqa: BLE001
                logger.exception("state_bus: subscriber raised")
        return new_status


__all__ = [
    "CloudLinkState",
    "EdgeStatus",
    "ModemState",
    "ModemSummary",
    "StateBus",
    "Subscriber",
    "TrayColor",
]
