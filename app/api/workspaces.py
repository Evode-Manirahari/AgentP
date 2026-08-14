from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.errors import operation_http_error
from app.db import get_session
from app.operations.base import KnownOperationError
from app.schemas import (
    ApiKeyCreate,
    ApiKeyCreateResponse,
    ApiKeyListResponse,
    ApiKeyResponse,
    WorkspaceCreate,
    WorkspaceCreateResponse,
    WorkspaceResponse,
)
from app.services.auth import AuthContext, require_auth_context, require_platform_admin
from app.services.workspaces import (
    create_workspace,
    create_workspace_api_key,
    get_workspace,
    list_workspace_api_keys,
    revoke_workspace_api_key,
    rotate_workspace_api_key,
)

workspace_router = APIRouter(prefix="/workspaces", tags=["workspaces"])
api_key_router = APIRouter(
    prefix="/api-keys",
    tags=["api-keys"],
    dependencies=[Depends(require_auth_context)],
)


def _key_error(error: KnownOperationError) -> Exception:
    status_code = status.HTTP_400_BAD_REQUEST
    if error.code == "API_KEY_NOT_FOUND":
        status_code = status.HTTP_404_NOT_FOUND
    if error.code == "PLATFORM_ADMIN_REQUIRED":
        status_code = status.HTTP_403_FORBIDDEN
    if error.code in {"API_KEY_REVOKED", "LAST_ACTIVE_API_KEY"}:
        status_code = status.HTTP_409_CONFLICT
    return operation_http_error(error, status_code=status_code)


@workspace_router.post(
    "",
    response_model=WorkspaceCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_workspace_endpoint(
    request: WorkspaceCreate,
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[AuthContext, Depends(require_platform_admin)],
) -> WorkspaceCreateResponse:
    return create_workspace(session, request=request)


@workspace_router.get("/current", response_model=WorkspaceResponse)
def get_current_workspace_endpoint(
    session: Annotated[Session, Depends(get_session)],
    context: Annotated[AuthContext, Depends(require_auth_context)],
) -> WorkspaceResponse:
    workspace = get_workspace(session, context.workspace_id)
    if workspace is None:
        raise operation_http_error(
            KnownOperationError(
                "WORKSPACE_NOT_FOUND",
                "The authenticated workspace no longer exists.",
                details={"workspace_id": context.workspace_id},
            ),
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return workspace


@api_key_router.post("", response_model=ApiKeyCreateResponse, status_code=status.HTTP_201_CREATED)
def create_api_key_endpoint(
    request: ApiKeyCreate,
    session: Annotated[Session, Depends(get_session)],
    context: Annotated[AuthContext, Depends(require_auth_context)],
) -> ApiKeyCreateResponse:
    return create_workspace_api_key(
        session,
        workspace_id=context.workspace_id,
        name=request.name,
    )


@api_key_router.get("", response_model=ApiKeyListResponse)
def list_api_keys_endpoint(
    session: Annotated[Session, Depends(get_session)],
    context: Annotated[AuthContext, Depends(require_auth_context)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ApiKeyListResponse:
    return list_workspace_api_keys(
        session,
        workspace_id=context.workspace_id,
        limit=limit,
        offset=offset,
    )


@api_key_router.post("/{api_key_id}/rotate", response_model=ApiKeyCreateResponse)
def rotate_api_key_endpoint(
    api_key_id: str,
    session: Annotated[Session, Depends(get_session)],
    context: Annotated[AuthContext, Depends(require_auth_context)],
) -> ApiKeyCreateResponse:
    try:
        return rotate_workspace_api_key(
            session,
            workspace_id=context.workspace_id,
            api_key_id=api_key_id,
            allow_platform_admin=context.is_platform_admin,
        )
    except KnownOperationError as exc:
        raise _key_error(exc) from exc


@api_key_router.post("/{api_key_id}/revoke", response_model=ApiKeyResponse)
def revoke_api_key_endpoint(
    api_key_id: str,
    session: Annotated[Session, Depends(get_session)],
    context: Annotated[AuthContext, Depends(require_auth_context)],
) -> ApiKeyResponse:
    try:
        return revoke_workspace_api_key(
            session,
            workspace_id=context.workspace_id,
            api_key_id=api_key_id,
            allow_platform_admin=context.is_platform_admin,
        )
    except KnownOperationError as exc:
        raise _key_error(exc) from exc
