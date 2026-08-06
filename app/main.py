from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, status
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.errors import install_exception_handlers
from app.api.files import router as files_router
from app.api.jobs import router as jobs_router
from app.api.operations import router as operations_router
from app.config import Settings, get_settings
from app.db import get_session, init_db
from app.mcp_server import mcp, mcp_app
from app.services.storage import StorageService


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


def _record_check(checks: dict[str, dict[str, str]], name: str, fn: Any) -> None:
    try:
        fn()
    except Exception as exc:
        checks[name] = {"status": "error", "message": str(exc)[:500]}
    else:
        checks[name] = {"status": "ok"}


@app.get("/ready", tags=["system"])
def ready(
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    checks: dict[str, dict[str, str]] = {}

    _record_check(checks, "database", lambda: session.execute(text("SELECT 1")))

    def check_redis() -> None:
        client = Redis.from_url(settings.redis_url)
        try:
            client.ping()
        finally:
            client.close()

    _record_check(checks, "redis", check_redis)
    _record_check(checks, "storage", lambda: StorageService(settings).ensure_bucket())

    if any(check["status"] != "ok" for check in checks.values()):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": {
                    "code": "SERVICE_NOT_READY",
                    "message": "One or more service dependencies are not ready.",
                    "details": {"checks": checks},
                    "retryable": True,
                }
            },
        )

    return {"status": "ready", "checks": checks}


app.include_router(files_router, prefix="/v1")
app.include_router(jobs_router, prefix="/v1")
app.include_router(operations_router, prefix="/v1")
app.mount("/mcp", mcp_app)
