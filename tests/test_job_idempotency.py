import importlib

import pytest

for _module_name in ["pydantic_settings", "redis", "rq", "sqlalchemy"]:
    pytest.importorskip(_module_name)

from sqlalchemy.exc import IntegrityError  # noqa: E402

config = importlib.import_module("app.config")
models = importlib.import_module("app.models")
schemas = importlib.import_module("app.schemas")
jobs = importlib.import_module("app.services.jobs")
KnownOperationError = importlib.import_module("app.operations.base").KnownOperationError


class FakeSession:
    """Session double that scripts scalar() lookups and flush() failures."""

    def __init__(
        self,
        *,
        scalar_results: list[object] | None = None,
        document: object | None = None,
        flush_results: list[object] | None = None,
    ) -> None:
        self.scalar_results = list(scalar_results or [])
        self.document = document
        self.flush_results = list(flush_results or [])
        self.locked: list[str] = []
        self.added: list[object] = []
        self.commits = 0
        self.rollbacks = 0

    def scalar(self, statement: object) -> object | None:
        statement_text = str(statement)
        if "count(jobs.id)" in statement_text:
            return 0
        if "FROM documents" in statement_text:
            parameters = statement.compile().params
            file_id = next(
                value
                for name, value in parameters.items()
                if name.startswith("id_")
            )
            self.locked.append(file_id)
            return self.document
        if not self.scalar_results:
            return None
        return self.scalar_results.pop(0)

    def get(self, model: object, item_id: str, **kwargs: object) -> object | None:
        if model is models.Workspace:
            return models.Workspace(id=item_id, name="Acme")
        if kwargs.get("with_for_update"):
            self.locked.append(item_id)
        return self.document

    def add(self, item: object) -> None:
        self.added.append(item)

    def flush(self) -> None:
        if not self.flush_results:
            return None
        result = self.flush_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _settings() -> object:
    return config.Settings(redis_url="redis://example.invalid/0")


def _document(file_id: str = "file_123") -> object:
    return models.Document(
        id=file_id,
        workspace_id="ws_acme",
        original_filename="source.pdf",
        mime_type="application/pdf",
        size_bytes=128,
        sha256="0" * 64,
        storage_key=f"inputs/{file_id}/source.pdf",
        page_count=2,
        status=models.DocumentStatus.VALIDATED.value,
    )


def _request() -> object:
    return schemas.JobCreate(
        operation="compress",
        inputs=[schemas.JobInputRef(file_id="file_123")],
        parameters={"preset": "ebook"},
    )


def _integrity_error() -> IntegrityError:
    return IntegrityError("INSERT INTO jobs", {}, Exception("uq_jobs_idempotency_key"))


def _existing_job(
    *,
    fingerprint: str | None,
    status: str,
    queue_job_id: str | None = None,
    error_code: str | None = None,
) -> object:
    return models.Job(
        id="job_existing",
        workspace_id="ws_acme",
        operation="compress",
        status=status,
        parameters={"preset": "ebook"},
        idempotency_key="demo-key",
        idempotency_fingerprint=fingerprint,
        queue_job_id=queue_job_id,
        error_code=error_code,
        error_message="queue down" if error_code else None,
    )


def test_concurrent_duplicate_key_returns_the_winning_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    winner = _existing_job(
        fingerprint=jobs.job_request_fingerprint(request),
        status=models.JobStatus.QUEUED.value,
        queue_job_id="rq_winner",
    )
    session = FakeSession(
        scalar_results=[None, None, winner],
        document=_document(),
        flush_results=[_integrity_error()],
    )
    monkeypatch.setattr(
        jobs,
        "enqueue_job",
        lambda *args, **kwargs: pytest.fail("the losing request must not enqueue a second job"),
    )

    result = jobs.create_job(
        session,
        workspace_id="ws_acme",
        request=request,
        idempotency_key="demo-key",
        settings=_settings(),
    )

    assert result is winner
    assert session.rollbacks == 1


def test_concurrent_duplicate_key_with_different_request_conflicts() -> None:
    request = _request()
    winner = _existing_job(
        fingerprint="a-different-fingerprint",
        status=models.JobStatus.QUEUED.value,
        queue_job_id="rq_winner",
    )
    session = FakeSession(
        scalar_results=[None, None, winner],
        document=_document(),
        flush_results=[_integrity_error()],
    )

    with pytest.raises(KnownOperationError) as exc:
        jobs.create_job(
            session,
            workspace_id="ws_acme",
            request=request,
            idempotency_key="demo-key",
            settings=_settings(),
        )

    assert exc.value.code == "IDEMPOTENCY_KEY_CONFLICT"
    assert session.rollbacks == 1


