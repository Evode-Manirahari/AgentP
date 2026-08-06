from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.api.errors import operation_http_error
from app.config import Settings, get_settings
from app.db import get_session
from app.operations.base import KnownOperationError
from app.schemas import JobCreate, JobCreatedResponse, JobStatusResponse
from app.services.auth import require_api_key
from app.services.jobs import (
    build_job_response,
    create_job,
    created_response,
    load_job_for_response,
)
from app.services.storage import StorageService

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(require_api_key)])


@router.post("", response_model=JobCreatedResponse, status_code=status.HTTP_202_ACCEPTED)
def create_job_endpoint(
    request: JobCreate,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> JobCreatedResponse:
    try:
        job = create_job(
            session,
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


@router.get("/{job_id}", response_model=JobStatusResponse)
def get_job_endpoint(
    job_id: str,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> JobStatusResponse:
    job = load_job_for_response(session, job_id)
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
