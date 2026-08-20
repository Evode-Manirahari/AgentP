from __future__ import annotations

import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Document, DocumentStatus, Job, JobInput, JobOutput, JobStatus
from app.operations.base import KnownOperationError, OperationOutput
from app.operations.executor import execute_operation
from app.operations.pdf_utils import sha256_path
from app.services.audit import add_audit_event
from app.services.storage import StorageService
from app.services.usage import enforce_document_quota
from app.services.validation import validate_input_pdf, validate_operation_result
from app.services.webhooks import deliver_webhook_delivery, safe_queue_terminal_job_webhooks

logger = logging.getLogger(__name__)

TERMINAL_JOB_STATUSES = {
    JobStatus.SUCCEEDED.value,
    JobStatus.COMPLETED_WITH_WARNINGS.value,
    JobStatus.FAILED.value,
    JobStatus.CANCELED.value,
}


def _terminal_success_outcome(warnings: list[dict]) -> tuple[str, str]:
    # Every assertion passed. Warnings describe the inputs, not a broken output, so they
    # change the terminal status without failing the job.
    if warnings:
        return JobStatus.COMPLETED_WITH_WARNINGS.value, "job.completed_with_warnings"
    return JobStatus.SUCCEEDED.value, "job.succeeded"


def _mark_failed(job_id: str, error: KnownOperationError) -> None:
    with SessionLocal() as session:
        job = session.get(Job, job_id)
        if job is None:
            return
        job.status = JobStatus.FAILED.value
        job.error_code = error.code
        job.error_message = error.message
        job.finished_at = datetime.now(UTC)
        session.add(job)
        add_audit_event(
            session,
            workspace_id=job.workspace_id,
            job_id=job.id,
            event_type="job.failed",
            payload=error.to_dict()["error"],
        )
        session.commit()
    safe_queue_terminal_job_webhooks(job_id=job_id, event_type="job.failed")


def _load_inputs(job_id: str, *, workspace_id: str) -> list[JobInput]:
    with SessionLocal() as session:
        return list(
            session.scalars(
                select(JobInput)
                .join(Job, Job.id == JobInput.job_id)
                .where(JobInput.job_id == job_id)
                .where(Job.workspace_id == workspace_id)
                .order_by(JobInput.position)
            )
        )


def _stage_outputs(
    storage: StorageService,
    *,
    workspace_id: str,
    job_id: str,
    outputs: list[OperationOutput],
) -> list[dict[str, Any]]:
    """Write every output to storage and describe it, before any row lock is taken.

    Reserving document quota locks the workspace row and holds it until the registration
    transaction commits. Uploading underneath that lock would block every upload and job
    completion in the workspace for the duration of an S3 round trip, while pinning a
    pooled database connection. Output keys derive only from the job and filename, so the
    bytes can be written before any document row exists.
    """
    staged: list[dict[str, Any]] = []
    for position, output in enumerate(outputs):
        storage_key = storage.output_key(
            workspace_id=workspace_id,
            job_id=job_id,
            filename=output.filename,
        )
        storage.upload_path(output.path, key=storage_key, content_type=output.mime_type)
        staged.append(
            {
                "position": position,
                "output": output,
                "storage_key": storage_key,
                "sha256": sha256_path(output.path),
                "size_bytes": output.path.stat().st_size,
            }
        )
    return staged


def _discard_staged_outputs(
    storage: StorageService,
    staged: list[dict[str, Any]],
    *,
    registered: bool,
) -> None:
    """Drop staged objects when the job never registered them.

    Staging writes bytes before the registration transaction, so a failure between the two
    would leave objects that no document row points at, and the retention sweep only works
    from document records. Best effort, and never raised: the job is already failing, and a
    cleanup error must not replace the reason it failed.
    """
    if registered:
        return
    for item in staged:
        try:
            storage.delete_object(key=item["storage_key"])
        except Exception:
            logger.exception(
                "Could not discard the orphaned storage object %s.", item["storage_key"]
            )