def test_integrity_error_without_idempotency_key_propagates() -> None:
    session = FakeSession(
        document=_document(),
        flush_results=[_integrity_error()],
    )

    with pytest.raises(IntegrityError):
        jobs.create_job(
            session,
            workspace_id="ws_acme",
            request=_request(),
            idempotency_key=None,
            settings=_settings(),
        )


def test_replay_reenqueues_a_job_that_never_reached_the_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    stranded = _existing_job(
        fingerprint=jobs.job_request_fingerprint(request),
        status=models.JobStatus.FAILED.value,
        queue_job_id=None,
        error_code="QUEUE_UNAVAILABLE",
    )
    session = FakeSession(scalar_results=[stranded])
    monkeypatch.setattr(jobs, "enqueue_job", lambda job_id, *, settings: "rq_retry")

    result = jobs.create_job(
        session,
        workspace_id="ws_acme",
        request=request,
        idempotency_key="demo-key",
        settings=_settings(),
    )

    assert result is stranded
    assert result.status == models.JobStatus.QUEUED.value
    assert result.queue_job_id == "rq_retry"
    assert result.error_code is None
    assert result.error_message is None

    retried = [
        event
        for event in session.added
        if isinstance(event, models.AuditEvent) and event.event_type == "job.enqueue_retried"
    ]
    assert len(retried) == 1


def test_replay_still_reports_a_queue_that_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request()
    stranded = _existing_job(
        fingerprint=jobs.job_request_fingerprint(request),
        status=models.JobStatus.FAILED.value,
        queue_job_id=None,
        error_code="QUEUE_UNAVAILABLE",
    )
    session = FakeSession(scalar_results=[stranded])

    def fail_to_enqueue(job_id: str, *, settings: object) -> str:
        raise RuntimeError("redis is unreachable")

    monkeypatch.setattr(jobs, "enqueue_job", fail_to_enqueue)

    with pytest.raises(KnownOperationError) as exc:
        jobs.create_job(
            session,
            workspace_id="ws_acme",
            request=request,
            idempotency_key="demo-key",
            settings=_settings(),
        )

    assert exc.value.code == "QUEUE_UNAVAILABLE"
    assert exc.value.retryable is True
    assert stranded.status == models.JobStatus.FAILED.value


def test_replay_of_a_queued_job_does_not_enqueue_again(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request()
    queued = _existing_job(
        fingerprint=jobs.job_request_fingerprint(request),
        status=models.JobStatus.QUEUED.value,
        queue_job_id="rq_original",
    )
    session = FakeSession(scalar_results=[queued])
    monkeypatch.setattr(
        jobs,
        "enqueue_job",
        lambda *args, **kwargs: pytest.fail("a queued job must not be enqueued twice"),
    )

    result = jobs.create_job(
        session,
        workspace_id="ws_acme",
        request=request,
        idempotency_key="demo-key",
        settings=_settings(),
    )

    assert result is queued
    assert result.queue_job_id == "rq_original"
    assert session.commits == 0


def test_replay_of_a_worker_failure_is_not_reenqueued(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request()
    failed = _existing_job(
        fingerprint=jobs.job_request_fingerprint(request),
        status=models.JobStatus.FAILED.value,
        queue_job_id="rq_original",
        error_code="QPDF_CHECK_FAILED",
    )
    session = FakeSession(scalar_results=[failed])
    monkeypatch.setattr(
        jobs,
        "enqueue_job",
        lambda *args, **kwargs: pytest.fail("a job that already ran must not be enqueued again"),
    )

    result = jobs.create_job(
        session,
        workspace_id="ws_acme",
        request=request,
        idempotency_key="demo-key",
        settings=_settings(),
    )

    assert result is failed
    assert result.status == models.JobStatus.FAILED.value


def test_job_inputs_are_locked_in_a_stable_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two creations sharing inputs must take locks in the same order or they can deadlock."""
    request = schemas.JobCreate(
        operation="merge",
        inputs=[
            schemas.JobInputRef(file_id="file_zebra"),
            schemas.JobInputRef(file_id="file_alpha"),
        ],
        parameters={},
    )
    session = FakeSession(document=_document(), scalar_results=[None])
    monkeypatch.setattr(jobs, "enqueue_job", lambda job_id, *, settings: "rq_1")

    jobs.create_job(
        session,
        workspace_id="ws_acme",
        request=request,
        idempotency_key=None,
        settings=_settings(),
    )

    assert session.locked == ["file_alpha", "file_zebra"]
