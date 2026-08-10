import importlib
from datetime import UTC, datetime, timedelta

import pytest

for _module_name in ["pydantic_settings", "redis", "rq", "sqlalchemy"]:
    pytest.importorskip(_module_name)

config = importlib.import_module("app.config")
models = importlib.import_module("app.models")
documents = importlib.import_module("app.services.documents")

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


class FakeSession:
    """Serves the sweep's two queries in the order it makes them."""

    def __init__(self, *, expired: list[object], in_use_ids: list[str] | None = None) -> None:
        self.responses: list[list[object]] = [expired, list(in_use_ids or [])]
        self.added: list[object] = []
        self.commits = 0

    def scalars(self, statement: object) -> list[object]:
        if not self.responses:
            return []
        return self.responses.pop(0)

    def add(self, item: object) -> None:
        self.added.append(item)

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1


class RecordingStorage:
    deleted_keys: list[str] = []

    def __init__(self, settings: object) -> None:
        self.settings = settings

    def delete_object(self, *, key: str) -> None:
        RecordingStorage.deleted_keys.append(key)


@pytest.fixture(autouse=True)
def _reset_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    RecordingStorage.deleted_keys = []
    monkeypatch.setattr(documents, "StorageService", RecordingStorage)


def _document(file_id: str, *, age_days: int, source_job_id: str | None = None) -> object:
    return models.Document(
        id=file_id,
        original_filename=f"{file_id}.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        sha256="b" * 64,
        storage_key=f"inputs/{file_id}/doc.pdf",
        page_count=1,
        status=models.DocumentStatus.VALIDATED.value,
        source_job_id=source_job_id,
        created_at=NOW - timedelta(days=age_days),
    )


def _settings(days: int | None) -> object:
    return config.Settings(document_retention_days=days)


def test_retention_is_off_by_default() -> None:
    assert config.Settings().document_retention_days is None


def test_a_disabled_window_purges_nothing() -> None:
    session = FakeSession(expired=[_document("file_1", age_days=900)])

    sweep = documents.purge_expired_documents(session, settings=_settings(None), now=NOW)

    assert sweep.cutoff is None
    assert sweep.purged == 0
    assert RecordingStorage.deleted_keys == []
    assert session.commits == 0


def test_the_cutoff_is_the_window_before_now() -> None:
    cutoff = documents.retention_cutoff(settings=_settings(30), now=NOW)

    assert cutoff == NOW - timedelta(days=30)


def test_expired_documents_are_purged() -> None:
    session = FakeSession(
        expired=[_document("file_1", age_days=40), _document("file_2", age_days=100)]
    )

    sweep = documents.purge_expired_documents(session, settings=_settings(30), now=NOW)

    assert sweep.examined == 2
    assert sweep.purged == 2
    assert sweep.skipped_in_use == 0
    assert sweep.purged_file_ids == ["file_1", "file_2"]
    assert RecordingStorage.deleted_keys == [
        "inputs/file_1/doc.pdf",
        "inputs/file_2/doc.pdf",
    ]


def test_a_purged_document_keeps_its_record() -> None:
    document = _document("file_1", age_days=40)
    session = FakeSession(expired=[document])

    documents.purge_expired_documents(session, settings=_settings(30), now=NOW)

    assert document.status == models.DocumentStatus.DELETED.value
    assert document.deleted_at is not None
    assert document.sha256 == "b" * 64
    assert document.original_filename == "file_1.pdf"


def test_an_input_an_unfinished_job_needs_survives_the_sweep() -> None:
    needed = _document("file_1", age_days=400)
    session = FakeSession(expired=[needed], in_use_ids=["file_1"])

    sweep = documents.purge_expired_documents(session, settings=_settings(30), now=NOW)

    assert sweep.examined == 1
    assert sweep.purged == 0
    assert sweep.skipped_in_use == 1
    assert RecordingStorage.deleted_keys == []
    assert needed.status == models.DocumentStatus.VALIDATED.value


def test_a_sweep_purges_what_it_can_and_skips_what_it_must() -> None:
    session = FakeSession(
        expired=[
            _document("file_busy", age_days=40),
            _document("file_free", age_days=40),
        ],
        in_use_ids=["file_busy"],
    )

    sweep = documents.purge_expired_documents(session, settings=_settings(30), now=NOW)

    assert sweep.purged_file_ids == ["file_free"]
    assert sweep.skipped_in_use == 1
    assert RecordingStorage.deleted_keys == ["inputs/file_free/doc.pdf"]


def test_a_retention_purge_is_labelled_in_the_job_trail() -> None:
    session = FakeSession(expired=[_document("file_1", age_days=40, source_job_id="job_7")])

    documents.purge_expired_documents(session, settings=_settings(30), now=NOW)

    events = [item for item in session.added if isinstance(item, models.AuditEvent)]
    assert [event.event_type for event in events] == ["output.deleted"]
    assert events[0].payload["reason"] == "retention"
    assert events[0].job_id == "job_7"


def test_an_explicit_delete_is_labelled_differently() -> None:
    document = _document("file_1", age_days=1, source_job_id="job_7")

    class SingleDocSession(FakeSession):
        def get(self, model: object, item_id: str) -> object:
            return document

    session = SingleDocSession(expired=[], in_use_ids=[])
    documents.delete_document(session, file_id="file_1", settings=_settings(None))

    events = [item for item in session.added if isinstance(item, models.AuditEvent)]
    assert events[0].payload["reason"] == "requested"


def test_nothing_expired_means_no_second_query() -> None:
    session = FakeSession(expired=[])

    sweep = documents.purge_expired_documents(session, settings=_settings(30), now=NOW)

    assert sweep.examined == 0
    assert sweep.purged == 0
    assert sweep.cutoff == NOW - timedelta(days=30)
    assert session.commits == 0


def test_the_sweep_summary_is_json_ready() -> None:
    session = FakeSession(expired=[_document("file_1", age_days=40)])

    summary = documents.purge_expired_documents(
        session, settings=_settings(30), now=NOW
    ).as_dict()

    assert summary == {
        "cutoff": (NOW - timedelta(days=30)).isoformat(),
        "examined": 1,
        "purged": 1,
        "skipped_in_use": 0,
        "purged_file_ids": ["file_1"],
    }
