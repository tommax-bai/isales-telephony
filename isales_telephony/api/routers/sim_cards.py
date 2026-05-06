"""/sim-cards CRUD."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from isales_common.enums import SimCardStatus
from isales_common.models import SimCard
from isales_common.schemas.sim_card import SimCardCreate, SimCardRead, SimCardUpdate
from sqlalchemy import func, select

from isales_telephony.api.deps import CurrentUser, DBSession
from isales_telephony.api.schemas import Page

router = APIRouter(prefix="/sim-cards", tags=["sim-cards"])


@router.get("", response_model=Page[SimCardRead])
async def list_sim_cards(
    session: DBSession,
    _user: CurrentUser,
    carrier: Annotated[str | None, Query()] = None,
    status_filter: Annotated[SimCardStatus | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page[SimCardRead]:
    stmt = select(SimCard).order_by(SimCard.id)
    count_stmt = select(func.count()).select_from(SimCard)
    if carrier is not None:
        stmt = stmt.where(SimCard.carrier == carrier)
        count_stmt = count_stmt.where(SimCard.carrier == carrier)
    if status_filter is not None:
        stmt = stmt.where(SimCard.status == status_filter)
        count_stmt = count_stmt.where(SimCard.status == status_filter)
    stmt = stmt.offset((page - 1) * page_size).limit(page_size)

    total = (await session.execute(count_stmt)).scalar_one()
    rows = (await session.execute(stmt)).scalars().all()
    return Page[SimCardRead](
        items=[SimCardRead.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{sim_id}", response_model=SimCardRead)
async def get_sim(sim_id: int, session: DBSession, _user: CurrentUser) -> SimCardRead:
    obj = await session.get(SimCard, sim_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="sim_card_not_found")
    return SimCardRead.model_validate(obj)


@router.post("", response_model=SimCardRead, status_code=status.HTTP_201_CREATED)
async def create_sim(
    payload: SimCardCreate, session: DBSession, _user: CurrentUser
) -> SimCardRead:
    obj = SimCard(**payload.model_dump(), status=SimCardStatus.ACTIVE)
    session.add(obj)
    await session.flush()
    await session.refresh(obj)
    return SimCardRead.model_validate(obj)


@router.patch("/{sim_id}", response_model=SimCardRead)
async def update_sim(
    sim_id: int, payload: SimCardUpdate, session: DBSession, _user: CurrentUser
) -> SimCardRead:
    obj = await session.get(SimCard, sim_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="sim_card_not_found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, field, value)
    await session.flush()
    await session.refresh(obj)
    return SimCardRead.model_validate(obj)


@router.delete("/{sim_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sim(sim_id: int, session: DBSession, _user: CurrentUser) -> None:
    obj = await session.get(SimCard, sim_id)
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="sim_card_not_found")
    await session.delete(obj)
