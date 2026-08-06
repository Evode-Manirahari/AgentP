from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
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


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    VALIDATING = "validating"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("file"))
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


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
        Index("ix_jobs_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("job"))
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=JobStatus.QUEUED.value)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
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
    __table_args__ = (Index("ix_audit_events_job_created", "job_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("evt"))
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    job: Mapped[Job] = relationship(back_populates="audit_events")
