"""FastAPI dependencies — DB session + (re-export) current_user."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from isales_telephony.common.auth import CurrentUser  # re-export

__all__ = ["CurrentUser", "DBSession"]


def _sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    sm = getattr(request.app.state, "sessionmaker", None)
    if sm is None:
        raise RuntimeError("app.state.sessionmaker not configured")
    return sm  # type: ignore[no-any-return]


async def get_session(
    sm: Annotated[async_sessionmaker[AsyncSession], Depends(_sessionmaker)],
) -> AsyncIterator[AsyncSession]:
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


DBSession = Annotated[AsyncSession, Depends(get_session)]
