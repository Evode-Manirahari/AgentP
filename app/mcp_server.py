from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from app.config import get_settings
from app.db import SessionLocal
from app.operations.base import KnownOperationError
from app.schemas import JobCreate, JobInputRef
from app.services.auth import AuthContext, get_current_auth_context
from app.services.documents import list_documents_for_response
from app.services.jobs import (
    build_job_response,
    cancel_job,
    create_job,
    created_response,
    list_jobs_for_response,
    load_job_for_response,
)
from app.services.operations_catalog import list_operation_specs
from app.services.storage import StorageService
from app.services.usage import get_workspace_usage

mcp = MCPServer("AgentP Document Execution")


def _mcp_auth_error() -> dict[str, Any]:
    return KnownOperationError(
        "UNAUTHORIZED",
        "A valid, active X-API-Key header is required.",
    ).to_dict()


def _mcp_auth_context() -> AuthContext | None:
    return get_current_auth_context()


def _queued_envelope(job_id: str, status: str) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "status": status,
        "next_action": {
            "tool": "get_job",
            "arguments": {"job_id": job_id},
        },
    }


def _submit_job(
    *,
    operation: str,
    file_ids: list[str],
    parameters: dict[str, Any],
    idempotency_key: str | None,
    input_labels: list[str] | None = None,
) -> dict[str, Any]:
    context = _mcp_auth_context()
    if context is None:
        return _mcp_auth_error()
    try:
        if input_labels is not None and len(input_labels) != len(file_ids):
            raise KnownOperationError(
                "INVALID_PACKET_LABELS",
                "Manifest ordering requires exactly one label for every input document.",
                details={"input_count": len(file_ids), "label_count": len(input_labels)},
            )
        request = JobCreate(
            operation=operation,  # type: ignore[arg-type]
            inputs=[
                JobInputRef(
                    file_id=file_id,
                    label=input_labels[index] if input_labels is not None else None,
                )
                for index, file_id in enumerate(file_ids)
            ],
            parameters=parameters,
        )
        with SessionLocal() as session:
            job = create_job(
                session,
                workspace_id=context.workspace_id,
                request=request,
                idempotency_key=idempotency_key,
                settings=get_settings(),
            )
            response = created_response(job)
            return _queued_envelope(response.job_id, response.status)
    except KnownOperationError as exc:
        return exc.to_dict()


@mcp.tool()
def list_operations() -> dict[str, Any]:
    """List supported PDF operations, input counts, and parameter schemas."""
    if _mcp_auth_context() is None:
        return _mcp_auth_error()
    return {"operations": list_operation_specs()}


