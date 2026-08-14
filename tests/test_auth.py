import importlib
from datetime import UTC, datetime

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("pydantic_settings")
pytest.importorskip("sqlalchemy")

models = importlib.import_module("app.models")
auth = importlib.import_module("app.services.auth")


class FakeSession:
    def __init__(self, record: object | None = None) -> None:
        self.record = record

    def scalar(self, statement: object) -> object | None:
        return self.record


def _api_key_record(
    token: str,
    *,
    workspace_id: str = "ws_acme",
    revoked_at: datetime | None = None,
    is_platform_admin: bool = False,
) -> object:
    workspace = models.Workspace(
        id=workspace_id,
        name="Acme",
        created_at=datetime.now(UTC),
    )
    return models.ApiKey(
        id="key_123",
        workspace_id=workspace_id,
        name="production",
        token_hash=auth.hash_api_key(token),
        prefix=auth.api_key_prefix(token),
        is_platform_admin=is_platform_admin,
        created_at=datetime.now(UTC),
        revoked_at=revoked_at,
        workspace=workspace,
    )


@pytest.mark.parametrize("api_key", [None, "", "wrong"])
def test_a_missing_or_incorrect_key_is_rejected(api_key: str | None) -> None:
    with pytest.raises(fastapi.HTTPException) as exc:
        auth.require_auth_context(api_key=api_key, session=FakeSession())

    assert exc.value.status_code == fastapi.status.HTTP_401_UNAUTHORIZED
    assert exc.value.detail["error"]["code"] == "UNAUTHORIZED"


def test_an_active_key_returns_its_workspace_context() -> None:
    token, _ = auth.generate_api_key_token()
    context = auth.require_auth_context(
        api_key=token,
        session=FakeSession(_api_key_record(token)),
    )

    assert context.workspace_id == "ws_acme"
    assert context.workspace_name == "Acme"
    assert context.api_key_id == "key_123"
    assert context.api_key_name == "production"


def test_a_revoked_key_is_rejected_even_if_a_session_returns_it() -> None:
    token, _ = auth.generate_api_key_token()
    record = _api_key_record(token, revoked_at=datetime.now(UTC))

    with pytest.raises(fastapi.HTTPException) as exc:
        auth.require_auth_context(api_key=token, session=FakeSession(record))

    assert exc.value.status_code == fastapi.status.HTTP_401_UNAUTHORIZED


def test_a_platform_admin_key_carries_the_admin_capability() -> None:
    token, _ = auth.generate_api_key_token()
    context = auth.require_auth_context(
        api_key=token,
        session=FakeSession(_api_key_record(token, is_platform_admin=True)),
    )

    assert context.is_platform_admin is True
    assert auth.require_platform_admin(context) is context


def test_a_workspace_key_cannot_create_workspaces() -> None:
    context = auth.AuthContext(
        workspace_id="ws_acme",
        workspace_name="Acme",
        api_key_id="key_123",
        api_key_name="production",
    )

    with pytest.raises(fastapi.HTTPException) as exc:
        auth.require_platform_admin(context)

    assert exc.value.status_code == fastapi.status.HTTP_403_FORBIDDEN
    assert exc.value.detail["error"]["code"] == "PLATFORM_ADMIN_REQUIRED"


def test_generated_keys_are_unique_and_expose_only_a_short_prefix() -> None:
    generated = [auth.generate_api_key_token() for _ in range(100)]
    tokens = {token for token, _ in generated}

    assert len(tokens) == 100
    assert all(token.startswith("agentp_") for token in tokens)
    assert all(token.startswith(prefix) for token, prefix in generated)
    assert all(len(prefix) < len(token) / 2 for token, prefix in generated)


def test_the_token_is_not_recoverable_from_what_is_stored() -> None:
    token, prefix = auth.generate_api_key_token()
    token_hash = auth.hash_api_key(token)

    assert token not in token_hash
    assert len(token_hash) == 64
    assert auth.api_key_prefix(token) == prefix


def test_hashing_is_stable_and_distinguishes_tokens() -> None:
    assert auth.hash_api_key("abc") == auth.hash_api_key("abc")
    assert auth.hash_api_key("abc") != auth.hash_api_key("abd")
