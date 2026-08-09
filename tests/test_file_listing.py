import importlib
from datetime import UTC, datetime, timedelta

import pytest

for _module_name in ["pydantic_settings", "redis", "rq", "sqlalchemy"]:
    pytest.importorskip(_module_name)

models = importlib.import_module("app.models")
documents = importlib.import_module("app.services.documents")
KnownOperationError = importlib.import_module("app.operations.base").KnownOperationError


class FakeSession:
    def __init__(self, found: list[object]) -> None:
        self.found = found
        self.statement: object = None

    def scalars(self, statement: object) -> list[object]:
        self.statement = statement
        return list(self.found)


def _document(
    file_id: str,
    *,
    status: str = "validated",
    source_job_id: str | None = None,
    deleted_at: datetime | None = None,
    created_at: datetime | None = None,
) -> object:
    return models.Document(
        id=file_id,
        original_filename=f"{file_id}.pdf",
        mime_type="application/pdf",
        size_bytes=4096,
        sha256="a" * 64,
        storage_key=f"inputs/{file_id}/doc.pdf",
        page_count=2,
        status=status,
        source_job_id=source_job_id,
        created_at=created_at or datetime.now(UTC),
        deleted_at=deleted_at,
    )


def test_listing_summarizes_each_document() -> None:
    created = datetime.now(UTC)
    session = FakeSession([_document("file_1", source_job_id="job_9", created_at=created)])

    response = documents.list_documents_for_response(session)

    assert response.count == 1
    assert response.limit == 50
    assert response.offset == 0
    summary = response.files[0]
    assert summary.file_id == "file_1"
    assert summary.filename == "file_1.pdf"
    assert summary.mime_type == "application/pdf"
    assert summary.size_bytes == 4096
    assert summary.sha256 == "a" * 64
    assert summary.page_count == 2
    assert summary.status == "validated"
    assert summary.source_job_id == "job_9"
    assert summary.created_at == created
    assert summary.deleted_at is None


def test_listing_reports_deleted_documents_with_their_timestamp() -> None:
    purged_at = datetime.now(UTC) - timedelta(hours=2)
    session = FakeSession([_document("file_1", status="deleted", deleted_at=purged_at)])

    response = documents.list_documents_for_response(session)

    assert response.files[0].status == "deleted"
    assert response.files[0].deleted_at == purged_at
    # The checksum outlives the bytes, so a purge is still auditable from the listing.
    assert response.files[0].sha256 == "a" * 64


def test_listing_echoes_the_requested_window() -> None:
    session = FakeSession([_document("file_1"), _document("file_2")])

    response = documents.list_documents_for_response(session, limit=2, offset=10)

    assert response.count == 2
    assert response.limit == 2
    assert response.offset == 10


def test_listing_is_empty_when_nothing_matches() -> None:
    response = documents.list_documents_for_response(FakeSession([]), status_filter="rejected")

    assert response.files == []
    assert response.count == 0


@pytest.mark.parametrize("status", ["uploaded", "validated", "rejected", "deleted"])
def test_every_document_status_is_an_accepted_filter(status: str) -> None:
    assert documents.validate_document_status_filter(status) == status


def test_no_filter_is_allowed() -> None:
    assert documents.validate_document_status_filter(None) is None


def test_an_unknown_status_filter_is_rejected() -> None:
    with pytest.raises(KnownOperationError) as exc:
        documents.validate_document_status_filter("purged")

    assert exc.value.code == "INVALID_DOCUMENT_STATUS"
    assert exc.value.details["allowed"] == ["deleted", "rejected", "uploaded", "validated"]


def test_a_job_status_is_not_a_valid_file_status() -> None:
    with pytest.raises(KnownOperationError) as exc:
        documents.validate_document_status_filter("succeeded")

    assert exc.value.code == "INVALID_DOCUMENT_STATUS"


def test_the_filter_set_tracks_the_model() -> None:
    assert documents.DOCUMENT_STATUSES == {status.value for status in models.DocumentStatus}


def test_paging_is_ordered_by_a_unique_tiebreak() -> None:
    # created_at is transaction time, so a job's outputs are written with byte-identical
    # timestamps. Ordering on it alone lets an offset repeat or skip a row between pages.
    session = FakeSession([_document("file_1")])

    documents.list_documents_for_response(session, limit=10, offset=10)

    order_by = str(session.statement).split("ORDER BY")[1]
    assert "documents.created_at DESC" in order_by
    assert "documents.id DESC" in order_by


def test_job_paging_uses_the_same_tiebreak() -> None:
    jobs = importlib.import_module("app.services.jobs")
    session = FakeSession([])

    jobs.list_jobs_for_response(session, limit=10, offset=10)

    order_by = str(session.statement).split("ORDER BY")[1]
    assert "jobs.created_at DESC" in order_by
    assert "jobs.id DESC" in order_by
