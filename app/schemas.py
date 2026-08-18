from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

OperationName = Literal["merge", "prepare_packet", "split", "ocr", "compress", "extract_text"]
WebhookEventName = Literal[
    "job.succeeded",
    "job.completed_with_warnings",
    "job.failed",
    "job.canceled",
]
WebhookDeliveryState = Literal["pending", "succeeded", "failed"]
DEFAULT_WEBHOOK_EVENTS: list[WebhookEventName] = [
    "job.succeeded",
    "job.completed_with_warnings",
    "job.failed",
    "job.canceled",
]


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


class DocumentDeleteResponse(BaseModel):
    file_id: str
    status: str
    deleted_at: datetime | None = None


class FileSummaryResponse(BaseModel):
    file_id: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    page_count: int | None = None
    status: str
    source_job_id: str | None = None
    created_at: datetime
    deleted_at: datetime | None = None


class FileListResponse(BaseModel):
    files: list[FileSummaryResponse]
    count: int
    limit: int
    offset: int


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
    label: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9._-]{0,63}$",
    )


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
    warning_count: int = 0
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
    status: str = "validated"
    download_url: str | None = None


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
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    audit: list[AuditEventResponse] = Field(default_factory=list)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class WebhookCreate(BaseModel):
    url: str = Field(max_length=2048)
    events: list[WebhookEventName] = Field(
        default_factory=lambda: list(DEFAULT_WEBHOOK_EVENTS),
        min_length=1,
    )
    active: bool = True

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Webhook URL must be an absolute http or https URL.")
        return value


class WebhookEndpointResponse(BaseModel):
    webhook_id: str
    url: str
    events: list[WebhookEventName]
    active: bool
    created_at: datetime


class WebhookCreateResponse(WebhookEndpointResponse):
    signing_secret: str


class WebhookEndpointListResponse(BaseModel):
    webhooks: list[WebhookEndpointResponse]
    count: int
    limit: int
    offset: int


class WebhookDeliveryResponse(BaseModel):
    delivery_id: str
    webhook_id: str
    job_id: str
    event_type: WebhookEventName
    status: WebhookDeliveryState
    attempts: int
    last_status_code: int | None = None
    last_error: str | None = None
    created_at: datetime
    delivered_at: datetime | None = None


class WebhookDeliveryListResponse(BaseModel):
    deliveries: list[WebhookDeliveryResponse]
    count: int
    limit: int
    offset: int


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    initial_key_name: str = Field(default="Default", min_length=1, max_length=200)

    @field_validator("name", "initial_key_name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Name must not be blank.")
        return normalized


class WorkspaceResponse(BaseModel):
    workspace_id: str
    name: str
    created_at: datetime


class UsageLimitResponse(BaseModel):
    used: int
    limit: int
    remaining: int
    utilization: float
    exhausted: bool


class WorkspaceUsageResponse(BaseModel):
    workspace_id: str
    generated_at: datetime
    storage_bytes: UsageLimitResponse
    documents: UsageLimitResponse
    active_jobs: UsageLimitResponse
    jobs_last_hour: UsageLimitResponse
    job_status_last_24_hours: dict[str, int]
    terminal_failure_rate_last_24_hours: float | None = None


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Name must not be blank.")
        return normalized


class ApiKeyResponse(BaseModel):
    api_key_id: str
    name: str
    prefix: str
    is_platform_admin: bool
    created_at: datetime
    revoked_at: datetime | None = None


class ApiKeyCreateResponse(ApiKeyResponse):
    token: str


class ApiKeyListResponse(BaseModel):
    api_keys: list[ApiKeyResponse]
    count: int
    limit: int
    offset: int


class WorkspaceCreateResponse(BaseModel):
    workspace: WorkspaceResponse
    api_key: ApiKeyCreateResponse


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False


class ErrorEnvelope(BaseModel):
    error: ErrorBody
