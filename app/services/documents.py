from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import Document, DocumentStatus, Job, JobInput, JobStatus
from app.operations.base import KnownOperationError
from app.schemas import DocumentDeleteResponse, FileListResponse, FileSummaryResponse
from app.services.audit import add_audit_event
from app.services.storage import StorageService

ACTIVE_JOB_STATUSES = (
    JobStatus.QUEUED.value,
    JobStatus.RUNNING.value,
    JobStatus.VALIDATING.value,
)


DOCUMENT_STATUSES = {status.value for status in DocumentStatus}


def is_deleted(document: Document) -> bool:
    return document.status == DocumentStatus.DELETED.value


def validate_document_status_filter(status_filter: str | None) -> str | None:
    if status_filter is None:
        return None
    if status_filter not in DOCUMENT_STATUSES:
        raise KnownOperationError(
            "INVALID_DOCUMENT_STATUS",
            "The requested file status filter is not supported.",
            details={"status": status_filter, "allowed": sorted(DOCUMENT_STATUSES)},
        )
    return status_filter


@dataclass(frozen=True)
class RetentionSweep:
    cutoff: datetime | None
    examined: int = 0
    purged: int = 0
    skipped_in_use: int = 0
    purged_file_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cutoff": self.cutoff.isoformat() if self.cutoff else None,
            "examined": self.examined,
            "purged": self.purged,
            "skipped_in_use": self.skipped_in_use,
            "purged_file_ids": list(self.purged_file_ids),
        }


def retention_cutoff(*, settings: Settings, now: datetime | None = None) -> datetime | None:
    if settings.document_retention_days is None:
        return None
    return (now or datetime.now(UTC)) - timedelta(days=settings.document_retention_days)


def lock_document(
    session: Session,
    file_id: str,
    *,
    workspace_id: str | None,
) -> Document | None:
    """Take a row lock so a document cannot be purged and job-referenced concurrently.

    ``workspace_id`` is required rather than defaulting, so that reaching across tenants is
    always a written choice. Only the retention sweep passes ``None``.
    """
    if workspace_id is None:
        return session.get(Document, file_id, with_for_update=True)
    return session.scalar(
        select(Document)
        .where(Document.id == file_id)
        .where(Document.workspace_id == workspace_id)
        .with_for_update()
    )


def is_in_active_job(
    session: Session,
    document_id: str,
    *,
    workspace_id: str | None,
) -> bool:
    statement = (
        select(JobInput.id)
        .join(Job, Job.id == JobInput.job_id)
        .where(JobInput.document_id == document_id)
        .where(Job.status.in_(ACTIVE_JOB_STATUSES))
        .limit(1)
    )
    if workspace_id is not None:
        statement = statement.where(Job.workspace_id == workspace_id)
    return session.scalar(statement) is not None


def purge_expired_documents(
    session: Session,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    limit: int = 500,
) -> RetentionSweep:
    """Purge documents older than the retention window, leaving those a job still needs."""
    active_settings = settings or get_settings()
    cutoff = retention_cutoff(settings=active_settings, now=now)
    if cutoff is None:
        return RetentionSweep(cutoff=None)

    candidate_ids = list(
        session.scalars(
            select(Document.id)
            .where(Document.created_at < cutoff)
            .where(Document.status != DocumentStatus.DELETED.value)
            .order_by(Document.created_at, Document.id)
            .limit(limit)
        )
    )

    purged_file_ids: list[str] = []
    examined = 0
    skipped = 0
    for file_id in candidate_ids:
        # One document at a time, locked. Selecting is not enough: a job could attach this
        # document as an input between the select above and the purge below, and would then
        # start with bytes that are about to disappear.
        # Deliberately unscoped: retention is a platform-wide sweep, not a tenant action.
        document = lock_document(session, file_id, workspace_id=None)
        if document is None or is_deleted(document):
            # A concurrent sweep already dealt with it.
            session.commit()
            continue

        examined += 1
        # An unfinished job still needs its input, however old that input is.
        if is_in_active_job(session, file_id, workspace_id=document.workspace_id):
            skipped += 1
            session.commit()
            continue

        purge_document(session, document, settings=active_settings, reason="retention")
        purged_file_ids.append(file_id)

    return RetentionSweep(
        cutoff=cutoff,
        examined=examined,
        purged=len(purged_file_ids),
        skipped_in_use=skipped,
        purged_file_ids=purged_file_ids,
    )


