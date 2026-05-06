"""Async SQLAlchemy session factory.

Each service builds its own engine + sessionmaker at startup. The URL is read
from ``ISALES_DATABASE_URL`` (same env var isales-common's alembic uses).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def get_engine(url: str | None = None) -> AsyncEngine:
    """Build an async engine. URL falls back to ``ISALES_DATABASE_URL``."""
    resolved = url or os.environ["ISALES_DATABASE_URL"]
    return create_async_engine(resolved, future=True)


def get_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def session_dependency(
    sm: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency that yields a session and commits/rolls back on exit."""
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
