from __future__ import annotations

import hashlib
import hmac
import secrets
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings, get_settings
from app.db import SessionLocal, get_session
from app.models import DEFAULT_WORKSPACE_ID, ApiKey, Workspace

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass(frozen=True)
class AuthContext:
    workspace_id: str
    workspace_name: str
    api_key_id: str
    api_key_name: str
    is_platform_admin: bool = False


_current_auth_context: ContextVar[AuthContext | None] = ContextVar(
    "agentp_auth_context",
    default=None,
)


def hash_api_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_api_key_token() -> tuple[str, str]:
    public_id = secrets.token_hex(4)
    token = f"agentp_{public_id}_{secrets.token_urlsafe(32)}"
    return token, f"agentp_{public_id}"


def api_key_prefix(token: str) -> str:
    if token.startswith("agentp_") and token.count("_") >= 2:
        return "_".join(token.split("_", 2)[:2])
    return f"{token[:4]}..." if token else "unknown"


def authenticate_api_key(session: Session, token: str | None) -> AuthContext | None:
    if not token:
        return None

    token_hash = hash_api_key(token)
    key = session.scalar(
        select(ApiKey)
        .options(selectinload(ApiKey.workspace))
        .where(ApiKey.token_hash == token_hash)
        .where(ApiKey.revoked_at.is_(None))
    )
    if (
        key is None
        or key.revoked_at is not None
        or not hmac.compare_digest(key.token_hash, token_hash)
    ):
        return None

    return AuthContext(
        workspace_id=key.workspace_id,
        workspace_name=key.workspace.name,
        api_key_id=key.id,
        api_key_name=key.name,
        is_platform_admin=key.is_platform_admin,
    )


def _auth_error() -> dict[str, Any]:
    return {
        "error": {
            "code": "UNAUTHORIZED",
            "message": "A valid, active X-API-Key header is required.",
            "details": {},
            "retryable": False,
        }
    }


def require_auth_context(
    api_key: Annotated[str | None, Depends(api_key_header)],
    session: Annotated[Session, Depends(get_session)],
) -> AuthContext:
    context = authenticate_api_key(session, api_key)
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_auth_error(),
        )
    return context


def require_platform_admin(
    context: Annotated[AuthContext, Depends(require_auth_context)],
) -> AuthContext:
    if not context.is_platform_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "code": "PLATFORM_ADMIN_REQUIRED",
                    "message": "A platform administrator API key is required.",
                    "details": {},
                    "retryable": False,
                }
            },
        )
    return context


def get_current_auth_context() -> AuthContext | None:
    return _current_auth_context.get()


def ensure_bootstrap_identity(settings: Settings | None = None) -> None:
    """Provision the default workspace and its first administrator key, once.

    Deliberately keyed on "does this workspace have any key at all" rather than on the
    configured token. Re-reading the environment on every start would let anyone who can
    set ``AGENTPDF_API_KEY`` mint themselves platform admin, and would resurrect keys an
    operator had revoked. Rotation goes through the key lifecycle instead.
    """
    active_settings = settings or get_settings()
    try:
        with SessionLocal() as session:
            # Migration 0005 creates this row before bootstrap runs. Locking it serializes
            # concurrent API replicas even if their environments disagree about the seed key.
            workspace = session.scalar(
                select(Workspace)
                .where(Workspace.id == DEFAULT_WORKSPACE_ID)
                .with_for_update()
            )
            if workspace is None:
                workspace = Workspace(id=DEFAULT_WORKSPACE_ID, name="Default Workspace")
                session.add(workspace)
                session.flush()

            existing_key = session.scalar(
                select(ApiKey.id).where(ApiKey.workspace_id == DEFAULT_WORKSPACE_ID).limit(1)
            )
            if existing_key is None:
                session.add(
                    ApiKey(
                        workspace_id=DEFAULT_WORKSPACE_ID,
                        name="Bootstrap administrator",
                        token_hash=hash_api_key(active_settings.api_key),
                        prefix=api_key_prefix(active_settings.api_key),
                        is_platform_admin=True,
                    )
                )
            session.commit()
    except IntegrityError as exc:
        # A metadata-only install can use create_all without migration 0005's seed row, so
        # two API processes may still race while creating it. Ignore the conflict only after
        # a fresh transaction proves the winner also created a key. Other integrity failures
        # are real startup errors and must stay visible.
        with SessionLocal() as session:
            existing_key = session.scalar(
                select(ApiKey.id).where(ApiKey.workspace_id == DEFAULT_WORKSPACE_ID).limit(1)
            )
        if existing_key is None:
            raise exc


class MCPAuthMiddleware:
    """Authenticate MCP HTTP requests and expose their workspace through a context variable."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        raw_token = headers.get(b"x-api-key")
        token = raw_token.decode("utf-8", errors="ignore") if raw_token else None
        with SessionLocal() as session:
            context = authenticate_api_key(session, token)

        if context is None:
            response = JSONResponse(_auth_error(), status_code=status.HTTP_401_UNAUTHORIZED)
            await response(scope, receive, send)
            return

        context_token = _current_auth_context.set(context)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_auth_context.reset(context_token)


# Backward-compatible import name for integrations that depended on the old dependency.
require_api_key = require_auth_context
