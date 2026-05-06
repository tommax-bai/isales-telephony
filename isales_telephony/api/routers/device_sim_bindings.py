"""/device-sim-bindings — read-only listing (lifecycle is managed elsewhere)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from isales_common.models import DeviceSimBinding
from sqlalchemy import select

from isales_telephony.api.deps import CurrentUser, DBSession
from isales_telephony.api.schemas import DeviceSimBindingRead

router = APIRouter(prefix="/device-sim-bindings", tags=["device-sim-bindings"])


@router.get("", response_model=list[DeviceSimBindingRead])
async def list_bindings(
    session: DBSession,
    _user: CurrentUser,
    device_id: Annotated[int | None, Query()] = None,
    sim_card_id: Annotated[int | None, Query()] = None,
    active_only: Annotated[bool, Query()] = False,
) -> list[DeviceSimBindingRead]:
    stmt = select(DeviceSimBinding).order_by(DeviceSimBinding.id)
    if device_id is not None:
        stmt = stmt.where(DeviceSimBinding.device_id == device_id)
    if sim_card_id is not None:
        stmt = stmt.where(DeviceSimBinding.sim_card_id == sim_card_id)
    if active_only:
        stmt = stmt.where(DeviceSimBinding.is_active.is_(True))
    rows = (await session.execute(stmt)).scalars().all()
    return [DeviceSimBindingRead.model_validate(r) for r in rows]
