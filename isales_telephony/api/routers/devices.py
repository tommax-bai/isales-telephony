"""/devices CRUD + modem-controller heartbeat endpoint."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from isales_common.enums import DeviceStatus
from isales_common.models import Device
from isales_common.schemas._base import AppModel
from isales_common.schemas.device import DeviceCreate, DeviceRead, DeviceUpdate
from pydantic import Field
from sqlalchemy import func, select

from isales_telephony.api.deps import CurrentUser, DBSession
from isales_telephony.api.schemas import Page


class HeartbeatPayload(AppModel):
    """Body of POST /devices/{id}/heartbeat — modem-controller side only.

    ``signal_strength`` belongs to sim_card per data-model spec, not device;
    accept it here so future bindings can fan it out to the active sim row
    (v1 logs and ignores). v1 only writes ``last_seen_at``.
    """

    signal_strength: int | None = Field(default=None, ge=0, le=99)

router = APIRouter(prefix="/devices", tags=["devices"])


@router.get("", response_model=Page[DeviceRead])
async def list_devices(
    session: DBSession,
    _user: CurrentUser,
    status_filter: Annotated[DeviceStatus | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page[DeviceRead]:
    stmt = select(Device).order_by(Device.id)
    count_stmt = select(func.count()).select_from(Device)
    if status_filter is not None:
        stmt = stmt.where(Device.status == status_filter)
        count_stmt = count_stmt.where(Device.status == status_filter)
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)

    total = (await session.execute(count_stmt)).scalar_one()
    rows = (await session.execute(stmt)).scalars().all()
    return Page[DeviceRead](
        items=[DeviceRead.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{device_id}", response_model=DeviceRead)
async def get_device(device_id: int, session: DBSession, _user: CurrentUser) -> DeviceRead:
    obj = await session.get(Device, device_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="device_not_found")
    return DeviceRead.model_validate(obj)


@router.post("", response_model=DeviceRead, status_code=status.HTTP_201_CREATED)
async def create_device(
    payload: DeviceCreate, session: DBSession, _user: CurrentUser
) -> DeviceRead:
    obj = Device(**payload.model_dump(), status=DeviceStatus.UNKNOWN)
    session.add(obj)
    await session.flush()
    await session.refresh(obj)
    return DeviceRead.model_validate(obj)


@router.patch("/{device_id}", response_model=DeviceRead)
async def update_device(
    device_id: int, payload: DeviceUpdate, session: DBSession, _user: CurrentUser
) -> DeviceRead:
    obj = await session.get(Device, device_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="device_not_found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    await session.flush()
    await session.refresh(obj)
    return DeviceRead.model_validate(obj)


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_device(device_id: int, session: DBSession, _user: CurrentUser) -> None:
    obj = await session.get(Device, device_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="device_not_found")
    await session.delete(obj)


@router.patch("/{device_id}/heartbeat", status_code=status.HTTP_204_NO_CONTENT)
async def device_heartbeat(
    device_id: int,
    payload: HeartbeatPayload,
    session: DBSession,
    _user: CurrentUser,
) -> None:
    """Refresh ``last_seen_at`` + ``signal_strength`` only.

    Spec: device-hardware § modem-controller 心跳与失联探测 — this endpoint
    MUST NOT touch ``status`` / ``last_call_at`` / ``imei`` etc. The
    modem-controller daemon hits it every 30s; the worker watchdog (separate
    process) flips long-stale rows to ``offline``.
    """

    obj = await session.get(Device, device_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="device_not_found")
    obj.last_seen_at = datetime.now(tz=UTC)
    # signal_strength is a sim_card column; v1 accepts it on the body but
    # doesn't fan out — see HeartbeatPayload docstring.
    _ = payload.signal_strength
