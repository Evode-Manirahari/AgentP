import importlib
from datetime import UTC, datetime

import pytest

for _module_name in ["pydantic_settings", "redis", "rq", "sqlalchemy"]:
    pytest.importorskip(_module_name)

config = importlib.import_module("app.config")
models = importlib.import_module("app.models")
documents = importlib.import_module("app.services.documents")
jobs = importlib.import_module("app.services.jobs")
KnownOperationError = importlib.import_module("app.operations.base").KnownOperationError


class FakeSession:
    def __init__(self, document: object | None, *, blocking_job_ids: list[str] | None = None):
        self.document = document
        self.blocking_job_ids = blocking_job_ids or []
        self.added: list[object] = []
        self.commits = 0

    def get(self, model: object, item_id: str) -> object | None:
        return self.document

    def scalars(self, statement: object) -> list[str]:
        return list(self.blocking_job_ids)

    def add(self, item: object) -> None:
        self.added.append(item)

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1


class RecordingStorage:
    def __init__(self, settings: object) -> None:
        self.settings = settings

    deleted_keys: list[str] = []

    def delete_object(self, *, key: str) -> None:
        RecordingStorage.deleted_keys.append(key)


@pytest.fixture(autouse=True)
def _reset_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingStorage.deleted_keys = []
    monkeypatch.setattr(documents, "StorageService", RecordingStorage)


def _document(
    *,
    status: str = "validated",
    source_job_id: str | None = None,
    deleted_at: datetime | None = None,
) -> object:
    return models.Document(
        id="file_123",
        original_filename="statement.pdf",
        mime_type="application/pdf",
        size_bytes=2048,
        sha256="0" * 64,
        storage_key="inputs/file_123/abc-statement.pdf",
        page_count=3,
        status=status,
        source_job_id=source_job_id,
        created_at=datetime.now(UTC),
        deleted_at=deleted_at,
    )


def _settings() -> object:
    return config.Settings()


def test_deleting_purges_the_bytes_and_keeps_the_record() -> None:
    document = _document()
    session = FakeSession(document)

    response = documents.delete_document(session, file_id="file_123", settings=_settings())

    assert RecordingStorage.deleted_keys == ["inputs/file_123/abc-statement.pdf"]
    assert response.status == "deleted"
    assert response.deleted_at is not None
    assert document.status == models.DocumentStatus.DELETED.value
    # Provenance survives: the row, its checksum, and its filename are still there.
    assert document.sha256 == "0" * 64
    assert document.original_filename == "statement.pdf"
    assert session.commits == 1


def test_deleting_is_idempotent() -> None:
    already = datetime.now(UTC)
    document = _document(status="deleted", deleted_at=already)
    session = FakeSession(document)

    response = documents.delete_document(session, file_id="file_123", settings=_settings())

    assert response.status == "deleted"
    assert response.deleted_at == already
    assert RecordingStorage.deleted_keys == []
    assert session.commits == 0


def test_deleting_an_unknown_file_is_a_not_found() -> None:
    with pytest.raises(KnownOperationError) as exc:
        documents.delete_document(FakeSession(None), file_id="file_missing", settings=_settings())

    assert exc.value.code == "FILE_NOT_FOUND"


def test_a_file_an_unfinished_job_needs_cannot_be_deleted() -> None:
    session = FakeSession(_document(), blocking_job_ids=["job_a", "job_b"])

    with pytest.raises(KnownOperationError) as exc:
        documents.delete_document(session, file_id="file_123", settings=_settings())

    assert exc.value.code == "FILE_IN_USE"
    assert exc.value.details["job_ids"] == ["job_a", "job_b"]
    # The bytes a running job still needs must survive the refusal.
    assert RecordingStorage.deleted_keys == []
    assert session.commits == 0


def test_deleting_an_output_is_recorded_on_the_job_that_produced_it() -> None:
    session = FakeSession(_document(source_job_id="job_123"))

    documents.delete_document(session, file_id="file_123", settings=_settings())

    events = [item for item in session.added if isinstance(item, models.AuditEvent)]
    assert [event.event_type for event in events] == ["output.deleted"]
    assert events[0].job_id == "job_123"
    assert events[0].payload["file_id"] == "file_123"


def test_deleting_an_uploaded_input_records_no_job_event() -> None:
    session = FakeSession(_document(source_job_id=None))

    documents.delete_document(session, file_id="file_123", settings=_settings())

    assert [item for item in session.added if isinstance(item, models.AuditEvent)] == []


def test_active_job_statuses_cover_everything_before_a_terminal_state() -> None:
    terminal = jobs.TERMINAL_JOB_STATUSES
    every_status = {status.value for status in models.JobStatus}

    assert set(documents.ACTIVE_JOB_STATUSES) == every_status - terminal


class _StubStorage:
    def presigned_download_url(self, *, key: str, filename: str) -> str:
        return f"https://example.invalid/{key}"


def _job_with_output(document: object) -> object:
    job = models.Job(
        id="job_123",
        operation="prepare_packet",
        status=models.JobStatus.SUCCEEDED.value,
        parameters={},
        created_at=datetime.now(UTC),
    )
    output = models.JobOutput(id="jout_1", job_id=job.id, document_id=document.id, position=0)
    output.document = document
    job.outputs = [output]
    job.audit_events = []
    return job


def test_a_job_stops_offering_a_download_url_for_a_purged_output() -> None:
    document = _document(status="deleted", source_job_id="job_123", deleted_at=datetime.now(UTC))

    response = jobs.build_job_response(job=_job_with_output(document), storage=_StubStorage())

    assert response.outputs[0].status == "deleted"
    assert response.outputs[0].download_url is None
    # The output is still listed, so the job's record of what it produced stays complete.
    assert response.outputs[0].file_id == "file_123"
    assert response.outputs[0].filename == "statement.pdf"


def test_a_live_output_still_has_a_download_url() -> None:
    response = jobs.build_job_response(job=_job_with_output(_document()), storage=_StubStorage())

    assert response.outputs[0].status == "validated"
    assert response.outputs[0].download_url is not None
