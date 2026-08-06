from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.models import (
    Document,
    DocumentStatus,
    Job,
    JobInput,
    JobOutput,
)
from app.operations.base import KnownOperationError
from app.operations.executor import SUPPORTED_OPERATIONS
from app.schemas import (
    AuditEventResponse,
    JobCreate,
    JobCreatedResponse,
    JobOutputResponse,
    JobStatusResponse,
)
from app.services.audit import add_audit_event
from app.services.storage import StorageService
from worker.queue import enqueue_job


def job_request_fingerprint(request: JobCreate) -> str:
    payload = {
        "operation": request.operation,
        "inputs": [input_ref.file_id for input_ref in request.inputs],
        "parameters": request.parameters,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _validate_job_request(request: JobCreate, documents: list[Document]) -> None:
    if request.operation not in SUPPORTED_OPERATIONS:
        raise KnownOperationError(
            "UNSUPPORTED_OPERATION",
            "The requested operation is not supported.",
            details={"operation": request.operation, "supported": sorted(SUPPORTED_OPERATIONS)},
        )

    if request.operation == "merge" and len(documents) < 2:
        raise KnownOperationError(
            "NOT_ENOUGH_INPUTS",
            "Merge requires at least two PDF inputs.",
            details={"input_count": len(documents)},
        )

    if request.operation in {"split", "ocr", "compress", "extract_text"} and len(documents) != 1:
        raise KnownOperationError(
            "INVALID_INPUT_COUNT",
            f"{request.operation} requires exactly one PDF input.",
            details={"input_count": len(documents)},
        )

    if request.operation == "split":
        ranges = request.parameters.get("page_ranges")
        if not isinstance(ranges, list) or not all(isinstance(item, str) for item in ranges):
            raise KnownOperationError(
                "INVALID_PARAMETERS",
                "Split requires page_ranges as a list of strings.",
                details={"parameters": request.parameters},
            )


def create_job(
    session: Session,
    *,
    request: JobCreate,
    idempotency_key: str | None,
    settings: Settings | None = None,
) -> Job:
    active_settings = settings or get_settings()
    idempotency_fingerprint = job_request_fingerprint(request) if idempotency_key else None

    if idempotency_key:
        existing = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing is not None:
            if (
                existing.idempotency_fingerprint
                and existing.idempotency_fingerprint != idempotency_fingerprint
            ):
                raise KnownOperationError(
                    "IDEMPOTENCY_KEY_CONFLICT",
                    "The idempotency key was already used for a different job request.",
                    details={"idempotency_key": idempotency_key},
                )
            return existing

    documents: list[Document] = []
    for input_ref in request.inputs:
        document = session.get(Document, input_ref.file_id)
        if document is None:
            raise KnownOperationError(
                "FILE_NOT_FOUND",
                "An input file does not exist.",
                details={"file_id": input_ref.file_id},
            )
        if document.status != DocumentStatus.VALIDATED.value:
            raise KnownOperationError(
                "FILE_NOT_VALIDATED",
                "All input files must be validated before processing.",
                details={"file_id": document.id, "status": document.status},
            )
        documents.append(document)

    _validate_job_request(request, documents)

    job = Job(
        operation=request.operation,
        parameters=request.parameters,
        idempotency_key=idempotency_key,
        idempotency_fingerprint=idempotency_fingerprint,
    )
    session.add(job)
    session.flush()

    for position, document in enumerate(documents):
        session.add(JobInput(job_id=job.id, document_id=document.id, position=position))

    add_audit_event(
        session,
        job_id=job.id,
        event_type="job.created",
        payload={
            "operation": request.operation,
            "input_file_ids": [document.id for document in documents],
            "parameters": request.parameters,
            "idempotency_key": idempotency_key,
        },
    )
    session.commit()

    try:
        rq_job_id = enqueue_job(job.id, settings=active_settings)
    except Exception as exc:
        job.error_code = "QUEUE_UNAVAILABLE"
        job.error_message = str(exc)
        job.status = "failed"
        session.add(job)
        add_audit_event(
            session,
            job_id=job.id,
            event_type="job.enqueue_failed",
            payload={"reason": str(exc)},
        )
        session.commit()
        raise KnownOperationError(
            "QUEUE_UNAVAILABLE",
            "The job queue is unavailable.",
            details={"reason": str(exc)},
            retryable=True,
        ) from exc

    add_audit_event(
        session,
        job_id=job.id,
        event_type="job.enqueued",
        payload={"rq_job_id": rq_job_id, "queue": active_settings.queue_name},
    )
    session.commit()
    return job


def created_response(job: Job) -> JobCreatedResponse:
    return JobCreatedResponse(job_id=job.id, status=job.status)


def load_job_for_response(session: Session, job_id: str) -> Job | None:
    return session.scalar(
        select(Job)
        .options(
            selectinload(Job.outputs).selectinload(JobOutput.document),
            selectinload(Job.audit_events),
        )
        .where(Job.id == job_id)
    )


def build_job_response(
    *,
    job: Job,
    storage: StorageService,
) -> JobStatusResponse:
    outputs = [
        JobOutputResponse(
            file_id=output.document.id,
            filename=output.document.original_filename,
            mime_type=output.document.mime_type,
            page_count=output.document.page_count,
            download_url=storage.presigned_download_url(
                key=output.document.storage_key,
                filename=output.document.original_filename,
            ),
        )
        for output in job.outputs
    ]
    audit = [
        AuditEventResponse(
            event_type=event.event_type,
            payload=event.payload,
            created_at=event.created_at,
        )
        for event in job.audit_events
    ]
    error: dict[str, Any] | None = None
    if job.error_code or job.error_message:
        error = {
            "code": job.error_code or "JOB_FAILED",
            "message": job.error_message or "The job failed.",
            "details": {},
            "retryable": False,
        }
    return JobStatusResponse(
        job_id=job.id,
        operation=job.operation,
        status=job.status,
        parameters=job.parameters,
        outputs=outputs,
        validation=job.validation,
        error=error,
        audit=audit,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )
