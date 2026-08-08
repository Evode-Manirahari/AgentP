from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from redis import Redis
from rq.exceptions import NoSuchJobError
from rq.job import Job as RQJob
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.config import Settings, get_settings
from app.models import (
    Document,
    DocumentStatus,
    Job,
    JobInput,
    JobOutput,
    JobStatus,
)
from app.operations.base import KnownOperationError
from app.operations.compress import PRESETS
from app.operations.executor import SUPPORTED_OPERATIONS
from app.operations.prepare_packet import PACKET_ORDERS
from app.schemas import (
    AuditEventResponse,
    JobCreate,
    JobCreatedResponse,
    JobListResponse,
    JobOutputResponse,
    JobStatusResponse,
    JobSummaryResponse,
)
from app.services.audit import add_audit_event
from app.services.documents import is_deleted
from app.services.storage import StorageService
from app.services.webhooks import safe_queue_terminal_job_webhooks
from worker.queue import enqueue_job

OCR_LANGUAGE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-")
ALLOWED_PARAMETERS = {
    "merge": {"ocr_if_needed", "language", "deskew"},
    "prepare_packet": {"language", "deskew", "order"},
    "split": {"page_ranges"},
    "ocr": {"language", "deskew"},
    "compress": {"preset"},
    "extract_text": {"include_coordinates"},
}
MULTI_INPUT_OPERATIONS = {"merge", "prepare_packet"}
JOB_STATUSES = {status.value for status in JobStatus}
TERMINAL_JOB_STATUSES = {
    JobStatus.SUCCEEDED.value,
    JobStatus.COMPLETED_WITH_WARNINGS.value,
    JobStatus.FAILED.value,
    JobStatus.CANCELED.value,
}


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


def validate_job_status_filter(status_filter: str | None) -> str | None:
    if status_filter is None:
        return None
    if status_filter not in JOB_STATUSES:
        raise KnownOperationError(
            "INVALID_JOB_STATUS",
            "The requested job status filter is not supported.",
            details={"status": status_filter, "allowed": sorted(JOB_STATUSES)},
        )
    return status_filter


def _reject_unknown_parameters(request: JobCreate) -> None:
    allowed = ALLOWED_PARAMETERS[request.operation]
    unknown = set(request.parameters) - allowed
    if unknown:
        raise KnownOperationError(
            "INVALID_PARAMETERS",
            "The request included unsupported operation parameters.",
            details={
                "operation": request.operation,
                "unsupported": sorted(unknown),
                "allowed": sorted(allowed),
            },
        )


def _require_bool_parameter(request: JobCreate, name: str) -> None:
    if name in request.parameters and not isinstance(request.parameters[name], bool):
        raise KnownOperationError(
            "INVALID_PARAMETERS",
            f"{name} must be a boolean.",
            details={"parameter": name, "value": request.parameters[name]},
        )


def _validate_ocr_language(request: JobCreate) -> None:
    if "language" not in request.parameters:
        return

    language = request.parameters["language"]
    if not isinstance(language, str) or not language or any(
        character not in OCR_LANGUAGE_CHARS for character in language
    ):
        raise KnownOperationError(
            "INVALID_OCR_LANGUAGE",
            "OCR language must contain only letters, numbers, '+', or '-'.",
            details={"language": language},
        )


