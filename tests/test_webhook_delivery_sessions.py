import importlib
from types import SimpleNamespace
from typing import Any

import pytest

for _module_name in ["httpx", "pydantic_settings", "sqlalchemy"]:
    pytest.importorskip(_module_name)

config = importlib.import_module("app.config")
models = importlib.import_module("app.models")
webhooks = importlib.import_module("app.services.webhooks")


class SessionTracker:
    """Counts how many sessions are open at any moment."""

    def __init__(self, delivery: object) -> None:
        self.delivery = delivery
        self.open_now = 0
        self.opened_total = 0

    def __call__(self) -> "TrackedSession":
        return TrackedSession(self)


class TrackedSession:
    def __init__(self, tracker: SessionTracker) -> None:
        self.tracker = tracker

    def __enter__(self) -> "TrackedSession":
        self.tracker.open_now += 1
        self.tracker.opened_total += 1
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.tracker.open_now -= 1

    def get(self, model: object, item_id: str) -> object:
        return self.tracker.delivery

    def add(self, item: object) -> None:
        return None

    def commit(self) -> None:
        return None


def _delivery() -> Any:
    endpoint = models.WebhookEndpoint(
        id="wh_123",
        url="https://collector.example.com/hook",
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


def _allow_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhooks, "ensure_public_webhook_target", lambda url, *, settings: None)


def test_no_session_is_open_while_the_receiver_is_being_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = SessionTracker(_delivery())
    open_during_post: list[int] = []

    class Client:
        def __init__(self, *, timeout: int) -> None:
            return None

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

        def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> object:
            open_during_post.append(tracker.open_now)
            return SimpleNamespace(status_code=204)

    _allow_target(monkeypatch)
    monkeypatch.setattr(webhooks, "SessionLocal", tracker)

    webhooks.deliver_webhook_delivery(
        "whd_123",
        settings=config.Settings(webhook_delivery_timeout_seconds=2, webhook_max_attempts=3),
        sleep_fn=lambda _: None,
        client_factory=Client,
    )

    assert open_during_post == [0]
    assert tracker.open_now == 0
    assert tracker.delivery.status == models.WebhookDeliveryStatus.SUCCEEDED.value


def test_no_session_is_open_while_backing_off(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = SessionTracker(_delivery())
    open_during_sleep: list[int] = []
    open_during_post: list[int] = []

    class Client:
        def __init__(self, *, timeout: int) -> None:
            return None

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

        def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> object:
            open_during_post.append(tracker.open_now)
            return SimpleNamespace(status_code=503)

    _allow_target(monkeypatch)
    monkeypatch.setattr(webhooks, "SessionLocal", tracker)

    webhooks.deliver_webhook_delivery(
        "whd_123",
        settings=config.Settings(webhook_delivery_timeout_seconds=2, webhook_max_attempts=3),
        sleep_fn=lambda _: open_during_sleep.append(tracker.open_now),
        client_factory=Client,
    )

    assert open_during_post == [0, 0, 0]
    assert open_during_sleep == [0, 0]
    assert tracker.open_now == 0
    assert tracker.delivery.status == models.WebhookDeliveryStatus.FAILED.value
    assert tracker.delivery.attempts == 3


def test_a_receiver_that_raises_still_leaves_no_session_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = SessionTracker(_delivery())

    class Client:
        def __init__(self, *, timeout: int) -> None:
            return None

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

        def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> object:
            raise TimeoutError("receiver timed out")

    _allow_target(monkeypatch)
    monkeypatch.setattr(webhooks, "SessionLocal", tracker)

    webhooks.deliver_webhook_delivery(
        "whd_123",
        settings=config.Settings(webhook_delivery_timeout_seconds=2, webhook_max_attempts=1),
        sleep_fn=lambda _: None,
        client_factory=Client,
    )

    assert tracker.open_now == 0
    assert tracker.delivery.status == models.WebhookDeliveryStatus.FAILED.value
    assert tracker.delivery.last_error == "receiver timed out"


def test_the_payload_is_read_once_rather_than_per_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = SessionTracker(_delivery())

    class Client:
        def __init__(self, *, timeout: int) -> None:
            return None

        def __enter__(self) -> "Client":
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

        def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> object:
            return SimpleNamespace(status_code=500)

    _allow_target(monkeypatch)
    monkeypatch.setattr(webhooks, "SessionLocal", tracker)

    webhooks.deliver_webhook_delivery(
        "whd_123",
        settings=config.Settings(webhook_delivery_timeout_seconds=2, webhook_max_attempts=2),
        sleep_fn=lambda _: None,
        client_factory=Client,
    )

    # One load plus one write per attempt state change: nothing is held across the loop.
    assert tracker.opened_total == 5
    assert tracker.open_now == 0
