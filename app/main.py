from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.errors import install_exception_handlers
from app.api.files import router as files_router
from app.api.jobs import router as jobs_router
from app.config import get_settings
from app.db import init_db
from app.mcp_server import mcp, mcp_app


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_db()
    async with mcp.session_manager.run():
        yield


settings = get_settings()
app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    summary="Typed, asynchronous, verifiable PDF execution API for AI agents.",
    lifespan=lifespan,
)
install_exception_handlers(app)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(files_router, prefix="/v1")
app.include_router(jobs_router, prefix="/v1")
app.mount("/mcp", mcp_app)