def _document_summary(document: Document) -> FileSummaryResponse:
    return FileSummaryResponse(
        file_id=document.id,
        filename=document.original_filename,
        mime_type=document.mime_type,
        size_bytes=document.size_bytes,
        sha256=document.sha256,
        page_count=document.page_count,
        status=document.status,
        source_job_id=document.source_job_id,
        created_at=document.created_at,
        deleted_at=document.deleted_at,
    )


def list_documents_for_response(
    session: Session,
    *,
    workspace_id: str,
    status_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> FileListResponse:
    validated_status = validate_document_status_filter(status_filter)
    # created_at is transaction time, so rows written together (a job's outputs, say) share
    # a timestamp exactly. Without a tiebreak, paging can repeat or skip them.
    statement = (
        select(Document)
        .where(Document.workspace_id == workspace_id)
        .order_by(Document.created_at.desc(), Document.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if validated_status is not None:
        statement = statement.where(Document.status == validated_status)

    found = list(session.scalars(statement))
    return FileListResponse(
        files=[_document_summary(document) for document in found],
        count=len(found),
        limit=limit,
        offset=offset,
    )


def _blocking_job_ids(
    session: Session,
    document_id: str,
    *,
    workspace_id: str,
) -> list[str]:
    return list(
        session.scalars(
            select(Job.id)
            .join(JobInput, JobInput.job_id == Job.id)
            .where(JobInput.document_id == document_id)
            .where(Job.workspace_id == workspace_id)
            .where(Job.status.in_(ACTIVE_JOB_STATUSES))
            .order_by(Job.created_at)
        )
    )


def purge_document(
    session: Session,
    document: Document,
    *,
    settings: Settings,
    reason: str = "requested",
) -> None:
    """Remove the stored bytes and retire the row. The single deletion path."""
    StorageService(settings).delete_object(key=document.storage_key)

    document.status = DocumentStatus.DELETED.value
    document.deleted_at = datetime.now(UTC)
    session.add(document)

    # An output document belongs to the job that produced it, so its removal belongs in
    # that job's trail. Uploaded inputs have no job to attach to; the document row is the
    # record for those.
    if document.source_job_id:
        add_audit_event(
            session,
            workspace_id=document.workspace_id,
            job_id=document.source_job_id,
            event_type="output.deleted",
            payload={
                "file_id": document.id,
                "filename": document.original_filename,
                "reason": reason,
            },
        )
    session.commit()


def delete_document(
    session: Session,
    *,
    file_id: str,
    workspace_id: str,
    settings: Settings | None = None,
) -> DocumentDeleteResponse:
    active_settings = settings or get_settings()
    # Locked for the same reason the sweep locks: the in-use check below is only meaningful
    # if no job can attach this document as an input before the purge commits.
    document = lock_document(session, file_id, workspace_id=workspace_id)
    if document is None:
        raise KnownOperationError(
            "FILE_NOT_FOUND",
            "The requested file does not exist.",
            details={"file_id": file_id},
        )

    # Deleting is idempotent: the caller asked for the bytes to be gone and they are.
    if is_deleted(document):
        return DocumentDeleteResponse(
            file_id=document.id,
            status=document.status,
            deleted_at=document.deleted_at,
        )

    blocking_job_ids = _blocking_job_ids(
        session,
        document.id,
        workspace_id=document.workspace_id,
    )
    if blocking_job_ids:
        raise KnownOperationError(
            "FILE_IN_USE",
            "The file is an input to a job that has not finished.",
            details={"file_id": document.id, "job_ids": blocking_job_ids},
        )

    purge_document(session, document, settings=active_settings)

    return DocumentDeleteResponse(
        file_id=document.id,
        status=document.status,
        deleted_at=document.deleted_at,
    )
