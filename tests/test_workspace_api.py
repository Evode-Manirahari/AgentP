from __future__ import annotations

import importlib
from collections.abc import Generator
from dataclasses import dataclass

import pytest

for _module_name in ["fastapi", "httpx", "pydantic_settings", "sqlalchemy"]:
    pytest.importorskip(_module_name)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

api_errors = importlib.import_module("app.api.errors")
workspace_api = importlib.import_module("app.api.workspaces")
usage_api = importlib.import_module("app.api.usage")
db = importlib.import_module("app.db")
models = importlib.import_module("app.models")
auth = importlib.import_module("app.services.auth")


@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json(element: object, compiler: object, **kw: object) -> str:
    return "JSON"


@dataclass(frozen=True)
class WorkspaceApi:
    client: TestClient
    sessions: sessionmaker[Session]
    admin_token: str
    admin_key_id: str
    default_key_token: str
    tenant_token: str
    tenant_key_id: str


def _stored_key(
    *,
    workspace_id: str,
    name: str,
    is_platform_admin: bool = False,
) -> tuple[str, object]:
    token, prefix = auth.generate_api_key_token()
    return token, models.ApiKey(
        workspace_id=workspace_id,
        name=name,
        token_hash=auth.hash_api_key(token),
        prefix=prefix,
        is_platform_admin=is_platform_admin,
    )


