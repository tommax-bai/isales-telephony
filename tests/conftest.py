"""Shared fixtures for telephony-api integration tests.

Tests run against a real PostgreSQL — testcontainers is listed in pyproject for
parity with CI, but a Docker daemon isn't always available locally. The strategy
here uses ``ISALES_TEST_DATABASE_URL`` (or ``ISALES_DATABASE_URL``) directly: in
CI the GitHub Actions PG service container provides it; locally, point at any
Postgres (e.g. ``postgresql+asyncpg://bears@localhost:5432/isales_telephony_test``).

If the env var is unset and the default URL is unreachable, integration tests
are skipped via ``engine`` fixture.

Engine uses ``NullPool`` so that asyncpg connections don't cross event loops —
each request the AsyncClient makes checks out a fresh connection.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

# impl-real-at: tests run without a real GSM modem. Opt the ATClient dispatcher
# (modem_controller/main.py:_make_at_client) into MockATClient mode for every
# test process — production deployments MUST NOT set this var.
os.environ.setdefault("ISALES_ALLOW_MOCK_AT", "1")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from isales_common.models import Base
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from isales_telephony.api.main import create_app
from isales_telephony.common.auth import current_user

DEFAULT_TEST_URL = "postgresql+asyncpg://bears@localhost:5432/isales_telephony_test"


def _resolve_url() -> str:
    return (
        os.environ.get("ISALES_TEST_DATABASE_URL")
        or os.environ.get("ISALES_DATABASE_URL")
        or DEFAULT_TEST_URL
    )


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(_resolve_url(), future=True, poolclass=NullPool)
    try:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:
        pytest.skip(f"PG not reachable for integration tests: {exc}")
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def clean_engine(engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """Truncate the tables this PR touches before each test."""
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "TRUNCATE device, sim_card, device_sim_binding, campaign, "
            "campaign_device RESTART IDENTITY CASCADE"
        )
    yield engine


@pytest_asyncio.fixture(loop_scope="session")
async def client(clean_engine: AsyncEngine) -> AsyncIterator[AsyncClient]:
    app = create_app()
    app.state.engine = clean_engine
    app.state.sessionmaker = async_sessionmaker(clean_engine, expire_on_commit=False)
    app.dependency_overrides[current_user] = lambda: {"sub": "test-admin"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
