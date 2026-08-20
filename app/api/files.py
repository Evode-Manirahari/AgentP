from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.api.errors import operation_http_error
from app.config import Settings, get_settings
from app.db import get_session
from app.models import Document, DocumentStatus, new_id
from app.operations.base import KnownOperationError
from app.operations.pdf_utils import sha256_path
from app.schemas import (
    DocumentDeleteResponse,
    DownloadResponse,
    FileListResponse,
    FileUploadResponse,
)
from app.services.auth import AuthContext, require_auth_context
from app.services.documents import delete_document, is_deleted, list_documents_for_response
from app.services.storage import StorageService
from app.services.usage import enforce_document_quota
from app.services.validation import validate_input_pdf

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/files",
    tags=["files"],
    dependencies=[Depends(require_auth_context)],
)


def _file_not_found(file_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "error": {
                "code": "FILE_NOT_FOUND",
                "message": "The requested file does not exist.",
                "details": {"file_id": file_id},
                "retryable": False,
            }
        },
    )


def _get_document_or_404(session: Session, file_id: str, *, workspace_id: str) -> Document:
    document = session.scalar(
        select(Document)
        .where(Document.id == file_id)
        .where(Document.workspace_id == workspace_id)
    )
    if document is None:
        raise _file_not_found(file_id)
    return document


def _get_readable_document(
    session: Session,
    file_id: str,
    *,
    workspace_id: str,
) -> Document:
    document = _get_document_or_404(session, file_id, workspace_id=workspace_id)
    if is_deleted(document):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={
                "error": {
                    "code": "FILE_DELETED",
                    "message": "The file was deleted and its contents are no longer stored.",
                    "details": {
                        "file_id": document.id,
                        "deleted_at": (
                            document.deleted_at.isoformat() if document.deleted_at else None
                        ),
                    },
                    "retryable": False,
                }
            },
        )
    return document


def _discard_stored_object(storage: StorageService, key: str | None) -> None:
    """Drop an object whose owning row was never committed.

    Storing before reserving quota means a rejected upload can leave bytes behind with
    nothing referencing them. Nothing sweeps an object that no document row points at, so
    the compensating delete happens here. It is best effort: the upload already failed, and
    reporting a cleanup failure instead of the real cause would hide it.
    """
    if key is None:
        return
    try:
        storage.delete_object(key=key)
    except Exception:
        logger.exception("Could not discard the orphaned storage object %s.", key)