@mcp.tool()
def list_jobs(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List recent jobs with compact status summaries."""
    context = _mcp_auth_context()
    if context is None:
        return _mcp_auth_error()
    if limit < 1 or limit > 100:
        return KnownOperationError(
            "INVALID_LIMIT",
            "limit must be between 1 and 100.",
            details={"limit": limit},
        ).to_dict()
    if offset < 0:
        return KnownOperationError(
            "INVALID_OFFSET",
            "offset must be greater than or equal to zero.",
            details={"offset": offset},
        ).to_dict()

    try:
        with SessionLocal() as session:
            response = list_jobs_for_response(
                session,
                workspace_id=context.workspace_id,
                status_filter=status,
                limit=limit,
                offset=offset,
            )
            return response.model_dump(mode="json")
    except KnownOperationError as exc:
        return exc.to_dict()


@mcp.tool()
def list_files(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List uploaded and produced files, newest first, with their status and checksums."""
    context = _mcp_auth_context()
    if context is None:
        return _mcp_auth_error()
    if limit < 1 or limit > 100:
        return KnownOperationError(
            "INVALID_LIMIT",
            "limit must be between 1 and 100.",
            details={"limit": limit},
        ).to_dict()
    if offset < 0:
        return KnownOperationError(
            "INVALID_OFFSET",
            "offset must be greater than or equal to zero.",
            details={"offset": offset},
        ).to_dict()

    try:
        with SessionLocal() as session:
            response = list_documents_for_response(
                session,
                workspace_id=context.workspace_id,
                status_filter=status,
                limit=limit,
                offset=offset,
            )
            return response.model_dump(mode="json")
    except KnownOperationError as exc:
        return exc.to_dict()


@mcp.tool()
def get_usage() -> dict[str, Any]:
    """Get this workspace's storage, document, active-job, and hourly-job usage."""
    context = _mcp_auth_context()
    if context is None:
        return _mcp_auth_error()
    with SessionLocal() as session:
        response = get_workspace_usage(
            session,
            workspace_id=context.workspace_id,
            settings=get_settings(),
        )
        return response.model_dump(mode="json")


@mcp.tool(name="cancel_job")
def cancel_job_tool(job_id: str) -> dict[str, Any]:
    """Cancel a queued job before the worker starts processing it."""
    context = _mcp_auth_context()
    if context is None:
        return _mcp_auth_error()
    try:
        with SessionLocal() as session:
            job = cancel_job(
                session,
                workspace_id=context.workspace_id,
                job_id=job_id,
                settings=get_settings(),
            )
            response = build_job_response(job=job, storage=StorageService(get_settings()))
            return response.model_dump(mode="json")
    except KnownOperationError as exc:
        return exc.to_dict()


@mcp.tool()
def merge_pdfs(
    file_ids: list[str],
    ocr_if_needed: bool = False,
    language: str = "eng",
    deskew: bool = True,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Merge two or more uploaded PDFs, optionally OCRing scanned inputs first."""
    return _submit_job(
        operation="merge",
        file_ids=file_ids,
        parameters={"ocr_if_needed": ocr_if_needed, "language": language, "deskew": deskew},
        idempotency_key=idempotency_key,
    )


@mcp.tool()
def prepare_packet(
    file_ids: list[str],
    order: str = "as_provided",
    language: str = "eng",
    deskew: bool = True,
    input_labels: list[str] | None = None,
    manifest: list[dict[str, Any]] | None = None,
    allow_unlisted: bool = False,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Turn a collection of documents into one validated packet PDF plus an audit report.

    Inspects every input, OCRs the scanned ones, orders them ("as_provided", "filename",
    or by a semantic manifest), merges them, and verifies the packet contains every page.
    Manifest ordering requires one input_labels entry per file and a manifest section list.
    """
    parameters: dict[str, Any] = {
        "order": order,
        "language": language,
        "deskew": deskew,
    }
    if manifest is not None:
        parameters["manifest"] = manifest
    if allow_unlisted:
        parameters["allow_unlisted"] = True
    return _submit_job(
        operation="prepare_packet",
        file_ids=file_ids,
        parameters=parameters,
        idempotency_key=idempotency_key,
        input_labels=input_labels,
    )


@mcp.tool()
def split_pdf(
    file_id: str,
    page_ranges: list[str],
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Split a PDF into one output per requested page range."""
    return _submit_job(
        operation="split",
        file_ids=[file_id],
        parameters={"page_ranges": page_ranges},
        idempotency_key=idempotency_key,
    )


@mcp.tool()
def ocr_pdf(
    file_id: str,
    language: str = "eng",
    deskew: bool = True,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Create a searchable OCR PDF from an uploaded PDF."""
    return _submit_job(
        operation="ocr",
        file_ids=[file_id],
        parameters={"language": language, "deskew": deskew},
        idempotency_key=idempotency_key,
    )


@mcp.tool()
def compress_pdf(
    file_id: str,
    preset: str = "ebook",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Compress a PDF using one of: screen, ebook, print."""
    return _submit_job(
        operation="compress",
        file_ids=[file_id],
        parameters={"preset": preset},
        idempotency_key=idempotency_key,
    )


@mcp.tool()
def extract_text(
    file_id: str,
    include_coordinates: bool = False,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Extract page-level PDF text into a JSON artifact."""
    return _submit_job(
        operation="extract_text",
        file_ids=[file_id],
        parameters={"include_coordinates": include_coordinates},
        idempotency_key=idempotency_key,
    )


@mcp.tool()
def get_job(job_id: str) -> dict[str, Any]:
    """Fetch job status, output download URLs, validation, and audit events."""
    context = _mcp_auth_context()
    if context is None:
        return _mcp_auth_error()
    with SessionLocal() as session:
        job = load_job_for_response(
            session,
            job_id,
            workspace_id=context.workspace_id,
        )
        if job is None:
            return KnownOperationError(
                "JOB_NOT_FOUND",
                "The requested job does not exist.",
                details={"job_id": job_id},
            ).to_dict()
        response = build_job_response(job=job, storage=StorageService(get_settings()))
        return response.model_dump(mode="json")


mcp_app = mcp.streamable_http_app(
    streamable_http_path="/",
    json_response=True,
    stateless_http=True,
)
