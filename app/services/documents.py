from __future__ import annotations

from datetime import UTC, datetime

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
    status_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> FileListResponse:
    validated_status = validate_document_status_filter(status_filter)
    # created_at is transaction time, so rows written together (a job's outputs, say) share
    # a timestamp exactly. Without a tiebreak, paging can repeat or skip them.
    statement = (
        select(Document)
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


def _blocking_job_ids(session: Session, document_id: str) -> list[str]:
    return list(
        session.scalars(
            select(Job.id)
            .join(JobInput, JobInput.job_id == Job.id)
            .where(JobInput.document_id == document_id)
            .where(Job.status.in_(ACTIVE_JOB_STATUSES))
            .order_by(Job.created_at)
        )
    )


def delete_document(
    session: Session,
    *,
    file_id: str,
    settings: Settings | None = None,
) -> DocumentDeleteResponse:
    active_settings = settings or get_settings()
    document = session.get(Document, file_id)
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

    blocking_job_ids = _blocking_job_ids(session, document.id)
    if blocking_job_ids:
        raise KnownOperationError(
            "FILE_IN_USE",
            "The file is an input to a job that has not finished.",
            details={"file_id": document.id, "job_ids": blocking_job_ids},
        )

    StorageService(active_settings).delete_object(key=document.storage_key)

    document.status = DocumentStatus.DELETED.value
    document.deleted_at = datetime.now(UTC)
    session.add(document)

    # An output document belongs to the job that produced it, so its removal belongs in
    # that job's trail. Uploaded inputs have no job to attach to; the document row is the
    # record for those.
    if document.source_job_id:
        add_audit_event(
            session,
            job_id=document.source_job_id,
            event_type="output.deleted",
            payload={"file_id": document.id, "filename": document.original_filename},
        )
    session.commit()

    return DocumentDeleteResponse(
        file_id=document.id,
        status=document.status,
        deleted_at=document.deleted_at,
    )
