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


class OperationParameterResponse(BaseModel):
    name: str
    type: str
    required: bool = False
    default: Any | None = None
    allowed_values: list[Any] | None = None
    description: str


class OperationSpecResponse(BaseModel):
    name: OperationName
    description: str
    input_count_min: int
    input_count_max: int | None
    parameters: list[OperationParameterResponse] = Field(default_factory=list)


class OperationsResponse(BaseModel):
    operations: list[OperationSpecResponse]


class JobInputRef(BaseModel):
    file_id: str


class JobCreate(BaseModel):
    operation: OperationName
    inputs: list[JobInputRef] = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)


class JobCreatedResponse(BaseModel):
    job_id: str
    status: str


class JobSummaryResponse(BaseModel):
    job_id: str
    operation: str
    status: str
    parameters: dict[str, Any]
    output_count: int = 0
    error: dict[str, Any] | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobListResponse(BaseModel):
    jobs: list[JobSummaryResponse]
    count: int
    limit: int
    offset: int


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
