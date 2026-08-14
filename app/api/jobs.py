from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.errors import operation_http_error
from app.config import Settings, get_settings
from app.db import get_session
from app.operations.base import KnownOperationError
from app.schemas import JobCreate, JobCreatedResponse, JobListResponse, JobStatusResponse
from app.services.auth import AuthContext, require_auth_context
from app.services.jobs import (
    build_job_response,
    cancel_job,
    create_job,
    created_response,
    list_jobs_for_response,
    load_job_for_response,
)
from app.services.storage import StorageService

router = APIRouter(
    prefix="/jobs",
    tags=["jobs"],
    dependencies=[Depends(require_auth_context)],
)


@router.post("", response_model=JobCreatedResponse, status_code=status.HTTP_202_ACCEPTED)
def create_job_endpoint(
    request: JobCreate,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    context: Annotated[AuthContext, Depends(require_auth_context)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JobCreatedResponse:
    try:
        job = create_job(
            session,
            workspace_id=context.workspace_id,
            request=request,
            idempotency_key=idempotency_key,
            settings=settings,
        )
    except KnownOperationError as exc:
        status_code = (
            status.HTTP_503_SERVICE_UNAVAILABLE if exc.retryable else status.HTTP_400_BAD_REQUEST
        )
        if exc.code == "IDEMPOTENCY_KEY_CONFLICT":
            status_code = status.HTTP_409_CONFLICT
        raise operation_http_error(
            exc,
            status_code=status_code,
        ) from exc
    return created_response(job)


@router.get("", response_model=JobListResponse)
def list_jobs_endpoint(
    session: Annotated[Session, Depends(get_session)],
    context: Annotated[AuthContext, Depends(require_auth_context)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobListResponse:
    try:
        return list_jobs_for_response(
            session,
            workspace_id=context.workspace_id,
            status_filter=status_filter,
            limit=limit,
            offset=offset,
        )
    except KnownOperationError as exc:
        raise operation_http_error(exc) from exc


@router.get("/{job_id}", response_model=JobStatusResponse)
def get_job_endpoint(
    job_id: str,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    context: Annotated[AuthContext, Depends(require_auth_context)],
) -> JobStatusResponse:
    job = load_job_for_response(session, job_id, workspace_id=context.workspace_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": {
                    "code": "JOB_NOT_FOUND",
                    "message": "The requested job does not exist.",
                    "details": {"job_id": job_id},
                    "retryable": False,
                }
            },
        )
    return build_job_response(job=job, storage=StorageService(settings))


@router.post("/{job_id}/cancel", response_model=JobStatusResponse)
def cancel_job_endpoint(
    job_id: str,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    context: Annotated[AuthContext, Depends(require_auth_context)],
) -> JobStatusResponse:
    try:
        job = cancel_job(
            session,
            workspace_id=context.workspace_id,
            job_id=job_id,
            settings=settings,
        )
    except KnownOperationError as exc:
        status_code = status.HTTP_400_BAD_REQUEST
        if exc.code == "JOB_NOT_FOUND":
            status_code = status.HTTP_404_NOT_FOUND
        if exc.code == "JOB_NOT_CANCELABLE":
            status_code = status.HTTP_409_CONFLICT
        if exc.retryable:
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        raise operation_http_error(exc, status_code=status_code) from exc

    return build_job_response(job=job, storage=StorageService(settings))
