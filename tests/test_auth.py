import importlib

import pytest


def test_require_api_key_accepts_matching_key() -> None:
    fastapi = pytest.importorskip("fastapi")
    pytest.importorskip("pydantic_settings")

    config = importlib.import_module("app.config")
    auth = importlib.import_module("app.services.auth")

    auth.require_api_key(api_key="secret", settings=config.Settings(api_key="secret"))
    assert fastapi.status.HTTP_401_UNAUTHORIZED == 401


def test_require_api_key_rejects_missing_or_incorrect_key() -> None:
    fastapi = pytest.importorskip("fastapi")
    pytest.importorskip("pydantic_settings")

    config = importlib.import_module("app.config")
    auth = importlib.import_module("app.services.auth")
    settings = config.Settings(api_key="secret")

    for api_key in [None, "wrong"]:
        with pytest.raises(fastapi.HTTPException) as exc:
            auth.require_api_key(api_key=api_key, settings=settings)

        assert exc.value.status_code == fastapi.status.HTTP_401_UNAUTHORIZED
        assert exc.value.detail["error"]["code"] == "UNAUTHORIZED"
