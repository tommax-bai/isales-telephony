"""/devices CRUD."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from isales_common.enums import DeviceStatus
from isales_common.models import Device
from isales_common.schemas.device import DeviceCreate, DeviceRead, DeviceUpdate
from sqlalchemy import func, select

from isales_telephony.api.deps import CurrentUser, DBSession
from isales_telephony.api.schemas import Page

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
