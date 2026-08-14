from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db import Base


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    VALIDATED = "validated"
    REJECTED = "rejected"
    DELETED = "deleted"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    VALIDATING = "validating"
    SUCCEEDED = "succeeded"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    CANCELED = "canceled"


class WebhookDeliveryStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


DEFAULT_WORKSPACE_ID = "ws_default"


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("ws"))
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    api_keys: Mapped[list[ApiKey]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_api_keys_token_hash"),
        Index("ix_api_keys_workspace_revoked", "workspace_id", "revoked_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("key"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # Only the hash is stored. The token itself is shown once, at creation.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    prefix: Mapped[str] = mapped_column(String(32), nullable=False)
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    workspace: Mapped[Workspace] = relationship(back_populates="api_keys")


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (Index("ix_documents_workspace_created", "workspace_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("file"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    original_filename: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=DocumentStatus.UPLOADED.value
    )
    source_job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_jobs_workspace_idempotency_key",
        ),
        Index("ix_jobs_status_created", "status", "created_at"),
        Index("ix_jobs_workspace_status_created", "workspace_id", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("job"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=JobStatus.QUEUED.value)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    idempotency_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    queue_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    validation: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    inputs: Mapped[list[JobInput]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobInput.position"
    )
    outputs: Mapped[list[JobOutput]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="JobOutput.position"
    )
    audit_events: Mapped[list[AuditEvent]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="AuditEvent.created_at"
    )


class JobInput(Base):
    __tablename__ = "job_inputs"
    __table_args__ = (UniqueConstraint("job_id", "position", name="uq_job_inputs_position"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("jin"))
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    job: Mapped[Job] = relationship(back_populates="inputs")
    document: Mapped[Document] = relationship()


class JobOutput(Base):
    __tablename__ = "job_outputs"
    __table_args__ = (UniqueConstraint("job_id", "position", name="uq_job_outputs_position"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("jout"))
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    job: Mapped[Job] = relationship(back_populates="outputs")
    document: Mapped[Document] = relationship()


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_job_created", "job_id", "created_at"),
        Index("ix_audit_events_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("evt"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    job: Mapped[Job] = relationship(back_populates="audit_events")


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        Index("ix_webhook_endpoints_active", "active"),
        Index("ix_webhook_endpoints_workspace_active", "workspace_id", "active"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("wh"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    secret: Mapped[str] = mapped_column(String(128), nullable=False)
    events: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    deliveries: Mapped[list[WebhookDelivery]] = relationship(back_populates="endpoint")


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        Index("ix_webhook_deliveries_endpoint_created", "endpoint_id", "created_at"),
        Index("ix_webhook_deliveries_job_created", "job_id", "created_at"),
        Index("ix_webhook_deliveries_status_created", "status", "created_at"),
        Index("ix_webhook_deliveries_workspace_created", "workspace_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("whd"))
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("webhook_endpoints.id"), nullable=False)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=WebhookDeliveryStatus.PENDING.value
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    endpoint: Mapped[WebhookEndpoint] = relationship(back_populates="deliveries")
    job: Mapped[Job] = relationship()
