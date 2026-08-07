import importlib
from datetime import UTC, datetime

import pytest

for _module_name in ["pydantic_settings", "redis", "rq", "sqlalchemy"]:
    pytest.importorskip(_module_name)

models = importlib.import_module("app.models")
schemas = importlib.import_module("app.schemas")
jobs = importlib.import_module("app.services.jobs")
webhooks = importlib.import_module("app.services.webhooks")

WARNING = {
    "code": "LOW_TEXT_AFTER_OCR",
    "message": "OCR completed but the document still contains very little text.",
    "position": 1,
    "filename": "scan.pdf",
}


def _job(*, status: str, validation: dict | None) -> object:
    return models.Job(
        id="job_123",
        operation="prepare_packet",
        status=status,
        parameters={"order": "filename"},
        validation=validation,
        created_at=datetime.now(UTC),
    )


def test_completed_with_warnings_is_a_terminal_status() -> None:
    assert models.JobStatus.COMPLETED_WITH_WARNINGS.value == "completed_with_warnings"
    assert models.JobStatus.COMPLETED_WITH_WARNINGS.value in jobs.TERMINAL_JOB_STATUSES

    runner = importlib.import_module("worker.runner")

    assert models.JobStatus.COMPLETED_WITH_WARNINGS.value in runner.TERMINAL_JOB_STATUSES


def test_a_clean_run_still_succeeds() -> None:
    runner = importlib.import_module("worker.runner")

    assert runner._terminal_success_outcome([]) == ("succeeded", "job.succeeded")


def test_warnings_change_the_terminal_status_without_failing() -> None:
    runner = importlib.import_module("worker.runner")

    status, event_type = runner._terminal_success_outcome([WARNING])

    assert status == "completed_with_warnings"
    assert event_type == "job.completed_with_warnings"
    assert status != models.JobStatus.FAILED.value


def test_completed_with_warnings_is_an_accepted_status_filter() -> None:
    assert jobs.validate_job_status_filter("completed_with_warnings") == (
        "completed_with_warnings"
    )


def test_job_warnings_reads_the_validation_record() -> None:
    job = _job(
        status=models.JobStatus.COMPLETED_WITH_WARNINGS.value,
        validation={"operation": "prepare_packet", "warnings": [WARNING]},
    )

    assert jobs.job_warnings(job) == [WARNING]


@pytest.mark.parametrize(
    "validation",
    [None, {}, {"warnings": []}, {"warnings": None}],
)
def test_job_warnings_is_empty_when_nothing_was_recorded(validation: dict | None) -> None:
    job = _job(status=models.JobStatus.SUCCEEDED.value, validation=validation)

    assert jobs.job_warnings(job) == []


def test_job_summary_reports_a_warning_count() -> None:
    job = _job(
        status=models.JobStatus.COMPLETED_WITH_WARNINGS.value,
        validation={"warnings": [WARNING]},
    )

    summary = jobs._job_summary(job)

    assert summary.status == "completed_with_warnings"
    assert summary.warning_count == 1


def test_webhook_payload_carries_warnings() -> None:
    job = _job(
        status=models.JobStatus.COMPLETED_WITH_WARNINGS.value,
        validation={"warnings": [WARNING]},
    )

    payload = webhooks.build_job_webhook_payload(
        event_type="job.completed_with_warnings",
        job=job,
    )

    assert payload["event"] == "job.completed_with_warnings"
    assert payload["job"]["status"] == "completed_with_warnings"
    assert payload["job"]["warnings"] == [WARNING]
    assert payload["job"]["error"] is None


def test_webhook_payload_has_an_empty_warning_list_for_clean_jobs() -> None:
    job = _job(status=models.JobStatus.SUCCEEDED.value, validation={"warnings": []})

    payload = webhooks.build_job_webhook_payload(event_type="job.succeeded", job=job)

    assert payload["job"]["warnings"] == []


def test_warning_event_is_registerable_and_on_by_default() -> None:
    assert "job.completed_with_warnings" in schemas.DEFAULT_WEBHOOK_EVENTS

    request = schemas.WebhookCreate(
        url="https://example.com/agentp/events",
        events=["job.completed_with_warnings"],
    )

    assert request.events == ["job.completed_with_warnings"]


def test_webhook_defaults_include_every_terminal_event() -> None:
    request = schemas.WebhookCreate(url="https://example.com/agentp/events")

    assert set(request.events) == {
        "job.succeeded",
        "job.completed_with_warnings",
        "job.failed",
        "job.canceled",
    }