def _validate_job_request(request: JobCreate, documents: list[Document]) -> None:
    if request.operation not in SUPPORTED_OPERATIONS:
        raise KnownOperationError(
            "UNSUPPORTED_OPERATION",
            "The requested operation is not supported.",
            details={"operation": request.operation, "supported": sorted(SUPPORTED_OPERATIONS)},
        )

    if request.operation in MULTI_INPUT_OPERATIONS and len(documents) < 2:
        raise KnownOperationError(
            "NOT_ENOUGH_INPUTS",
            f"{request.operation} requires at least two PDF inputs.",
            details={"operation": request.operation, "input_count": len(documents)},
        )

    if request.operation in {"split", "ocr", "compress", "extract_text"} and len(documents) != 1:
        raise KnownOperationError(
            "INVALID_INPUT_COUNT",
            f"{request.operation} requires exactly one PDF input.",
            details={"input_count": len(documents)},
        )

    _reject_unknown_parameters(request)

    if request.operation == "merge":
        _require_bool_parameter(request, "ocr_if_needed")
        _require_bool_parameter(request, "deskew")
        _validate_ocr_language(request)

    if request.operation == "prepare_packet":
        _require_bool_parameter(request, "deskew")
        _validate_ocr_language(request)
        order = request.parameters.get("order", "as_provided")
        if not isinstance(order, str) or order not in PACKET_ORDERS:
            raise KnownOperationError(
                "INVALID_PACKET_ORDER",
                "Unsupported packet order.",
                details={"order": order, "allowed": sorted(PACKET_ORDERS)},
            )

    if request.operation == "split":
        ranges = request.parameters.get("page_ranges")
        if not isinstance(ranges, list) or not all(isinstance(item, str) for item in ranges):
            raise KnownOperationError(
                "INVALID_PARAMETERS",
                "Split requires page_ranges as a list of strings.",
                details={"parameters": request.parameters},
            )

    if request.operation == "ocr":
        _require_bool_parameter(request, "deskew")
        _validate_ocr_language(request)

    if request.operation == "compress":
        preset = request.parameters.get("preset", "ebook")
        if not isinstance(preset, str) or preset not in PRESETS:
            raise KnownOperationError(
                "INVALID_COMPRESSION_PRESET",
                "Unsupported compression preset.",
                details={"preset": preset, "allowed": sorted(PRESETS)},
            )

    if request.operation == "extract_text":
        _require_bool_parameter(request, "include_coordinates")


def _idempotent_match(
    session: Session,
    *,
    idempotency_key: str,
    fingerprint: str | None,
) -> Job | None:
    existing = session.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
    if existing is None:
        return None
    if existing.idempotency_fingerprint and existing.idempotency_fingerprint != fingerprint:
        raise KnownOperationError(
            "IDEMPOTENCY_KEY_CONFLICT",
            "The idempotency key was already used for a different job request.",
            details={"idempotency_key": idempotency_key},
        )
    return existing


def _never_reached_queue(job: Job) -> bool:
    return (
        job.queue_job_id is None
        and job.status == JobStatus.FAILED.value
        and job.error_code == "QUEUE_UNAVAILABLE"
    )


def _enqueue_or_fail(session: Session, job: Job, *, settings: Settings) -> Job:
    try:
        rq_job_id = enqueue_job(job.id, settings=settings)
    except Exception as exc:
        job.error_code = "QUEUE_UNAVAILABLE"
        job.error_message = str(exc)
        job.status = JobStatus.FAILED.value
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

    job.queue_job_id = rq_job_id
    session.add(job)
    add_audit_event(
        session,
        job_id=job.id,
        event_type="job.enqueued",
        payload={"rq_job_id": rq_job_id, "queue": settings.queue_name},
    )
    session.commit()
    return job


def _resume_existing_job(session: Session, job: Job, *, settings: Settings) -> Job:
    # A QUEUE_UNAVAILABLE failure is reported as retryable, so an idempotent replay has to
    # put the job back on the queue instead of returning a job that will never run.
    if not _never_reached_queue(job):
        return job

    job.status = JobStatus.QUEUED.value
    job.error_code = None
    job.error_message = None
    session.add(job)
    add_audit_event(
        session,
        job_id=job.id,
        event_type="job.enqueue_retried",
        payload={"previous_error_code": "QUEUE_UNAVAILABLE"},
    )
    session.commit()
    return _enqueue_or_fail(session, job, settings=settings)


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
        existing = _idempotent_match(
            session,
            idempotency_key=idempotency_key,
            fingerprint=idempotency_fingerprint,
        )
        if existing is not None:
            return _resume_existing_job(session, existing, settings=active_settings)

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
    try:
        session.flush()
    except IntegrityError:
        # A concurrent request with the same idempotency key won the insert race.
        session.rollback()
        winner = (
            _idempotent_match(
                session,
                idempotency_key=idempotency_key,
                fingerprint=idempotency_fingerprint,
            )
            if idempotency_key
            else None
        )
        if winner is None:
            raise
        return _resume_existing_job(session, winner, settings=active_settings)

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

    return _enqueue_or_fail(session, job, settings=active_settings)


