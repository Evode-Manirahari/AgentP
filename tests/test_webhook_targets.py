import importlib
import ipaddress

import pytest

for _module_name in ["pydantic_settings", "redis", "rq", "sqlalchemy"]:
    pytest.importorskip(_module_name)

config = importlib.import_module("app.config")
schemas = importlib.import_module("app.schemas")
webhooks = importlib.import_module("app.services.webhooks")
KnownOperationError = importlib.import_module("app.operations.base").KnownOperationError


def _settings(*, allow_private: bool = False) -> object:
    return config.Settings(webhook_allow_private_urls=allow_private)


def _resolves_to(*addresses: str):
    """Replace DNS so these tests never touch the network."""

    def resolve(host: str, port: int) -> list:
        return [ipaddress.ip_address(address) for address in addresses]

    return resolve


@pytest.mark.parametrize(
    "address",
    [
        "169.254.169.254",  # cloud instance metadata
        "127.0.0.1",
        "10.0.0.5",
        "172.16.0.9",
        "192.168.1.20",
        "0.0.0.0",
        "::1",
        "fd00::1",
        "fe80::1",
    ],
)
def test_internal_targets_are_rejected(
    address: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(webhooks, "_resolve_addresses", _resolves_to(address))

    with pytest.raises(KnownOperationError) as exc:
        webhooks.ensure_public_webhook_target(
            "https://collector.example.com/hook",
            settings=_settings(),
        )

    assert exc.value.code == "WEBHOOK_TARGET_NOT_ALLOWED"
    assert exc.value.details["blocked_addresses"] == [address]


@pytest.mark.parametrize("address", ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])
def test_public_targets_are_allowed(address: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhooks, "_resolve_addresses", _resolves_to(address))

    webhooks.ensure_public_webhook_target(
        "https://collector.example.com/hook",
        settings=_settings(),
    )


def test_a_host_that_resolves_both_ways_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # DNS rebinding: one public answer is not enough to make the target safe.
    monkeypatch.setattr(
        webhooks,
        "_resolve_addresses",
        _resolves_to("93.184.216.34", "169.254.169.254"),
    )

    with pytest.raises(KnownOperationError) as exc:
        webhooks.ensure_public_webhook_target(
            "https://rebind.example.com/hook",
            settings=_settings(),
        )

    assert exc.value.code == "WEBHOOK_TARGET_NOT_ALLOWED"


def test_an_unresolvable_host_is_left_to_the_delivery_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(webhooks, "_resolve_addresses", lambda host, port: [])

    webhooks.ensure_public_webhook_target(
        "https://not-registered.example.com/hook",
        settings=_settings(),
    )


def test_the_guard_can_be_disabled_for_local_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(webhooks, "_resolve_addresses", _resolves_to("127.0.0.1"))

    webhooks.ensure_public_webhook_target(
        "http://localhost:9000/hook",
        settings=_settings(allow_private=True),
    )


def test_registration_rejects_an_internal_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webhooks, "_resolve_addresses", _resolves_to("169.254.169.254"))

    with pytest.raises(KnownOperationError) as exc:
        webhooks.create_webhook_endpoint(
            session=None,
            request=schemas.WebhookCreate(url="http://169.254.169.254/latest/meta-data/"),
            settings=_settings(),
        )

    assert exc.value.code == "WEBHOOK_TARGET_NOT_ALLOWED"


class FakeDeliverySession:
    def __init__(self, delivery: object) -> None:
        self.delivery = delivery
        self.commits = 0

    def __enter__(self) -> "FakeDeliverySession":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, model: object, item_id: str) -> object:
        return self.delivery

    def add(self, item: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1


def test_delivery_is_blocked_when_dns_moves_inward(monkeypatch: pytest.MonkeyPatch) -> None:
    models = importlib.import_module("app.models")
    endpoint = models.WebhookEndpoint(
        id="wh_123",
        url="https://rebind.example.com/hook",
        secret="secret_123",
        events=["job.succeeded"],
        active=True,
    )
    delivery = models.WebhookDelivery(
        id="whd_123",
        endpoint_id=endpoint.id,
        job_id="job_123",
        event_type="job.succeeded",
        payload={"event": "job.succeeded"},
        status=models.WebhookDeliveryStatus.PENDING.value,
        attempts=0,
        endpoint=endpoint,
    )
    session = FakeDeliverySession(delivery)
    monkeypatch.setattr(webhooks, "SessionLocal", lambda: session)
    monkeypatch.setattr(webhooks, "_resolve_addresses", _resolves_to("169.254.169.254"))

    def refuse_client(**kwargs: object):
        pytest.fail("a blocked target must never be contacted")

    webhooks.deliver_webhook_delivery(
        "whd_123",
        settings=_settings(),
        client_factory=refuse_client,
    )

    assert delivery.status == models.WebhookDeliveryStatus.FAILED.value
    assert "public address" in delivery.last_error
    assert session.commits == 1
