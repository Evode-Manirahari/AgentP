from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ApiKey, Workspace
from app.operations.base import KnownOperationError
from app.schemas import (
    ApiKeyCreateResponse,
    ApiKeyListResponse,
    ApiKeyResponse,
    WorkspaceCreate,
    WorkspaceCreateResponse,
    WorkspaceResponse,
)
from app.services.auth import generate_api_key_token, hash_api_key


def _workspace_response(workspace: Workspace) -> WorkspaceResponse:
    return WorkspaceResponse(
        workspace_id=workspace.id,
        name=workspace.name,
        created_at=workspace.created_at,
    )


def _api_key_response(api_key: ApiKey) -> ApiKeyResponse:
    return ApiKeyResponse(
        api_key_id=api_key.id,
        name=api_key.name,
        prefix=api_key.prefix,
        is_platform_admin=api_key.is_platform_admin,
        created_at=api_key.created_at,
        revoked_at=api_key.revoked_at,
    )


def _create_api_key(
    session: Session,
    *,
    workspace_id: str,
    name: str,
    is_platform_admin: bool = False,
) -> tuple[ApiKey, str]:
    token, prefix = generate_api_key_token()
    api_key = ApiKey(
        workspace_id=workspace_id,
        name=name,
        token_hash=hash_api_key(token),
        prefix=prefix,
        is_platform_admin=is_platform_admin,
    )
    session.add(api_key)
    session.flush()
    return api_key, token


def _lock_workspace(session: Session, workspace_id: str) -> Workspace:
    workspace = session.scalar(
        select(Workspace)
        .where(Workspace.id == workspace_id)
        .with_for_update()
    )
    if workspace is None:
        raise KnownOperationError(
            "WORKSPACE_NOT_FOUND",
            "The requested workspace does not exist.",
            details={"workspace_id": workspace_id},
        )
    return workspace


def create_workspace(
    session: Session,
    *,
    request: WorkspaceCreate,
) -> WorkspaceCreateResponse:
    workspace = Workspace(name=request.name)
    session.add(workspace)
    session.flush()
    api_key, token = _create_api_key(
        session,
        workspace_id=workspace.id,
        name=request.initial_key_name,
    )
    session.commit()
    return WorkspaceCreateResponse(
        workspace=_workspace_response(workspace),
        api_key=ApiKeyCreateResponse(**_api_key_response(api_key).model_dump(), token=token),
    )


def get_workspace(session: Session, workspace_id: str) -> WorkspaceResponse | None:
    workspace = session.get(Workspace, workspace_id)
    return _workspace_response(workspace) if workspace is not None else None


def create_workspace_api_key(
    session: Session,
    *,
    workspace_id: str,
    name: str,
) -> ApiKeyCreateResponse:
    # Every key mutation takes the same workspace lock. Besides returning a clean error for
    # stale workspace credentials, this serializes create/rotate/revoke so the last-active-key
    # invariant cannot be defeated by concurrent requests.
    _lock_workspace(session, workspace_id)
    api_key, token = _create_api_key(
        session,
        workspace_id=workspace_id,
        name=name,
    )
    session.commit()
    return ApiKeyCreateResponse(**_api_key_response(api_key).model_dump(), token=token)


def list_workspace_api_keys(
    session: Session,
    *,
    workspace_id: str,
    limit: int = 50,
    offset: int = 0,
) -> ApiKeyListResponse:
    statement = (
        select(ApiKey)
        .where(ApiKey.workspace_id == workspace_id)
        .order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
        .limit(limit)
        .offset(offset)
    )
    api_keys = list(session.scalars(statement))
    return ApiKeyListResponse(
        api_keys=[_api_key_response(api_key) for api_key in api_keys],
        count=len(api_keys),
        limit=limit,
        offset=offset,
    )


def _get_workspace_api_key(
    session: Session,
    *,
    workspace_id: str,
    api_key_id: str,
) -> ApiKey | None:
    return session.scalar(
        select(ApiKey)
        .where(ApiKey.id == api_key_id)
        .where(ApiKey.workspace_id == workspace_id)
    )


def rotate_workspace_api_key(
    session: Session,
    *,
    workspace_id: str,
    api_key_id: str,
    allow_platform_admin: bool = False,
) -> ApiKeyCreateResponse:
    _lock_workspace(session, workspace_id)
    current = _get_workspace_api_key(
        session,
        workspace_id=workspace_id,
        api_key_id=api_key_id,
    )
    if current is None:
        raise KnownOperationError(
            "API_KEY_NOT_FOUND",
            "The requested API key does not exist.",
            details={"api_key_id": api_key_id},
        )
    if current.is_platform_admin and not allow_platform_admin:
        raise KnownOperationError(
            "PLATFORM_ADMIN_REQUIRED",
            "A platform administrator API key is required to rotate this key.",
            details={"api_key_id": api_key_id},
        )
    if current.revoked_at is not None:
        raise KnownOperationError(
            "API_KEY_REVOKED",
            "A revoked API key cannot be rotated.",
            details={"api_key_id": api_key_id},
        )

    replacement, token = _create_api_key(
        session,
        workspace_id=workspace_id,
        name=current.name,
        is_platform_admin=current.is_platform_admin,
    )
    current.revoked_at = datetime.now(UTC)
    session.add(current)
    session.commit()
    return ApiKeyCreateResponse(
        **_api_key_response(replacement).model_dump(),
        token=token,
    )


def revoke_workspace_api_key(
    session: Session,
    *,
    workspace_id: str,
    api_key_id: str,
    allow_platform_admin: bool = False,
) -> ApiKeyResponse:
    _lock_workspace(session, workspace_id)
    api_key = _get_workspace_api_key(
        session,
        workspace_id=workspace_id,
        api_key_id=api_key_id,
    )
    if api_key is None:
        raise KnownOperationError(
            "API_KEY_NOT_FOUND",
            "The requested API key does not exist.",
            details={"api_key_id": api_key_id},
        )
    if api_key.is_platform_admin and not allow_platform_admin:
        raise KnownOperationError(
            "PLATFORM_ADMIN_REQUIRED",
            "A platform administrator API key is required to revoke this key.",
            details={"api_key_id": api_key_id},
        )
    if api_key.revoked_at is not None:
        return _api_key_response(api_key)

    active_count = session.scalar(
        select(func.count(ApiKey.id))
        .where(ApiKey.workspace_id == workspace_id)
        .where(ApiKey.revoked_at.is_(None))
    )
    if active_count is not None and active_count <= 1:
        raise KnownOperationError(
            "LAST_ACTIVE_API_KEY",
            "The last active API key cannot be revoked. Rotate it instead.",
            details={"api_key_id": api_key_id},
        )

    api_key.revoked_at = datetime.now(UTC)
    session.add(api_key)
    session.commit()
    return _api_key_response(api_key)
