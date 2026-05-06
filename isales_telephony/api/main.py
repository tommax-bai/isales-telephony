"""telephony-api FastAPI entrypoint.

PR #2 (skeleton) wires the bare app + ``/health``; PR #3 adds CRUD routers.
"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="isales-telephony-api", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

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
