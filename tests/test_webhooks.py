import hashlib
import hmac
import importlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

for _module_name in ["httpx", "pydantic_settings", "sqlalchemy"]:
    pytest.importorskip(_module_name)

config = importlib.import_module("app.config")
models = importlib.import_module("app.models")
schemas = importlib.import_module("app.schemas")
webhooks = importlib.import_module("app.services.webhooks")


class FakeSession:
    def __init__(self, delivery: object) -> None:
        self.delivery = delivery
        self.commits = 0

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def get(self, model: object, item_id: str) -> object:
        return self.delivery

    def add(self, item: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1


def _delivery() -> Any:
    endpoint = models.WebhookEndpoint(
        id="wh_123",
        url="https://example.com/hook",
        secret="secret_123",
        events=["job.succeeded"],
        active=True,
    )
    return models.WebhookDelivery(
        id="whd_123",
        endpoint_id=endpoint.id,
        job_id="job_123",
        event_type="job.succeeded",
        payload={"event": "job.succeeded", "job": {"job_id": "job_123"}},
        status=models.WebhookDeliveryStatus.PENDING.value,
        attempts=0,
        endpoint=endpoint,
    )


def test_webhook_create_rejects_relative_url() -> None:
    with pytest.raises(ValueError, match="absolute http or https"):
        schemas.WebhookCreate(url="/local/hook")


def test_sign_webhook_payload_uses_timestamp_and_body() -> None:
    body = b'{"event":"job.succeeded"}'
    signature = webhooks.sign_webhook_payload(secret="topsecret", timestamp=1234, body=body)
    expected = hmac.new(b"topsecret", b'1234.{"event":"job.succeeded"}', hashlib.sha256)

    assert signature == f"sha256={expected.hexdigest()}"


def test_build_job_webhook_payload_includes_outputs() -> None:
    document = models.Document(
        id="file_123",
        original_filename="merged.pdf",
        mime_type="application/pdf",
        size_bytes=20,
        sha256="a" * 64,
        storage_key="outputs/job_123/merged.pdf",
        page_count=2,
        status=models.DocumentStatus.VALIDATED.value,
    )
    job = models.Job(
        id="job_123",
        operation="merge",
        status=models.JobStatus.SUCCEEDED.value,
        parameters={"ocr_if_needed": False},
        created_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    job.outputs = [
        models.JobOutput(
            id="jout_123",
            job_id=job.id,
            document_id=document.id,
            position=0,
            document=document,
        )
    ]

    payload = webhooks.build_job_webhook_payload(event_type="job.succeeded", job=job)

    assert payload["event"] == "job.succeeded"
    assert payload["job"]["job_id"] == "job_123"
    assert payload["job"]["outputs"][0]["file_id"] == "file_123"
    assert payload["job"]["error"] is None
    assert payload["job"]["created_at"] == job.created_at.isoformat()
    assert json.loads(json.dumps(payload)) == payload


def test_record_enqueue_failure_marks_delivery_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = _delivery()
    monkeypatch.setattr(webhooks, "SessionLocal", lambda: FakeSession(delivery))

    webhooks._record_enqueue_failure("whd_123", RuntimeError("Redis unavailable"))

    assert delivery.status == models.WebhookDeliveryStatus.FAILED.value
    assert delivery.attempts == 0
    assert delivery.last_error == "Webhook could not be enqueued: Redis unavailable"


def test_deliver_webhook_delivery_sends_signed_request(monkeypatch: pytest.MonkeyPatch) -> None:
    delivery = _delivery()
    requests: list[dict[str, Any]] = []

    class Client:
        def __init__(self, *, timeout: int) -> None:
            self.timeout = timeout

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

        def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> object:
            requests.append({"url": url, "content": content, "headers": headers})
            return SimpleNamespace(status_code=204)

    monkeypatch.setattr(webhooks, "SessionLocal", lambda: FakeSession(delivery))
    monkeypatch.setattr(webhooks.time, "time", lambda: 1234)

    webhooks.deliver_webhook_delivery(
        "whd_123",
        settings=config.Settings(webhook_delivery_timeout_seconds=2, webhook_max_attempts=3),
        sleep_fn=lambda _: None,
        client_factory=Client,
    )

    assert delivery.status == models.WebhookDeliveryStatus.SUCCEEDED.value
    assert delivery.attempts == 1
    assert delivery.delivered_at is not None
    assert requests[0]["url"] == "https://example.com/hook"
    assert requests[0]["headers"]["X-AgentP-Delivery"] == "whd_123"
    assert requests[0]["headers"]["X-AgentP-Event"] == "job.succeeded"
    assert requests[0]["headers"]["X-AgentP-Timestamp"] == "1234"
    assert requests[0]["headers"]["X-AgentP-Signature"] == webhooks.sign_webhook_payload(
        secret="secret_123",
        timestamp=1234,
        body=requests[0]["content"],
    )


def test_deliver_webhook_delivery_retries_then_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    delivery = _delivery()
    sleeps: list[float] = []

    class Client:
        def __init__(self, *, timeout: int) -> None:
            return None

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

        def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> object:
            return SimpleNamespace(status_code=500)

    monkeypatch.setattr(webhooks, "SessionLocal", lambda: FakeSession(delivery))

    webhooks.deliver_webhook_delivery(
        "whd_123",
        settings=config.Settings(webhook_delivery_timeout_seconds=2, webhook_max_attempts=2),
        sleep_fn=sleeps.append,
        client_factory=Client,
    )

    assert delivery.status == models.WebhookDeliveryStatus.FAILED.value
    assert delivery.attempts == 2
    assert delivery.last_status_code == 500
    assert delivery.last_error == "HTTP 500"
    assert sleeps == [1.0]