def created_response(job: Job) -> JobCreatedResponse:
    return JobCreatedResponse(job_id=job.id, status=job.status)


def cancel_queued_rq_job(job: Job, *, settings: Settings) -> None:
    if not job.queue_job_id:
        return

    client = Redis.from_url(settings.redis_url)
    try:
        rq_job = RQJob.fetch(job.queue_job_id, connection=client)
        rq_job.cancel()
    except NoSuchJobError:
        return
    except Exception as exc:
        raise KnownOperationError(
            "QUEUE_CANCEL_FAILED",
            "The queued job could not be canceled.",
            details={"reason": str(exc), "queue_job_id": job.queue_job_id},
            retryable=True,
        ) from exc
    finally:
        client.close()


def cancel_job(
    session: Session,
    *,
    job_id: str,
    settings: Settings | None = None,
) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise KnownOperationError(
            "JOB_NOT_FOUND",
            "The requested job does not exist.",
            details={"job_id": job_id},
        )

    if job.status == JobStatus.CANCELED.value:
        return job

    if job.status != JobStatus.QUEUED.value:
        raise KnownOperationError(
            "JOB_NOT_CANCELABLE",
            "Only queued jobs can be canceled.",
            details={"job_id": job.id, "status": job.status},
        )

    cancel_queued_rq_job(job, settings=settings or get_settings())
    job.status = JobStatus.CANCELED.value
    job.finished_at = datetime.now(UTC)
    session.add(job)
    add_audit_event(
        session,
        job_id=job.id,
        event_type="job.canceled",
        payload={"queue_job_id": job.queue_job_id},
    )
    session.commit()
    safe_queue_terminal_job_webhooks(
        job_id=job.id,
        event_type="job.canceled",
        settings=settings or get_settings(),
    )
    return job


def _job_error(job: Job) -> dict[str, Any] | None:
    if not job.error_code and not job.error_message:
        return None
    return {
        "code": job.error_code or "JOB_FAILED",
        "message": job.error_message or "The job failed.",
        "details": {},
        "retryable": False,
    }


def job_warnings(job: Job) -> list[dict[str, Any]]:
    if not job.validation:
        return []
    return job.validation.get("warnings") or []


def _job_summary(job: Job) -> JobSummaryResponse:
    return JobSummaryResponse(
        job_id=job.id,
        operation=job.operation,
        status=job.status,
        parameters=job.parameters,
        output_count=len(job.outputs),
        warning_count=len(job_warnings(job)),
        error=_job_error(job),
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def list_jobs_for_response(
    session: Session,
    *,
    status_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JobListResponse:
    validated_status = validate_job_status_filter(status_filter)
    statement = (
        select(Job)
        .options(selectinload(Job.outputs))
        .order_by(Job.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if validated_status is not None:
        statement = statement.where(Job.status == validated_status)

    jobs = list(session.scalars(statement))
    return JobListResponse(
        jobs=[_job_summary(job) for job in jobs],
        count=len(jobs),
        limit=limit,
        offset=offset,
    )


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
            status=output.document.status,
            # A purged output keeps its provenance but has no bytes left to sign a URL for.
            download_url=(
                None
                if is_deleted(output.document)
                else storage.presigned_download_url(
                    key=output.document.storage_key,
                    filename=output.document.original_filename,
                )
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
    return JobStatusResponse(
        job_id=job.id,
        operation=job.operation,
        status=job.status,
        parameters=job.parameters,
        outputs=outputs,
        validation=job.validation,
        warnings=job_warnings(job),
        error=_job_error(job),
        audit=audit,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )
