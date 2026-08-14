import importlib
from datetime import UTC, datetime

import pytest

for _module_name in ["pydantic_settings", "sqlalchemy"]:
    pytest.importorskip(_module_name)

models = importlib.import_module("app.models")
schemas = importlib.import_module("app.schemas")
workspaces = importlib.import_module("app.services.workspaces")
provision = importlib.import_module("app.provision")
KnownOperationError = importlib.import_module("app.operations.base").KnownOperationError


class FakeSession:
    def __init__(self, scalar_results: list[object] | None = None) -> None:
        self.scalar_results = list(scalar_results or [])
        self.added: list[object] = []
        self.commits = 0
        self.flushes = 0
        self.statements: list[object] = []

    def scalar(self, statement: object) -> object | None:
        self.statements.append(statement)
        return self.scalar_results.pop(0) if self.scalar_results else None

    def scalars(self, statement: object) -> list[object]:
        result = self.scalar()
        return list(result) if isinstance(result, list) else []

    def add(self, item: object) -> None:
        self.added.append(item)

    def flush(self) -> None:
        self.flushes += 1
        for index, item in enumerate(self.added, start=1):
            if getattr(item, "id", None) is None:
                prefix = "ws" if isinstance(item, models.Workspace) else "key"
                item.id = f"{prefix}_{index + 100}"
            if getattr(item, "created_at", None) is None:
                item.created_at = datetime.now(UTC)

    def commit(self) -> None:
        self.commits += 1


def _api_key(*, revoked: bool = False, admin: bool = False) -> object:
    return models.ApiKey(
        id="key_1",
        workspace_id="ws_acme",
        name="production",
        token_hash="a" * 64,
        prefix="agentp_12345678",
        is_platform_admin=admin,
        created_at=datetime.now(UTC),
        revoked_at=datetime.now(UTC) if revoked else None,
    )


def _workspace(workspace_id: str = "ws_acme") -> object:
    return models.Workspace(
        id=workspace_id,
        name="Acme",
        created_at=datetime.now(UTC),
    )


def test_creating_a_workspace_returns_its_one_time_initial_key() -> None:
    session = FakeSession()

    response = workspaces.create_workspace(
        session,
        request=schemas.WorkspaceCreate(
            name="  Acme Lending  ",
            initial_key_name=" production ",
        ),
    )

    assert response.workspace.name == "Acme Lending"
    assert response.api_key.name == "production"
    assert response.api_key.token.startswith("agentp_")
    key_record = next(item for item in session.added if isinstance(item, models.ApiKey))
    assert key_record.token_hash == importlib.import_module("app.services.auth").hash_api_key(
        response.api_key.token
    )
    assert response.api_key.token != key_record.token_hash
    assert session.commits == 1


def test_rotating_a_key_revokes_it_and_preserves_admin_capability() -> None:
    current = _api_key(admin=True)
    session = FakeSession([_workspace(), current])

    replacement = workspaces.rotate_workspace_api_key(
        session,
        workspace_id="ws_acme",
        api_key_id="key_1",
        allow_platform_admin=True,
    )

    assert current.revoked_at is not None
    assert replacement.api_key_id != current.id
    assert replacement.is_platform_admin is True
    assert replacement.token.startswith("agentp_")


def test_a_revoked_key_cannot_be_rotated() -> None:
    with pytest.raises(KnownOperationError) as exc:
        workspaces.rotate_workspace_api_key(
            FakeSession([_workspace(), _api_key(revoked=True)]),
            workspace_id="ws_acme",
            api_key_id="key_1",
        )

    assert exc.value.code == "API_KEY_REVOKED"


def test_the_last_active_key_cannot_be_revoked() -> None:
    with pytest.raises(KnownOperationError) as exc:
        workspaces.revoke_workspace_api_key(
            FakeSession([_workspace(), _api_key(), 1]),
            workspace_id="ws_acme",
            api_key_id="key_1",
        )

    assert exc.value.code == "LAST_ACTIVE_API_KEY"


def test_revocation_is_idempotent() -> None:
    key = _api_key(revoked=True)
    response = workspaces.revoke_workspace_api_key(
        FakeSession([_workspace(), key]),
        workspace_id="ws_acme",
        api_key_id="key_1",
    )

    assert response.revoked_at == key.revoked_at


def test_key_lookup_is_scoped_to_the_workspace() -> None:
    session = FakeSession([_workspace("ws_other"), None])

    with pytest.raises(KnownOperationError) as exc:
        workspaces.rotate_workspace_api_key(
            session,
            workspace_id="ws_other",
            api_key_id="key_1",
        )

    assert exc.value.code == "API_KEY_NOT_FOUND"


@pytest.mark.parametrize("operation", ["rotate", "revoke"])
def test_a_workspace_key_cannot_manage_a_platform_admin_key(operation: str) -> None:
    admin_key = _api_key(admin=True)
    session = FakeSession([_workspace(), admin_key])

    with pytest.raises(KnownOperationError) as exc:
        if operation == "rotate":
            workspaces.rotate_workspace_api_key(
                session,
                workspace_id="ws_acme",
                api_key_id=admin_key.id,
            )
        else:
            workspaces.revoke_workspace_api_key(
                session,
                workspace_id="ws_acme",
                api_key_id=admin_key.id,
            )

    assert exc.value.code == "PLATFORM_ADMIN_REQUIRED"
    assert admin_key.revoked_at is None
    assert session.commits == 0


def test_key_mutations_lock_the_workspace_before_reading_the_key() -> None:
    session = FakeSession([_workspace(), _api_key(), 2])

    workspaces.revoke_workspace_api_key(
        session,
        workspace_id="ws_acme",
        api_key_id="key_1",
    )

    assert "FROM workspaces" in str(session.statements[0])
    assert "FOR UPDATE" in str(session.statements[0])


def test_the_cli_exposes_workspace_and_key_lifecycle_commands() -> None:
    parser = provision.build_parser()

    assert parser.parse_args(["create-workspace", "Acme"]).command == "create-workspace"
    assert parser.parse_args(["create-key", "ws_acme"]).command == "create-key"
    assert parser.parse_args(["list-keys", "ws_acme"]).command == "list-keys"
    assert parser.parse_args(["rotate-key", "ws_acme", "key_1"]).command == "rotate-key"
    assert parser.parse_args(["revoke-key", "ws_acme", "key_1"]).command == "revoke-key"


def test_the_cli_requires_a_command() -> None:
    with pytest.raises(SystemExit):
        provision.build_parser().parse_args([])
