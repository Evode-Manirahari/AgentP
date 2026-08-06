from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.api.errors import operation_http_error
from app.config import Settings, get_settings
from app.db import get_session
from app.models import Document, DocumentStatus
from app.operations.base import KnownOperationError
from app.operations.pdf_utils import sha256_path
from app.schemas import DownloadResponse, FileUploadResponse
from app.services.auth import require_api_key
from app.services.storage import StorageService
from app.services.validation import validate_input_pdf

router = APIRouter(prefix="/files", tags=["files"], dependencies=[Depends(require_api_key)])


@router.post("", response_model=FileUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_file(
    file: Annotated[UploadFile, File(...)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
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

    try:
        validation = validate_input_pdf(tmp_path, settings=settings)
        sha256 = sha256_path(tmp_path)
        document = Document(
            original_filename=filename,
            mime_type=validation["mime_type"],
            size_bytes=total_bytes,
            sha256=sha256,
            storage_key="pending",
            page_count=validation["page_count"],
            status=DocumentStatus.VALIDATED.value,
        )
        session.add(document)
        session.flush()
        storage_key = storage.input_key(document_id=document.id, filename=filename)
        storage.upload_path(tmp_path, key=storage_key, content_type=validation["mime_type"])
        document.storage_key = storage_key
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
        raise operation_http_error(exc) from exc
    finally:
        tmp_path.unlink(missing_ok=True)


@router.get("/{file_id}/download", response_model=DownloadResponse)
def download_file(
    file_id: str,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DownloadResponse:
    document = session.get(Document, file_id)
    if document is None:
        raise HTTPException(
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

    storage = StorageService(settings)
    return DownloadResponse(
        file_id=document.id,
        download_url=storage.presigned_download_url(
            key=document.storage_key,
            filename=document.original_filename,
        ),
        expires_in_seconds=settings.download_url_expires_seconds,
    )
