"""udev watcher tests — fake events drive last_seen_at / offline transitions."""

from __future__ import annotations

import pytest
from isales_common.enums import DeviceStatus
from isales_common.models import Device
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from isales_telephony.modem_controller.udev_watcher import (
    GSM_MODEM_WHITELIST,
    UdevEvent,
    consume_events,
    fake_events,
    start_udev_watcher,
)


@pytest.mark.asyncio(loop_scope="session")
async def test_add_event_writes_last_seen_at(clean_engine: AsyncEngine) -> None:
    sm = async_sessionmaker(clean_engine, expire_on_commit=False)
    async with sm() as session:
        dev = Device(name="m1", imei="i1", usb_port="/dev/ttyUSB0")
        session.add(dev)
        await session.commit()
        device_id = dev.id

    vid, pid = next(iter(GSM_MODEM_WHITELIST))
    events = fake_events(
        [
            UdevEvent(
                action="add",
                device_node="/dev/ttyUSB0",
                vendor_id=vid,
                product_id=pid,
            )
        ]
    )
    await consume_events(sm, events)

    async with sm() as session:
        d = await session.get(Device, device_id)
        assert d is not None and d.last_seen_at is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_remove_event_marks_offline(clean_engine: AsyncEngine) -> None:
    sm = async_sessionmaker(clean_engine, expire_on_commit=False)
    async with sm() as session:
        dev = Device(
            name="m2",
            imei="i2",
            usb_port="/dev/ttyUSB1",
            status=DeviceStatus.IDLE,
        )
        session.add(dev)
        await session.commit()
        device_id = dev.id

    events = fake_events(
        [
            UdevEvent(
                action="remove",
                device_node="/dev/ttyUSB1",
                vendor_id=None,
                product_id=None,
            )
        ]
    )
    await consume_events(sm, events)

    async with sm() as session:
        d = await session.get(Device, device_id)
        assert d is not None and d.status == DeviceStatus.OFFLINE


@pytest.mark.asyncio(loop_scope="session")
async def test_add_unknown_vendor_is_ignored(clean_engine: AsyncEngine) -> None:
    sm = async_sessionmaker(clean_engine, expire_on_commit=False)
    async with sm() as session:
        dev = Device(name="m3", imei="i3", usb_port="/dev/ttyUSB2")
        session.add(dev)
        await session.commit()
        device_id = dev.id

    events = fake_events(
        [
            UdevEvent(
                action="add",
                device_node="/dev/ttyUSB2",
                vendor_id="0000",
                product_id="0000",
            )
        ]
    )
    await consume_events(sm, events)

    async with sm() as session:
        d = await session.get(Device, device_id)
        assert d is not None and d.last_seen_at is None


@pytest.mark.asyncio(loop_scope="session")
async def test_skip_on_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    # macOS or any host with the env var → start_udev_watcher returns None
    monkeypatch.setenv("ISALES_SKIP_UDEV", "1")
    sm = async_sessionmaker(create_engine_stub(), expire_on_commit=False)
    task = await start_udev_watcher(sm)
    assert task is None


def create_engine_stub() -> object:
    """A bare-minimum stand-in — start_udev_watcher should return before
    actually using the engine when ISALES_SKIP_UDEV=1."""
    return object()
