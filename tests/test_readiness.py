import importlib
from typing import Any

import pytest

for _module_name in [
    "fastapi",
    "mcp",
    "psycopg",
    "pydantic_settings",
    "redis",
    "rq",
    "sqlalchemy",
]:
    pytest.importorskip(_module_name)

main = importlib.import_module("app.main")
config = importlib.import_module("app.config")
fastapi = importlib.import_module("fastapi")


class FakeSession:
    def execute(self, statement: Any) -> None:
        self.statement = statement


class HealthyRedisClient:
    def ping(self) -> None:
        return None

    def close(self) -> None:
        return None


class FailingRedisClient:
    def ping(self) -> None:
        raise RuntimeError("redis unavailable")

    def close(self) -> None:
        return None


class HealthyRedis:
    @staticmethod
    def from_url(redis_url: str) -> HealthyRedisClient:
        return HealthyRedisClient()


class FailingRedis:
    @staticmethod
    def from_url(redis_url: str) -> FailingRedisClient:
        return FailingRedisClient()


class HealthyStorage:
    def __init__(self, settings: object) -> None:
        self.settings = settings

    def ensure_bucket(self) -> None:
        return None


def test_ready_returns_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "Redis", HealthyRedis)
    monkeypatch.setattr(main, "StorageService", HealthyStorage)

    response = main.ready(session=FakeSession(), settings=config.Settings())

    assert response["status"] == "ready"
    assert response["checks"] == {
        "database": {"status": "ok"},
        "redis": {"status": "ok"},
        "storage": {"status": "ok"},
    }


def test_ready_returns_503_when_a_dependency_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "Redis", FailingRedis)
    monkeypatch.setattr(main, "StorageService", HealthyStorage)

    with pytest.raises(fastapi.HTTPException) as exc:
        main.ready(session=FakeSession(), settings=config.Settings())

    assert exc.value.status_code == 503
    assert exc.value.detail["error"]["code"] == "SERVICE_NOT_READY"
    assert exc.value.detail["error"]["details"]["checks"]["redis"]["status"] == "error"
