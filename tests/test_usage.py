from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

for _module_name in ["pydantic_settings", "sqlalchemy"]:
    pytest.importorskip(_module_name)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.config import Settings  # noqa: E402
from app.models import Document, DocumentStatus, Job, JobStatus, Workspace  # noqa: E402
from app.operations.base import KnownOperationError  # noqa: E402
from app.services.usage import (  # noqa: E402
    enforce_document_quota,
    enforce_job_quota,
    get_workspace_usage,
)


@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json(element: Any, compiler: Any, **kw: Any) -> str:
    return "JSON"


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    Workspace.__table__.create(engine)
    Job.__table__.create(engine)
    Document.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as active_session:
        active_session.add_all(
            [
                Workspace(id="ws_acme", name="Acme"),
                Workspace(id="ws_rival", name="Rival"),
            ]
        )
        active_session.commit()
        yield active_session
    engine.dispose()


def _document(
    file_id: str,
    *,
    workspace_id: str = "ws_acme",
    size_bytes: int = 100,
    status: str = DocumentStatus.VALIDATED.value,
) -> Document:
    return Document(
        id=file_id,
        workspace_id=workspace_id,
        original_filename=f"{file_id}.pdf",
        mime_type="application/pdf",
        size_bytes=size_bytes,
        sha256="a" * 64,
        storage_key=f"workspaces/{workspace_id}/{file_id}.pdf",
        page_count=1,
        status=status,
    )


def _job(
    job_id: str,
    *,
    status: str,
    created_at: datetime,
    workspace_id: str = "ws_acme",
) -> Job:
    return Job(
        id=job_id,
        workspace_id=workspace_id,
        operation="prepare_packet",
        status=status,
        parameters={},
        created_at=created_at,
    )


def test_usage_is_workspace_scoped_and_excludes_deleted_storage(session: Session) -> None:
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    session.add_all(
        [
            _document("file_live", size_bytes=125),
            _document("file_deleted", size_bytes=900, status=DocumentStatus.DELETED.value),
            _document("file_rival", workspace_id="ws_rival", size_bytes=5_000),
            _job("job_running", status=JobStatus.RUNNING.value, created_at=now),
            _job(
                "job_succeeded",
                status=JobStatus.SUCCEEDED.value,
                created_at=now - timedelta(minutes=10),
            ),
            _job(
                "job_failed",
                status=JobStatus.FAILED.value,
                created_at=now - timedelta(hours=2),
            ),
            _job(
                "job_old",
                status=JobStatus.FAILED.value,
                created_at=now - timedelta(days=2),
            ),
            _job(
                "job_rival",
                workspace_id="ws_rival",
                status=JobStatus.RUNNING.value,
                created_at=now,
            ),
        ]
    )
    session.commit()

    usage = get_workspace_usage(
        session,
        workspace_id="ws_acme",
        settings=Settings(
            workspace_storage_limit_bytes=1_000,
            workspace_document_limit=10,
            workspace_active_job_limit=5,
            workspace_jobs_per_hour_limit=20,
        ),
        now=now,
    )

    assert usage.storage_bytes.used == 125
    assert usage.storage_bytes.remaining == 875
    assert usage.documents.used == 1
    assert usage.active_jobs.used == 1
    assert usage.jobs_last_hour.used == 2
    assert usage.job_status_last_24_hours[JobStatus.SUCCEEDED.value] == 1
    assert usage.job_status_last_24_hours[JobStatus.FAILED.value] == 1
    assert usage.terminal_failure_rate_last_24_hours == 0.5


def test_document_quota_allows_capacity_and_rejects_storage_overrun(session: Session) -> None:
    session.add(_document("file_existing", size_bytes=80))
    session.commit()
    settings = Settings(
        workspace_storage_limit_bytes=100,
        workspace_document_limit=10,
    )

    enforce_document_quota(
        session,
        workspace_id="ws_acme",
        incoming_bytes=20,
        incoming_documents=1,
        settings=settings,
    )

    with pytest.raises(KnownOperationError) as exc:
        enforce_document_quota(
            session,
            workspace_id="ws_acme",
            incoming_bytes=21,
            incoming_documents=1,
            settings=settings,
        )

    assert exc.value.code == "WORKSPACE_STORAGE_LIMIT_EXCEEDED"
    assert exc.value.details["used_bytes"] == 80
    assert exc.value.details["incoming_bytes"] == 21


def test_document_quota_rejects_document_count_overrun(session: Session) -> None:
    session.add(_document("file_existing"))
    session.commit()

    with pytest.raises(KnownOperationError) as exc:
        enforce_document_quota(
            session,
            workspace_id="ws_acme",
            incoming_bytes=1,
            incoming_documents=1,
            settings=Settings(workspace_document_limit=1),
        )

    assert exc.value.code == "WORKSPACE_DOCUMENT_LIMIT_EXCEEDED"


def test_job_quota_rejects_too_many_active_jobs(session: Session) -> None:
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    session.add(_job("job_active", status=JobStatus.QUEUED.value, created_at=now))
    session.commit()

    with pytest.raises(KnownOperationError) as exc:
        enforce_job_quota(
            session,
            workspace_id="ws_acme",
            settings=Settings(workspace_active_job_limit=1),
            now=now,
        )

    assert exc.value.code == "WORKSPACE_ACTIVE_JOB_LIMIT_EXCEEDED"
    assert exc.value.retryable is True


def test_job_quota_rejects_hourly_throughput_after_active_jobs_finish(
    session: Session,
) -> None:
    now = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    session.add(
        _job(
            "job_recent",
            status=JobStatus.SUCCEEDED.value,
            created_at=now - timedelta(minutes=5),
        )
    )
    session.commit()

    with pytest.raises(KnownOperationError) as exc:
        enforce_job_quota(
            session,
            workspace_id="ws_acme",
            settings=Settings(workspace_jobs_per_hour_limit=1),
            now=now,
        )

    assert exc.value.code == "WORKSPACE_JOB_RATE_LIMIT_EXCEEDED"
    assert exc.value.details["window_seconds"] == 3600