@router.post("", response_model=FileUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_file(
    file: Annotated[UploadFile, File(...)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    context: Annotated[AuthContext, Depends(require_auth_context)],
) -> FileUploadResponse:
    storage = StorageService(settings)
    filename = file.filename or "document.pdf"
    total_bytes = 0

    with tempfile.NamedTemporaryFile(
        prefix=settings.temp_prefix,
        suffix=".pdf",
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > settings.max_upload_bytes:
                tmp.close()
                tmp_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail={
                        "error": {
                            "code": "UPLOAD_TOO_LARGE",
                            "message": "The uploaded file exceeds the configured size limit.",
                            "details": {
                                "max_upload_bytes": settings.max_upload_bytes,
                                "received_bytes": total_bytes,
                            },
                            "retryable": False,
                        }
                    },
                )
            tmp.write(chunk)

    # The identifier is generated here rather than by the insert, so the object can be
    # stored before any row lock is taken. Reserving quota locks the workspace row and holds
    # it until this request commits; uploading underneath that lock would serialize every
    # upload and job completion in the workspace behind an S3 round trip, and hold a pooled
    # database connection for its duration.
    document_id = new_id("file")
    stored_key: str | None = None

    try:
        validation = validate_input_pdf(tmp_path, settings=settings)
        sha256 = sha256_path(tmp_path)
        storage_key = storage.input_key(
            workspace_id=context.workspace_id,
            document_id=document_id,
            filename=filename,
        )
        storage.upload_path(tmp_path, key=storage_key, content_type=validation["mime_type"])
        stored_key = storage_key

        enforce_document_quota(
            session,
            workspace_id=context.workspace_id,
            incoming_bytes=total_bytes,
            incoming_documents=1,
            settings=settings,
        )
        document = Document(
            id=document_id,
            workspace_id=context.workspace_id,
            original_filename=filename,
            mime_type=validation["mime_type"],
            size_bytes=total_bytes,
            sha256=sha256,
            storage_key=storage_key,
            page_count=validation["page_count"],
            status=DocumentStatus.VALIDATED.value,
        )
        session.add(document)
        session.commit()
        return FileUploadResponse(
            file_id=document.id,
            filename=document.original_filename,
            sha256=document.sha256,
            page_count=document.page_count or 0,
            status=document.status,
        )
    except KnownOperationError as exc:
        session.rollback()
        _discard_stored_object(storage, stored_key)
        status_code = status.HTTP_409_CONFLICT if exc.code.startswith("WORKSPACE_") else 400
        raise operation_http_error(exc, status_code=status_code) from exc
    except Exception:
        session.rollback()
        _discard_stored_object(storage, stored_key)
        raise
    finally:
        tmp_path.unlink(missing_ok=True)


@router.get("", response_model=FileListResponse)
def list_files(
    session: Annotated[Session, Depends(get_session)],
    context: Annotated[AuthContext, Depends(require_auth_context)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FileListResponse:
    try:
        return list_documents_for_response(
            session,
            workspace_id=context.workspace_id,
            status_filter=status_filter,
            limit=limit,
            offset=offset,
        )
    except KnownOperationError as exc:
        raise operation_http_error(exc) from exc


@router.delete("/{file_id}", response_model=DocumentDeleteResponse)
def delete_file(
    file_id: str,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    context: Annotated[AuthContext, Depends(require_auth_context)],
) -> DocumentDeleteResponse:
    try:
        return delete_document(
            session,
            file_id=file_id,
            workspace_id=context.workspace_id,
            settings=settings,
        )
    except KnownOperationError as exc:
        status_code = status.HTTP_400_BAD_REQUEST
        if exc.code == "FILE_NOT_FOUND":
            status_code = status.HTTP_404_NOT_FOUND
        if exc.code == "FILE_IN_USE":
            status_code = status.HTTP_409_CONFLICT
        raise operation_http_error(exc, status_code=status_code) from exc


@router.get("/{file_id}/download", response_model=DownloadResponse)
def download_file(
    file_id: str,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    context: Annotated[AuthContext, Depends(require_auth_context)],
) -> DownloadResponse:
    document = _get_readable_document(
        session,
        file_id,
        workspace_id=context.workspace_id,
    )
    storage = StorageService(settings)
    return DownloadResponse(
        file_id=document.id,
        download_url=storage.presigned_download_url(
            key=document.storage_key,
            filename=document.original_filename,
        ),
        expires_in_seconds=settings.download_url_expires_seconds,
    )


@router.get("/{file_id}/content", response_class=FileResponse)
def download_file_content(
    file_id: str,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    context: Annotated[AuthContext, Depends(require_auth_context)],
) -> FileResponse:
    document = _get_readable_document(
        session,
        file_id,
        workspace_id=context.workspace_id,
    )
    suffix = Path(document.original_filename).suffix or ".pdf"

    with tempfile.NamedTemporaryFile(
        prefix=settings.temp_prefix,
        suffix=suffix,
        delete=False,
    ) as tmp:
        tmp_path = Path(tmp.name)

    try:
        StorageService(settings).download_to_path(key=document.storage_key, path=tmp_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return FileResponse(
        path=tmp_path,
        filename=document.original_filename,
        media_type=document.mime_type,
        background=BackgroundTask(lambda: tmp_path.unlink(missing_ok=True)),
    )
