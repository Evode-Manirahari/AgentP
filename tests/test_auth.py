import asyncio
import importlib
import json
from datetime import UTC, datetime

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("pydantic_settings")
pytest.importorskip("sqlalchemy")

from sqlalchemy.exc import IntegrityError  # noqa: E402

config = importlib.import_module("app.config")
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


class _BootstrapSession:
    def __init__(
        self,
        scalar_results: list[object | None],
        *,
        conflict: bool = False,
    ) -> None:
        self.scalar_results = list(scalar_results)
        self.conflict = conflict
        self.committed = False
        self.statements: list[object] = []

    def __enter__(self) -> "_BootstrapSession":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def scalar(self, statement: object) -> object | None:
        self.statements.append(statement)
        return self.scalar_results.pop(0) if self.scalar_results else None

    def add(self, item: object) -> None:
        return None

    def flush(self) -> None:
        if self.conflict:
            raise IntegrityError("INSERT", {}, Exception("duplicate key"))

    def commit(self) -> None:
        self.committed = True


def test_a_lost_bootstrap_race_is_not_a_startup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second API process must accept the identity written by the winning process."""
    losing_session = _BootstrapSession([None], conflict=True)
    verification_session = _BootstrapSession(["key_winner"])
    sessions = iter([losing_session, verification_session])
    monkeypatch.setattr(auth, "SessionLocal", lambda: next(sessions))

    auth.ensure_bootstrap_identity(config.Settings())

    assert losing_session.committed is False


def test_an_unexplained_bootstrap_integrity_error_is_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    losing_session = _BootstrapSession([None], conflict=True)
    verification_session = _BootstrapSession([None])
    sessions = iter([losing_session, verification_session])
    monkeypatch.setattr(auth, "SessionLocal", lambda: next(sessions))

    with pytest.raises(IntegrityError):
        auth.ensure_bootstrap_identity(config.Settings())


def test_bootstrap_provisions_the_default_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _BootstrapSession([None, None])
    monkeypatch.setattr(auth, "SessionLocal", lambda: session)

    auth.ensure_bootstrap_identity(config.Settings())

    assert session.committed is True
    assert "FROM workspaces" in str(session.statements[0])
    assert "FOR UPDATE" in str(session.statements[0])


class _ContextSession:
    def __enter__(self) -> "_ContextSession":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


async def _call_asgi(app: object, *, token: str | None = None) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []
    request_sent = False

    async def receive() -> dict[str, object]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    headers = [(b"x-api-key", token.encode())] if token is not None else []
    await app(  # type: ignore[operator]
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/mcp",
            "raw_path": b"/mcp",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        },
        receive,
        send,
    )
    return messages


def test_mcp_middleware_rejects_a_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    downstream_called = False

    async def downstream(scope: object, receive: object, send: object) -> None:
        nonlocal downstream_called
        downstream_called = True

    monkeypatch.setattr(auth, "SessionLocal", _ContextSession)
    monkeypatch.setattr(auth, "authenticate_api_key", lambda session, token: None)

    messages = asyncio.run(_call_asgi(auth.MCPAuthMiddleware(downstream)))

    assert downstream_called is False
    assert messages[0]["status"] == 401
    body = json.loads(messages[1]["body"])
    assert body["error"]["code"] == "UNAUTHORIZED"


def test_mcp_middleware_exposes_and_then_resets_the_workspace_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = auth.AuthContext(
        workspace_id="ws_acme",
        workspace_name="Acme",
        api_key_id="key_123",
        api_key_name="production",
    )
    observed: list[object] = []

    async def downstream(scope: object, receive: object, send: object) -> None:
        observed.append(auth.get_current_auth_context())
        await send({"type": "http.response.start", "status": 204, "headers": []})  # type: ignore[operator]
        await send({"type": "http.response.body", "body": b""})  # type: ignore[operator]

    monkeypatch.setattr(auth, "SessionLocal", _ContextSession)
    monkeypatch.setattr(
        auth,
        "authenticate_api_key",
        lambda session, token: context if token == "working-key" else None,
    )

    messages = asyncio.run(
        _call_asgi(auth.MCPAuthMiddleware(downstream), token="working-key")
    )

    assert messages[0]["status"] == 204
    assert observed == [context]
    assert auth.get_current_auth_context() is None


def test_mcp_middleware_resets_context_when_the_downstream_app_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = auth.AuthContext(
        workspace_id="ws_acme",
        workspace_name="Acme",
        api_key_id="key_123",
        api_key_name="production",
    )

    async def downstream(scope: object, receive: object, send: object) -> None:
        assert auth.get_current_auth_context() == context
        raise RuntimeError("downstream failed")

    monkeypatch.setattr(auth, "SessionLocal", _ContextSession)
    monkeypatch.setattr(auth, "authenticate_api_key", lambda session, token: context)

    with pytest.raises(RuntimeError, match="downstream failed"):
        asyncio.run(_call_asgi(auth.MCPAuthMiddleware(downstream), token="working-key"))

    assert auth.get_current_auth_context() is None
