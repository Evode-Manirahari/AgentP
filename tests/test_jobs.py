import importlib

import pytest


class FakeSession:
    def __init__(self, job: object | None) -> None:
        self.job = job
        self.added: list[object] = []
        self.committed = False

    def get(self, model: object, item_id: str) -> object | None:
        return self.job

    def add(self, item: object) -> None:
        self.added.append(item)

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        self.committed = True


def test_job_request_fingerprint_is_stable_for_parameter_order() -> None:
    for module_name in ["pydantic_settings", "redis", "rq", "sqlalchemy"]:
        pytest.importorskip(module_name)

    schemas = importlib.import_module("app.schemas")
    jobs = importlib.import_module("app.services.jobs")

    first = schemas.JobCreate(
        operation="compress",
        inputs=[schemas.JobInputRef(file_id="file_123")],
        parameters={"preset": "ebook", "options": {"b": 2, "a": 1}},
    )
    second = schemas.JobCreate(
        operation="compress",
        inputs=[schemas.JobInputRef(file_id="file_123")],
        parameters={"options": {"a": 1, "b": 2}, "preset": "ebook"},
    )

    assert jobs.job_request_fingerprint(first) == jobs.job_request_fingerprint(second)


def test_job_request_fingerprint_changes_with_request_shape() -> None:
    for module_name in ["pydantic_settings", "redis", "rq", "sqlalchemy"]:
        pytest.importorskip(module_name)

    schemas = importlib.import_module("app.schemas")
    jobs = importlib.import_module("app.services.jobs")

    base = schemas.JobCreate(
        operation="split",
        inputs=[schemas.JobInputRef(file_id="file_123")],
        parameters={"page_ranges": ["1-2"]},
    )
    different_parameters = schemas.JobCreate(
        operation="split",
        inputs=[schemas.JobInputRef(file_id="file_123")],
        parameters={"page_ranges": ["2-3"]},
    )
    different_inputs = schemas.JobCreate(
        operation="split",
        inputs=[schemas.JobInputRef(file_id="file_456")],
        parameters={"page_ranges": ["1-2"]},
    )

    assert jobs.job_request_fingerprint(base) != jobs.job_request_fingerprint(different_parameters)
    assert jobs.job_request_fingerprint(base) != jobs.job_request_fingerprint(different_inputs)


def test_validate_job_status_filter_accepts_known_statuses() -> None:
    for module_name in ["pydantic_settings", "redis", "rq", "sqlalchemy"]:
        pytest.importorskip(module_name)

    jobs = importlib.import_module("app.services.jobs")

    assert jobs.validate_job_status_filter(None) is None
    assert jobs.validate_job_status_filter("queued") == "queued"
    assert jobs.validate_job_status_filter("succeeded") == "succeeded"


def test_validate_job_status_filter_rejects_unknown_status() -> None:
    for module_name in ["pydantic_settings", "redis", "rq", "sqlalchemy"]:
        pytest.importorskip(module_name)

    jobs = importlib.import_module("app.services.jobs")
    error_module = importlib.import_module("app.operations.base")

    with pytest.raises(error_module.KnownOperationError) as exc:
        jobs.validate_job_status_filter("finished")

    assert exc.value.code == "INVALID_JOB_STATUS"
    assert "succeeded" in exc.value.details["allowed"]


def test_cancel_job_marks_queued_job_canceled(monkeypatch: pytest.MonkeyPatch) -> None:
    for module_name in ["pydantic_settings", "redis", "rq", "sqlalchemy"]:
        pytest.importorskip(module_name)

    config = importlib.import_module("app.config")
    jobs = importlib.import_module("app.services.jobs")
    models = importlib.import_module("app.models")

    job = models.Job(
        id="job_123",
        operation="compress",
        status=models.JobStatus.QUEUED.value,
        parameters={},
        queue_job_id="rq_123",
    )
    session = FakeSession(job)
    canceled_queue_ids: list[str] = []

    def record_cancel(queued_job: object, *, settings: object) -> None:
        canceled_queue_ids.append(queued_job.queue_job_id)

    monkeypatch.setattr(jobs, "cancel_queued_rq_job", record_cancel)

    result = jobs.cancel_job(
        session,
        job_id="job_123",
        settings=config.Settings(redis_url="redis://example.invalid/0"),
    )

    assert result.status == models.JobStatus.CANCELED.value
    assert result.finished_at is not None
    assert session.committed is True
    assert canceled_queue_ids == ["rq_123"]


def test_cancel_job_rejects_running_job() -> None:
    for module_name in ["pydantic_settings", "redis", "rq", "sqlalchemy"]:
        pytest.importorskip(module_name)

    config = importlib.import_module("app.config")
    jobs = importlib.import_module("app.services.jobs")
    models = importlib.import_module("app.models")
    error_module = importlib.import_module("app.operations.base")

    job = models.Job(
        id="job_123",
        operation="compress",
        status=models.JobStatus.RUNNING.value,
        parameters={},
    )

    with pytest.raises(error_module.KnownOperationError) as exc:
        jobs.cancel_job(
            FakeSession(job),
            job_id="job_123",
            settings=config.Settings(redis_url="redis://example.invalid/0"),
        )

    assert exc.value.code == "JOB_NOT_CANCELABLE"
    assert exc.value.details["status"] == "running"
