from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Document, DocumentStatus, Job, JobStatus, Workspace
from app.operations.base import KnownOperationError
from app.schemas import UsageLimitResponse, WorkspaceUsageResponse

ACTIVE_JOB_STATUSES = {
    JobStatus.QUEUED.value,
    JobStatus.RUNNING.value,
    JobStatus.VALIDATING.value,
}
KNOWN_JOB_STATUSES = [status.value for status in JobStatus]


def _lock_workspace(session: Session, workspace_id: str) -> None:
    workspace = session.get(Workspace, workspace_id, with_for_update=True)
    if workspace is None:
        raise KnownOperationError(
            "WORKSPACE_NOT_FOUND",
            "The authenticated workspace no longer exists.",
            details={"workspace_id": workspace_id},
        )


def _document_usage(session: Session, workspace_id: str) -> tuple[int, int]:
    row = session.execute(
        select(
            func.count(Document.id),
            func.coalesce(func.sum(Document.size_bytes), 0),
        )
        .where(Document.workspace_id == workspace_id)
        .where(Document.status != DocumentStatus.DELETED.value)
    ).one()
    return int(row[0]), int(row[1])


def _active_job_count(session: Session, workspace_id: str) -> int:
    return int(
        session.scalar(
            select(func.count(Job.id))
            .where(Job.workspace_id == workspace_id)
            .where(Job.status.in_(ACTIVE_JOB_STATUSES))
        )
        or 0
    )


def _job_count_since(session: Session, workspace_id: str, since: datetime) -> int:
    return int(
        session.scalar(
            select(func.count(Job.id))
            .where(Job.workspace_id == workspace_id)
            .where(Job.created_at >= since)
        )
        or 0
    )


def _limit(used: int, limit: int) -> UsageLimitResponse:
    return UsageLimitResponse(
        used=used,
        limit=limit,
        remaining=max(limit - used, 0),
        utilization=round(used / limit, 6),
        exhausted=used >= limit,
    )


def enforce_document_quota(
    session: Session,
    *,
    workspace_id: str,
    incoming_bytes: int,
    incoming_documents: int,
    settings: Settings,
) -> None:
    """Serialize storage reservations so concurrent writes cannot overrun a workspace."""
    _lock_workspace(session, workspace_id)
    document_count, storage_bytes = _document_usage(session, workspace_id)

    if document_count + incoming_documents > settings.workspace_document_limit:
        raise KnownOperationError(
            "WORKSPACE_DOCUMENT_LIMIT_EXCEEDED",
            "The workspace document limit would be exceeded.",
            details={
                "used": document_count,
                "incoming": incoming_documents,
                "limit": settings.workspace_document_limit,
                "delete_endpoint": "/v1/files/{file_id}",
            },
        )
    if storage_bytes + incoming_bytes > settings.workspace_storage_limit_bytes:
        raise KnownOperationError(
            "WORKSPACE_STORAGE_LIMIT_EXCEEDED",
            "The workspace storage limit would be exceeded.",
            details={
                "used_bytes": storage_bytes,
                "incoming_bytes": incoming_bytes,
                "limit_bytes": settings.workspace_storage_limit_bytes,
                "delete_endpoint": "/v1/files/{file_id}",
            },
        )


def enforce_job_quota(
    session: Session,
    *,
    workspace_id: str,
    settings: Settings,
    now: datetime | None = None,
) -> None:
    """Serialize job admission and enforce both concurrency and hourly throughput."""
    current_time = now or datetime.now(UTC)
    _lock_workspace(session, workspace_id)
    active_jobs = _active_job_count(session, workspace_id)
    if active_jobs >= settings.workspace_active_job_limit:
        raise KnownOperationError(
            "WORKSPACE_ACTIVE_JOB_LIMIT_EXCEEDED",
            "The workspace already has the maximum number of active jobs.",
            details={
                "used": active_jobs,
                "limit": settings.workspace_active_job_limit,
                "retry_after": "Wait for an active job to finish or cancel a queued job.",
            },
            retryable=True,
        )

    jobs_last_hour = _job_count_since(
        session,
        workspace_id,
        current_time - timedelta(hours=1),
    )
    if jobs_last_hour >= settings.workspace_jobs_per_hour_limit:
        raise KnownOperationError(
            "WORKSPACE_JOB_RATE_LIMIT_EXCEEDED",
            "The workspace hourly job limit has been reached.",
            details={
                "used": jobs_last_hour,
                "limit": settings.workspace_jobs_per_hour_limit,
                "window_seconds": 3600,
            },
            retryable=True,
        )


def get_workspace_usage(
    session: Session,
    *,
    workspace_id: str,
    settings: Settings,
    now: datetime | None = None,
) -> WorkspaceUsageResponse:
    generated_at = now or datetime.now(UTC)
    document_count, storage_bytes = _document_usage(session, workspace_id)
    active_jobs = _active_job_count(session, workspace_id)
    jobs_last_hour = _job_count_since(
        session,
        workspace_id,
        generated_at - timedelta(hours=1),
    )

    rows = session.execute(
        select(Job.status, func.count(Job.id))
        .where(Job.workspace_id == workspace_id)
        .where(Job.created_at >= generated_at - timedelta(hours=24))
        .group_by(Job.status)
    ).all()
    status_counts = {status: 0 for status in KNOWN_JOB_STATUSES}
    for status, count in rows:
        status_counts[str(status)] = int(count)

    terminal_count = sum(
        status_counts[status]
        for status in [
            JobStatus.SUCCEEDED.value,
            JobStatus.COMPLETED_WITH_WARNINGS.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELED.value,
        ]
    )
    terminal_failure_rate = (
        round(status_counts[JobStatus.FAILED.value] / terminal_count, 6)
        if terminal_count
        else None
    )

    return WorkspaceUsageResponse(
        workspace_id=workspace_id,
        generated_at=generated_at,
        storage_bytes=_limit(storage_bytes, settings.workspace_storage_limit_bytes),
        documents=_limit(document_count, settings.workspace_document_limit),
        active_jobs=_limit(active_jobs, settings.workspace_active_job_limit),
        jobs_last_hour=_limit(jobs_last_hour, settings.workspace_jobs_per_hour_limit),
        job_status_last_24_hours=status_counts,
        terminal_failure_rate_last_24_hours=terminal_failure_rate,
    )
