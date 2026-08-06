import importlib

import pytest


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
