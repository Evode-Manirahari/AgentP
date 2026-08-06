from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

OperationName = Literal["merge", "split", "ocr", "compress", "extract_text"]


class FileUploadResponse(BaseModel):
    file_id: str
    filename: str
    sha256: str
    page_count: int
    status: str


class DownloadResponse(BaseModel):
    file_id: str
    download_url: str
    expires_in_seconds: int


class JobInputRef(BaseModel):
    file_id: str


class JobCreate(BaseModel):
    operation: OperationName
    inputs: list[JobInputRef] = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)


class JobCreatedResponse(BaseModel):
    job_id: str
    status: str


class JobOutputResponse(BaseModel):
    file_id: str
    filename: str
    mime_type: str
    page_count: int | None
    download_url: str


class AuditEventResponse(BaseModel):
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class JobStatusResponse(BaseModel):
    job_id: str
    operation: str
    status: str
    parameters: dict[str, Any]
    outputs: list[JobOutputResponse] = Field(default_factory=list)
    validation: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    audit: list[AuditEventResponse] = Field(default_factory=list)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False


class ErrorEnvelope(BaseModel):
    error: ErrorBody

