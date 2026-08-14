from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.errors import operation_http_error
from app.config import Settings, get_settings
from app.db import get_session
from app.operations.base import KnownOperationError
from app.schemas import (
    WebhookCreate,
    WebhookCreateResponse,
    WebhookDeliveryListResponse,
    WebhookDeliveryState,
    WebhookEndpointListResponse,
    WebhookEndpointResponse,
)
from app.services.auth import AuthContext, require_auth_context
from app.services.webhooks import (
    create_webhook_endpoint,
    disable_webhook_endpoint,
    get_webhook_endpoint,
    list_webhook_deliveries,
    list_webhook_endpoints,
)

router = APIRouter(
    prefix="/webhooks",
    tags=["webhooks"],
    dependencies=[Depends(require_auth_context)],
)


def _webhook_not_found(webhook_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "error": {
                "code": "WEBHOOK_NOT_FOUND",
                "message": "The requested webhook endpoint does not exist.",
                "details": {"webhook_id": webhook_id},
                "retryable": False,
            }
        },
    )


@router.post("", response_model=WebhookCreateResponse, status_code=status.HTTP_201_CREATED)
def create_webhook_endpoint_api(
    request: WebhookCreate,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    context: Annotated[AuthContext, Depends(require_auth_context)],
) -> WebhookCreateResponse:
    try:
        return create_webhook_endpoint(
            session,
            workspace_id=context.workspace_id,
            request=request,
            settings=settings,
        )
    except KnownOperationError as exc:
        raise operation_http_error(exc) from exc


@router.get("", response_model=WebhookEndpointListResponse)
def list_webhook_endpoints_api(
    session: Annotated[Session, Depends(get_session)],
    context: Annotated[AuthContext, Depends(require_auth_context)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WebhookEndpointListResponse:
    return list_webhook_endpoints(
        session,
        workspace_id=context.workspace_id,
        limit=limit,
        offset=offset,
    )


@router.get("/deliveries", response_model=WebhookDeliveryListResponse)
def list_webhook_deliveries_api(
    session: Annotated[Session, Depends(get_session)],
    context: Annotated[AuthContext, Depends(require_auth_context)],
    endpoint_id: str | None = None,
    job_id: str | None = None,
    status_filter: Annotated[WebhookDeliveryState | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> WebhookDeliveryListResponse:
    return list_webhook_deliveries(
        session,
        workspace_id=context.workspace_id,
        endpoint_id=endpoint_id,
        job_id=job_id,
        status_filter=status_filter,
        limit=limit,
        offset=offset,
    )


@router.get("/{webhook_id}", response_model=WebhookEndpointResponse)
def get_webhook_endpoint_api(
    webhook_id: str,
    session: Annotated[Session, Depends(get_session)],
    context: Annotated[AuthContext, Depends(require_auth_context)],
) -> WebhookEndpointResponse:
    endpoint = get_webhook_endpoint(
        session,
        webhook_id,
        workspace_id=context.workspace_id,
    )
    if endpoint is None:
        raise _webhook_not_found(webhook_id)
    return endpoint


@router.post("/{webhook_id}/disable", response_model=WebhookEndpointResponse)
def disable_webhook_endpoint_api(
    webhook_id: str,
    session: Annotated[Session, Depends(get_session)],
    context: Annotated[AuthContext, Depends(require_auth_context)],
) -> WebhookEndpointResponse:
    endpoint = disable_webhook_endpoint(
        session,
        webhook_id,
        workspace_id=context.workspace_id,
    )
    if endpoint is None:
        raise _webhook_not_found(webhook_id)
    return endpoint
