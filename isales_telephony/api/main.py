"""telephony-api FastAPI entrypoint."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from isales_telephony.api.health_monitor import build_scheduler
from isales_telephony.api.routers import device_sim_bindings, devices, select, sim_cards
from isales_telephony.common.db import get_engine, get_sessionmaker


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = get_engine()
    sessionmaker = get_sessionmaker(engine)
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker
    scheduler = build_scheduler(sessionmaker)
    scheduler.start()
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        await engine.dispose()


def create_app() -> FastAPI:
    # Skip lifespan when ISALES_DATABASE_URL is unset (tests configure their own
    # sessionmaker via app.state).
    use_lifespan = lifespan if os.environ.get("ISALES_DATABASE_URL") else None
    app = FastAPI(
        title="isales-telephony-api",
        version="0.1.0",
        lifespan=use_lifespan,
    )

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(devices.router)
    app.include_router(sim_cards.router)
    app.include_router(device_sim_bindings.router)
    app.include_router(select.router)
    return app


app = create_app()


def run() -> None:
    """Console-script entry point: ``telephony-api``."""
    uvicorn.run(
        "isales_telephony.api.main:app",
        host="127.0.0.1",
        port=8001,
        ws="auto",
    )
