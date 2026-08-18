from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_session
from app.schemas import WorkspaceUsageResponse
from app.services.auth import AuthContext, require_auth_context
from app.services.usage import get_workspace_usage

router = APIRouter(
    prefix="/usage",
    tags=["usage"],
    dependencies=[Depends(require_auth_context)],
)


@router.get("", response_model=WorkspaceUsageResponse)
def get_usage_endpoint(
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    context: Annotated[AuthContext, Depends(require_auth_context)],
) -> WorkspaceUsageResponse:
    return get_workspace_usage(
        session,
        workspace_id=context.workspace_id,
        settings=settings,
    )
