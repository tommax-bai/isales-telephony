"""StateBus / EdgeStatus tests — pure-Python, runs on any platform.

The UI itself (pystray, PySide6) needs Windows-only deps; the state
plumbing under it does not. Covering this independently lets us
catch regressions on every CI run, not just the Windows runner.
"""

from __future__ import annotations

import threading

from isales_telephony.ui.state import (
    CloudLinkState,
    EdgeStatus,
    ModemState,
    ModemSummary,
    StateBus,
    TrayColor,
)


def test_default_status_red() -> None:
    status = EdgeStatus()
    assert status.tray_color is TrayColor.RED
    assert status.is_activated is False


def test_green_requires_cloud_connected_and_an_idle_modem() -> None:
    s1 = EdgeStatus(
        cloud_link=CloudLinkState.CONNECTED,
        modems=(ModemSummary("COM3", ModemState.IDLE),),
    )
    assert s1.tray_color is TrayColor.GREEN

    s2 = EdgeStatus(cloud_link=CloudLinkState.CONNECTED, modems=())
    assert s2.tray_color is TrayColor.RED

    s3 = EdgeStatus(
        cloud_link=CloudLinkState.CONNECTED,
        modems=(ModemSummary("COM3", ModemState.OFFLINE),),
    )
    assert s3.tray_color is TrayColor.RED

    s4 = EdgeStatus(
        cloud_link=CloudLinkState.DISCONNECTED,
        modems=(ModemSummary("COM3", ModemState.IDLE),),
    )
    assert s4.tray_color is TrayColor.RED


def test_is_activated_flips_after_awaiting_activation() -> None:
    s = EdgeStatus(cloud_link=CloudLinkState.AWAITING_ACTIVATION)
    assert s.is_activated is False
    s2 = EdgeStatus(cloud_link=CloudLinkState.DISCONNECTED)
    assert s2.is_activated is True


# --------------------------------------------------------------- StateBus


def test_state_bus_publishes_to_subscriber() -> None:
    bus = StateBus()
    received: list[EdgeStatus] = []
    bus.subscribe(received.append)
    new = EdgeStatus(cloud_link=CloudLinkState.CONNECTED)
    bus.set(new)
    assert received == [new]


def test_state_bus_update_partial() -> None:
    bus = StateBus()
    bus.update(cloud_link=CloudLinkState.CONNECTED)
    snap = bus.status
    assert snap.cloud_link is CloudLinkState.CONNECTED
    assert snap.modems == ()  # untouched


def test_state_bus_unsubscribe_stops_callbacks() -> None:
    bus = StateBus()
    received: list[EdgeStatus] = []
    unsub = bus.subscribe(received.append)
    bus.update(cloud_link=CloudLinkState.CONNECTED)
    assert len(received) == 1
    unsub()
    bus.update(cloud_link=CloudLinkState.DISCONNECTED)
    assert len(received) == 1  # no further calls


def test_state_bus_subscriber_exception_is_isolated() -> None:
    bus = StateBus()
    received_b: list[EdgeStatus] = []

    def bad_subscriber(_status: EdgeStatus) -> None:
        raise RuntimeError("buggy widget")

    bus.subscribe(bad_subscriber)
    bus.subscribe(received_b.append)
    bus.update(cloud_link=CloudLinkState.CONNECTED)
    # Despite the first subscriber raising, the second still received it.
    assert len(received_b) == 1


def test_state_bus_thread_safe_concurrent_publish() -> None:
    """Hammer the bus from multiple threads. The lock should serialise
    set() so each subscriber sees a coherent snapshot (no torn reads)."""
    bus = StateBus()
    received: list[EdgeStatus] = []
    lock = threading.Lock()

    def collect(s: EdgeStatus) -> None:
        with lock:
            received.append(s)

    bus.subscribe(collect)

    def worker(state: CloudLinkState) -> None:
        for _ in range(50):
            bus.update(cloud_link=state)

    threads = [
        threading.Thread(target=worker, args=(CloudLinkState.CONNECTED,)),
        threading.Thread(target=worker, args=(CloudLinkState.DISCONNECTED,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(received) == 100
    for snap in received:
        assert snap.cloud_link in (
            CloudLinkState.CONNECTED,
            CloudLinkState.DISCONNECTED,
        )