@pytest.fixture
def workspace_api_fixture() -> Generator[WorkspaceApi, None, None]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Workspace.__table__.create(engine)
    models.ApiKey.__table__.create(engine)
    models.Job.__table__.create(engine)
    models.Document.__table__.create(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    admin_token, admin_key = _stored_key(
        workspace_id=models.DEFAULT_WORKSPACE_ID,
        name="platform admin",
        is_platform_admin=True,
    )
    default_key_token, default_key = _stored_key(
        workspace_id=models.DEFAULT_WORKSPACE_ID,
        name="default automation",
    )
    tenant_token, tenant_key = _stored_key(
        workspace_id="ws_tenant",
        name="tenant production",
    )
    with sessions() as session:
        session.add_all(
            [
                models.Workspace(id=models.DEFAULT_WORKSPACE_ID, name="Default Workspace"),
                models.Workspace(id="ws_tenant", name="Tenant"),
                admin_key,
                default_key,
                tenant_key,
            ]
        )
        session.commit()

    test_app = FastAPI()
    api_errors.install_exception_handlers(test_app)
    test_app.include_router(workspace_api.workspace_router, prefix="/v1")
    test_app.include_router(workspace_api.api_key_router, prefix="/v1")
    test_app.include_router(usage_api.router, prefix="/v1")

    def get_test_session() -> Generator[Session, None, None]:
        with sessions() as session:
            yield session

    test_app.dependency_overrides[db.get_session] = get_test_session
    with TestClient(test_app) as client:
        yield WorkspaceApi(
            client=client,
            sessions=sessions,
            admin_token=admin_token,
            admin_key_id=admin_key.id,
            default_key_token=default_key_token,
            tenant_token=tenant_token,
            tenant_key_id=tenant_key.id,
        )
    engine.dispose()


def _headers(token: str) -> dict[str, str]:
    return {"X-API-Key": token}


def test_workspace_routes_require_an_active_key(workspace_api_fixture: WorkspaceApi) -> None:
    response = workspace_api_fixture.client.get("/v1/workspaces/current")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_platform_admin_can_create_a_workspace_with_a_working_initial_key(
    workspace_api_fixture: WorkspaceApi,
) -> None:
    response = workspace_api_fixture.client.post(
        "/v1/workspaces",
        headers=_headers(workspace_api_fixture.admin_token),
        json={"name": "Acme Lending", "initial_key_name": "production"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["workspace"]["name"] == "Acme Lending"
    assert body["api_key"]["token"].startswith("agentp_")
    with workspace_api_fixture.sessions() as session:
        context = auth.authenticate_api_key(session, body["api_key"]["token"])
    assert context is not None
    assert context.workspace_id == body["workspace"]["workspace_id"]
    assert context.is_platform_admin is False


def test_ordinary_workspace_key_cannot_create_workspaces(
    workspace_api_fixture: WorkspaceApi,
) -> None:
    response = workspace_api_fixture.client.post(
        "/v1/workspaces",
        headers=_headers(workspace_api_fixture.tenant_token),
        json={"name": "Forbidden", "initial_key_name": "default"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PLATFORM_ADMIN_REQUIRED"


def test_ordinary_key_cannot_rotate_a_platform_admin_key(
    workspace_api_fixture: WorkspaceApi,
) -> None:
    response = workspace_api_fixture.client.post(
        f"/v1/api-keys/{workspace_api_fixture.admin_key_id}/rotate",
        headers=_headers(workspace_api_fixture.default_key_token),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PLATFORM_ADMIN_REQUIRED"
    with workspace_api_fixture.sessions() as session:
        assert auth.authenticate_api_key(session, workspace_api_fixture.admin_token) is not None


def test_last_active_workspace_key_cannot_be_revoked(
    workspace_api_fixture: WorkspaceApi,
) -> None:
    response = workspace_api_fixture.client.post(
        f"/v1/api-keys/{workspace_api_fixture.tenant_key_id}/revoke",
        headers=_headers(workspace_api_fixture.tenant_token),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LAST_ACTIVE_API_KEY"


def test_key_lifecycle_returns_secrets_once_and_stays_in_workspace(
    workspace_api_fixture: WorkspaceApi,
) -> None:
    created = workspace_api_fixture.client.post(
        "/v1/api-keys",
        headers=_headers(workspace_api_fixture.default_key_token),
        json={"name": "automation"},
    )
    assert created.status_code == 201
    created_body = created.json()

    listed = workspace_api_fixture.client.get(
        "/v1/api-keys",
        headers=_headers(workspace_api_fixture.default_key_token),
    )
    assert listed.status_code == 200
    listed_key = next(
        item
        for item in listed.json()["api_keys"]
        if item["api_key_id"] == created_body["api_key_id"]
    )
    assert "token" not in listed_key

    cross_workspace = workspace_api_fixture.client.post(
        f"/v1/api-keys/{created_body['api_key_id']}/rotate",
        headers=_headers(workspace_api_fixture.tenant_token),
    )
    assert cross_workspace.status_code == 404
    assert cross_workspace.json()["error"]["code"] == "API_KEY_NOT_FOUND"

    rotated = workspace_api_fixture.client.post(
        f"/v1/api-keys/{created_body['api_key_id']}/rotate",
        headers=_headers(workspace_api_fixture.default_key_token),
    )
    assert rotated.status_code == 200
    with workspace_api_fixture.sessions() as session:
        assert auth.authenticate_api_key(session, created_body["token"]) is None
        assert auth.authenticate_api_key(session, rotated.json()["token"]) is not None


def test_usage_endpoint_reports_only_the_authenticated_workspace(
    workspace_api_fixture: WorkspaceApi,
) -> None:
    with workspace_api_fixture.sessions() as session:
        session.add_all(
            [
                models.Document(
                    id="file_tenant",
                    workspace_id="ws_tenant",
                    original_filename="tenant.pdf",
                    mime_type="application/pdf",
                    size_bytes=321,
                    sha256="a" * 64,
                    storage_key="workspaces/ws_tenant/tenant.pdf",
                    page_count=1,
                    status=models.DocumentStatus.VALIDATED.value,
                ),
                models.Document(
                    id="file_default",
                    workspace_id=models.DEFAULT_WORKSPACE_ID,
                    original_filename="default.pdf",
                    mime_type="application/pdf",
                    size_bytes=9_999,
                    sha256="b" * 64,
                    storage_key="workspaces/ws_default/default.pdf",
                    page_count=1,
                    status=models.DocumentStatus.VALIDATED.value,
                ),
                models.Job(
                    id="job_tenant",
                    workspace_id="ws_tenant",
                    operation="prepare_packet",
                    status=models.JobStatus.RUNNING.value,
                    parameters={},
                ),
            ]
        )
        session.commit()

    response = workspace_api_fixture.client.get(
        "/v1/usage",
        headers=_headers(workspace_api_fixture.tenant_token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["workspace_id"] == "ws_tenant"
    assert body["storage_bytes"]["used"] == 321
    assert body["documents"]["used"] == 1
    assert body["active_jobs"]["used"] == 1
