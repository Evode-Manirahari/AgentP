from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Document, DocumentStatus, Job, JobInput, JobOutput, JobStatus
from app.operations.base import KnownOperationError
from app.operations.executor import execute_operation
from app.operations.pdf_utils import sha256_path
from app.services.audit import add_audit_event
from app.services.storage import StorageService
from app.services.validation import validate_input_pdf, validate_operation_result
from app.services.webhooks import deliver_webhook_delivery, safe_queue_terminal_job_webhooks

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


def process_job(job_id: str) -> None:
    settings = get_settings()
    storage = StorageService(settings)

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

            with SessionLocal() as session:
                job = session.get(Job, job_id)
                if job is None:
                    raise KnownOperationError("JOB_NOT_FOUND", "The job no longer exists.")

                output_file_ids: list[str] = []
                for position, output in enumerate(result.outputs):
                    sha256 = sha256_path(output.path)
                    storage_key = storage.output_key(
                        workspace_id=job.workspace_id,
                        job_id=job.id,
                        filename=output.filename,
                    )
                    storage.upload_path(output.path, key=storage_key, content_type=output.mime_type)
                    output_validation = validation["outputs"][position]
                    page_count = output_validation.get("page_count", output.page_count)
                    document = Document(
                        workspace_id=job.workspace_id,
                        original_filename=output.filename,
                        mime_type=output.mime_type,
                        size_bytes=output.path.stat().st_size,
                        sha256=sha256,
                        storage_key=storage_key,
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
                safe_queue_terminal_job_webhooks(
                    job_id=job.id,
                    event_type=event_type,
                    settings=settings,
                )
    except KnownOperationError as exc:
        _mark_failed(job_id, exc)
        raise
    except Exception as exc:
        error = KnownOperationError(
            "UNEXPECTED_WORKER_ERROR",
            "The worker failed unexpectedly.",
            details={"reason": str(exc)},
            retryable=True,
        )
        _mark_failed(job_id, error)
        raise


def process_webhook_delivery(delivery_id: str) -> None:
    deliver_webhook_delivery(delivery_id)