def process_job(job_id: str) -> None:
    settings = get_settings()
    storage = StorageService(settings)
    staged: list[dict[str, Any]] = []
    registered = False

    try:
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise KnownOperationError(
                    "JOB_NOT_FOUND",
                    "The queued job no longer exists.",
                    details={"job_id": job_id},
                    retryable=False,
                )
            if job.status in TERMINAL_JOB_STATUSES:
                return
            job.status = JobStatus.RUNNING.value
            job.started_at = datetime.now(UTC)
            session.add(job)
            add_audit_event(
                session,
                workspace_id=job.workspace_id,
                job_id=job.id,
                event_type="job.running",
            )
            session.commit()
            workspace_id = job.workspace_id
            operation = job.operation
            parameters = dict(job.parameters)

        with tempfile.TemporaryDirectory(prefix=f"{settings.temp_prefix}{job_id}-") as tmp_dir:
            workspace = Path(tmp_dir)
            input_paths: list[Path] = []
            input_names: list[str] = []
            input_validations: list[dict] = []
            input_labels = parameters.get("input_labels")

            for job_input in _load_inputs(job_id, workspace_id=workspace_id):
                with SessionLocal() as session:
                    document = session.scalar(
                        select(Document)
                        .where(Document.id == job_input.document_id)
                        .where(Document.workspace_id == workspace_id)
                    )
                    if document is None:
                        raise KnownOperationError(
                            "FILE_NOT_FOUND",
                            "An input file no longer exists.",
                            details={"file_id": job_input.document_id},
                        )
                    path = workspace / f"input-{job_input.position + 1}.pdf"
                    storage.download_to_path(key=document.storage_key, path=path)
                    validation = validate_input_pdf(path, settings=settings)
                    input_paths.append(path)
                    input_names.append(document.original_filename)
                    input_validation = {
                        "file_id": document.id,
                        "position": job_input.position,
                        "page_count": validation["page_count"],
                        "qpdf_check": validation["qpdf_check"]["status"],
                    }
                    if isinstance(input_labels, list) and job_input.position < len(input_labels):
                        input_validation["label"] = input_labels[job_input.position]
                    input_validations.append(input_validation)

            with SessionLocal() as session:
                job = session.get(Job, job_id)
                if job is None:
                    raise KnownOperationError("JOB_NOT_FOUND", "The job no longer exists.")
                add_audit_event(
                    session,
                    workspace_id=job.workspace_id,
                    job_id=job.id,
                    event_type="inputs.validated",
                    payload={"inputs": input_validations},
                )
                session.commit()

            result = execute_operation(
                operation=operation,
                input_paths=input_paths,
                parameters=parameters,
                workspace=workspace,
                settings=settings,
                input_names=input_names,
            )

            with SessionLocal() as session:
                job = session.get(Job, job_id)
                if job is None:
                    raise KnownOperationError("JOB_NOT_FOUND", "The job no longer exists.")
                job.status = JobStatus.VALIDATING.value
                session.add(job)
                add_audit_event(
                    session,
                    workspace_id=job.workspace_id,
                    job_id=job.id,
                    event_type="operation.completed",
                    payload={"metadata": result.metadata},
                )
                session.commit()

            validation = validate_operation_result(
                operation=operation,
                input_paths=input_paths,
                result=result,
                settings=settings,
            )

            staged = _stage_outputs(
                storage,
                workspace_id=workspace_id,
                job_id=job_id,
                outputs=result.outputs,
            )

            with SessionLocal() as session:
                job = session.get(Job, job_id)
                if job is None:
                    raise KnownOperationError("JOB_NOT_FOUND", "The job no longer exists.")

                enforce_document_quota(
                    session,
                    workspace_id=job.workspace_id,
                    incoming_bytes=sum(item["size_bytes"] for item in staged),
                    incoming_documents=len(staged),
                    settings=settings,
                )

                output_file_ids: list[str] = []
                for item in staged:
                    position = item["position"]
                    output = item["output"]
                    output_validation = validation["outputs"][position]
                    page_count = output_validation.get("page_count", output.page_count)
                    document = Document(
                        workspace_id=job.workspace_id,
                        original_filename=output.filename,
                        mime_type=output.mime_type,
                        size_bytes=item["size_bytes"],
                        sha256=item["sha256"],
                        storage_key=item["storage_key"],
                        page_count=page_count,
                        status=DocumentStatus.VALIDATED.value,
                        source_job_id=job.id,
                    )
                    session.add(document)
                    session.flush()
                    session.add(
                        JobOutput(job_id=job.id, document_id=document.id, position=position)
                    )
                    output_file_ids.append(document.id)

                warnings = validation.get("warnings") or []
                job.status, event_type = _terminal_success_outcome(warnings)
                job.validation = validation
                job.finished_at = datetime.now(UTC)
                session.add(job)
                add_audit_event(
                    session,
                    workspace_id=job.workspace_id,
                    job_id=job.id,
                    event_type=event_type,
                    payload={
                        "output_file_ids": output_file_ids,
                        "validation": validation,
                        "warnings": warnings,
                    },
                )
                session.commit()
                registered = True
                safe_queue_terminal_job_webhooks(
                    job_id=job.id,
                    event_type=event_type,
                    settings=settings,
                )
    except KnownOperationError as exc:
        _discard_staged_outputs(storage, staged, registered=registered)
        _mark_failed(job_id, exc)
        raise
    except Exception as exc:
        error = KnownOperationError(
            "UNEXPECTED_WORKER_ERROR",
            "The worker failed unexpectedly.",
            details={"reason": str(exc)},
            retryable=True,
        )
        _discard_staged_outputs(storage, staged, registered=registered)
        _mark_failed(job_id, error)
        raise


def process_webhook_delivery(delivery_id: str) -> None:
    deliver_webhook_delivery(delivery_id)
