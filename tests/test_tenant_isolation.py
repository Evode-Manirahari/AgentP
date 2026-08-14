"""Tenant isolation, exercised against a real database.

Every other suite in this repo stands the services up on a ``FakeSession`` that returns
whatever it was handed and ignores the statement entirely. That is fine for testing
branching, but it means a ``.where(Model.workspace_id == workspace_id)`` clause can be
deleted from any query in the codebase without a single test failing.

These tests issue the real SQL against real rows in two populated workspaces. They are the
ones that go red when scoping regresses, so they are deliberately written against the same
service functions the API and MCP layers call rather than against the routes.
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

for _module_name in ["pydantic_settings", "sqlalchemy"]:
    pytest.importorskip(_module_name)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

config = importlib.import_module("app.config")
db = importlib.import_module("app.db")
models = importlib.import_module("app.models")
schemas = importlib.import_module("app.schemas")
auth = importlib.import_module("app.services.auth")
documents_service = importlib.import_module("app.services.documents")
jobs_service = importlib.import_module("app.services.jobs")
storage_service = importlib.import_module("app.services.storage")
webhooks_service = importlib.import_module("app.services.webhooks")
workspaces_service = importlib.import_module("app.services.workspaces")
KnownOperationError = importlib.import_module("app.operations.base").KnownOperationError

ACME = "ws_acme"
RIVAL = "ws_rival"


@compiles(JSONB, "sqlite")
def _compile_jsonb_as_json(element: Any, compiler: Any, **kw: Any) -> str:
    """SQLite has no JSONB. The column semantics under test here are not JSON semantics."""
    return "JSON"


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    db.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as active_session:
        _seed(active_session)
        yield active_session
    db.Base.metadata.drop_all(engine)


def _seed(session: Session) -> None:
    """Two workspaces holding the same shapes of data, so any leak is visible."""
    base = datetime(2026, 8, 1, tzinfo=UTC)
    for index, workspace_id in enumerate((ACME, RIVAL)):
        session.add(
            models.Workspace(
                id=workspace_id,
                name=workspace_id,
                created_at=base,
            )
        )
        session.flush()

        session.add(
            models.Document(
                id=f"file_{workspace_id}",
                workspace_id=workspace_id,
                original_filename=f"{workspace_id}.pdf",
                mime_type="application/pdf",
                size_bytes=1024,
                sha256="a" * 64,
                storage_key=f"workspaces/{workspace_id}/inputs/file/{workspace_id}.pdf",
                page_count=3,
                status=models.DocumentStatus.VALIDATED.value,
                created_at=base + timedelta(minutes=index),
            )
        )
        session.add(
            models.Job(
                id=f"job_{workspace_id}",
                workspace_id=workspace_id,
                operation="compress",
                status=models.JobStatus.QUEUED.value,
                parameters={},
                created_at=base + timedelta(minutes=index),
            )
        )
        session.add(
            models.WebhookEndpoint(
                id=f"wh_{workspace_id}",
                workspace_id=workspace_id,
                url=f"https://{workspace_id}.example.com/hook",
                secret="s" * 32,
                events=["job.succeeded"],
                active=True,
                created_at=base + timedelta(minutes=index),
            )
        )
        session.flush()
        session.add(
            models.WebhookDelivery(
                id=f"whd_{workspace_id}",
                workspace_id=workspace_id,
                endpoint_id=f"wh_{workspace_id}",
                job_id=f"job_{workspace_id}",
                event_type="job.succeeded",
                payload={},
                created_at=base + timedelta(minutes=index),
            )
        )
    session.commit()


def _issue_key(
    session: Session,
    *,
    workspace_id: str,
    revoked: bool = False,
    is_platform_admin: bool = False,
) -> tuple[str, models.ApiKey]:
    token, prefix = auth.generate_api_key_token()
    key = models.ApiKey(
        workspace_id=workspace_id,
        name="production",
        token_hash=auth.hash_api_key(token),
        prefix=prefix,
        is_platform_admin=is_platform_admin,
        created_at=datetime.now(UTC),
        revoked_at=datetime.now(UTC) if revoked else None,
    )
    session.add(key)
    session.commit()
    return token, key


# --- Reads -------------------------------------------------------------------------


def test_a_workspace_lists_only_its_own_files(session: Session) -> None:
    response = documents_service.list_documents_for_response(session, workspace_id=ACME)

    assert [item.file_id for item in response.files] == [f"file_{ACME}"]
    assert response.count == 1


def test_a_workspace_lists_only_its_own_jobs(session: Session) -> None:
    response = jobs_service.list_jobs_for_response(session, workspace_id=ACME)

    assert [item.job_id for item in response.jobs] == [f"job_{ACME}"]
    assert response.count == 1


def test_another_workspaces_job_is_not_readable(session: Session) -> None:
    assert jobs_service.load_job_for_response(session, f"job_{RIVAL}", workspace_id=ACME) is None
    assert jobs_service.load_job_for_response(session, f"job_{ACME}", workspace_id=ACME) is not None


def test_another_workspaces_document_cannot_be_locked(session: Session) -> None:
    assert documents_service.lock_document(session, f"file_{RIVAL}", workspace_id=ACME) is None
    assert (
        documents_service.lock_document(session, f"file_{ACME}", workspace_id=ACME) is not None
    )


def test_a_workspace_lists_only_its_own_webhook_endpoints(session: Session) -> None:
    response = webhooks_service.list_webhook_endpoints(session, workspace_id=ACME)

    assert [item.webhook_id for item in response.webhooks] == [f"wh_{ACME}"]


def test_another_workspaces_webhook_endpoint_is_not_readable(session: Session) -> None:
    assert webhooks_service.get_webhook_endpoint(session, f"wh_{RIVAL}", workspace_id=ACME) is None
    assert (
        webhooks_service.get_webhook_endpoint(session, f"wh_{ACME}", workspace_id=ACME) is not None
    )


def test_a_workspace_lists_only_its_own_webhook_deliveries(session: Session) -> None:
    response = webhooks_service.list_webhook_deliveries(session, workspace_id=ACME)

    assert [item.delivery_id for item in response.deliveries] == [f"whd_{ACME}"]


def test_a_delivery_filter_cannot_reach_across_workspaces(session: Session) -> None:
    """The endpoint filter is caller-supplied, so it must not widen the workspace scope."""
    response = webhooks_service.list_webhook_deliveries(
        session,
        workspace_id=ACME,
        endpoint_id=f"wh_{RIVAL}",
    )

    assert response.deliveries == []


# --- Writes ------------------------------------------------------------------------


def test_another_workspaces_job_cannot_be_canceled(session: Session) -> None:
    with pytest.raises(KnownOperationError) as exc:
        jobs_service.cancel_job(
            session,
            workspace_id=ACME,
            job_id=f"job_{RIVAL}",
            settings=config.Settings(),
        )

    assert exc.value.code == "JOB_NOT_FOUND"
    rival_job = session.get(models.Job, f"job_{RIVAL}")
    assert rival_job.status == models.JobStatus.QUEUED.value


def test_another_workspaces_file_cannot_be_deleted(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted: list[str] = []
    monkeypatch.setattr(
        storage_service.StorageService,
        "delete_object",
        lambda self, *, key: deleted.append(key),
    )

    with pytest.raises(KnownOperationError) as exc:
        documents_service.delete_document(
            session,
            file_id=f"file_{RIVAL}",
            workspace_id=ACME,
            settings=config.Settings(),
        )

    assert exc.value.code == "FILE_NOT_FOUND"
    assert deleted == []
    rival_document = session.get(models.Document, f"file_{RIVAL}")
    assert rival_document.status == models.DocumentStatus.VALIDATED.value


def test_another_workspaces_webhook_endpoint_cannot_be_disabled(session: Session) -> None:
    assert (
        webhooks_service.disable_webhook_endpoint(session, f"wh_{RIVAL}", workspace_id=ACME)
        is None
    )

    rival_endpoint = session.get(models.WebhookEndpoint, f"wh_{RIVAL}")
    assert rival_endpoint.active is True


def test_another_workspaces_file_cannot_become_a_job_input(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jobs_service, "enqueue_job", lambda job_id, *, settings: "rq_1")

    with pytest.raises(KnownOperationError) as exc:
        jobs_service.create_job(
            session,
            workspace_id=ACME,
            request=schemas.JobCreate(
                operation="compress",
                inputs=[schemas.JobInputRef(file_id=f"file_{RIVAL}")],
                parameters={},
            ),
            idempotency_key=None,
            settings=config.Settings(),
        )

    assert exc.value.code == "FILE_NOT_FOUND"


def test_a_job_only_sees_blocking_jobs_from_its_own_workspace(session: Session) -> None:
    """A neighbour's in-flight job must not make this workspace's file undeletable."""
    session.add(
        models.JobInput(
            job_id=f"job_{RIVAL}",
            document_id=f"file_{ACME}",
            position=0,
        )
    )
    session.commit()

    assert (
        documents_service.is_in_active_job(session, f"file_{ACME}", workspace_id=ACME) is False
    )
    assert (
        documents_service.is_in_active_job(session, f"file_{ACME}", workspace_id=RIVAL) is True
    )


# --- Idempotency -------------------------------------------------------------------


def test_the_same_idempotency_key_is_independent_per_workspace(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The uniqueness constraint moved to (workspace_id, idempotency_key) in 0005."""
    monkeypatch.setattr(jobs_service, "enqueue_job", lambda job_id, *, settings: "rq_1")

    def submit(workspace_id: str) -> str:
        job = jobs_service.create_job(
            session,
            workspace_id=workspace_id,
            request=schemas.JobCreate(
                operation="compress",
                inputs=[schemas.JobInputRef(file_id=f"file_{workspace_id}")],
                parameters={},
            ),
            idempotency_key="shared-key",
            settings=config.Settings(),
        )
        return job.id

    acme_job_id = submit(ACME)
    rival_job_id = submit(RIVAL)

    assert acme_job_id != rival_job_id
    assert session.get(models.Job, acme_job_id).workspace_id == ACME
    assert session.get(models.Job, rival_job_id).workspace_id == RIVAL

    # Replaying the key inside one workspace still returns that workspace's original job.
    assert submit(ACME) == acme_job_id


# --- Authentication ----------------------------------------------------------------


def test_authentication_resolves_the_owning_workspace(session: Session) -> None:
    token, key = _issue_key(session, workspace_id=RIVAL)

    context = auth.authenticate_api_key(session, token)

    assert context is not None
    assert context.workspace_id == RIVAL
    assert context.api_key_id == key.id
    assert context.is_platform_admin is False


def test_a_revoked_key_no_longer_authenticates(session: Session) -> None:
    token, _ = _issue_key(session, workspace_id=ACME, revoked=True)

    assert auth.authenticate_api_key(session, token) is None


def test_an_unknown_token_does_not_authenticate(session: Session) -> None:
    _issue_key(session, workspace_id=ACME)
    unknown, _ = auth.generate_api_key_token()

    assert auth.authenticate_api_key(session, unknown) is None


def test_rotation_invalidates_the_old_token_and_issues_a_working_one(session: Session) -> None:
    old_token, key = _issue_key(session, workspace_id=ACME)

    rotated = workspaces_service.rotate_workspace_api_key(
        session,
        workspace_id=ACME,
        api_key_id=key.id,
    )

    assert auth.authenticate_api_key(session, old_token) is None
    context = auth.authenticate_api_key(session, rotated.token)
    assert context is not None
    assert context.workspace_id == ACME


def test_another_workspaces_api_key_cannot_be_rotated(session: Session) -> None:
    token, key = _issue_key(session, workspace_id=RIVAL)

    with pytest.raises(KnownOperationError) as exc:
        workspaces_service.rotate_workspace_api_key(
            session,
            workspace_id=ACME,
            api_key_id=key.id,
        )

    assert exc.value.code == "API_KEY_NOT_FOUND"
    assert auth.authenticate_api_key(session, token) is not None


def test_another_workspaces_api_key_cannot_be_revoked(session: Session) -> None:
    token, key = _issue_key(session, workspace_id=RIVAL)

    with pytest.raises(KnownOperationError) as exc:
        workspaces_service.revoke_workspace_api_key(
            session,
            workspace_id=ACME,
            api_key_id=key.id,
        )

    assert exc.value.code == "API_KEY_NOT_FOUND"
    assert auth.authenticate_api_key(session, token) is not None


@pytest.mark.parametrize("operation", ["rotate", "revoke"])
def test_an_ordinary_key_cannot_mint_admin_rights_from_the_admin_key(
    session: Session,
    operation: str,
) -> None:
    """Rotation preserves the admin flag and returns a token, so it must stay privileged.

    The bootstrap admin key shares the default workspace with ordinary keys, so workspace
    scoping alone does not separate them.
    """
    admin_token, admin_key = _issue_key(session, workspace_id=ACME, is_platform_admin=True)
    _issue_key(session, workspace_id=ACME)

    call = getattr(workspaces_service, f"{operation}_workspace_api_key")
    with pytest.raises(KnownOperationError) as exc:
        call(session, workspace_id=ACME, api_key_id=admin_key.id)

    assert exc.value.code == "PLATFORM_ADMIN_REQUIRED"
    # The admin credential is untouched and still the only admin credential.
    context = auth.authenticate_api_key(session, admin_token)
    assert context is not None
    assert context.is_platform_admin is True


def test_a_platform_admin_may_rotate_its_own_key(session: Session) -> None:
    admin_token, admin_key = _issue_key(session, workspace_id=ACME, is_platform_admin=True)

    rotated = workspaces_service.rotate_workspace_api_key(
        session,
        workspace_id=ACME,
        api_key_id=admin_key.id,
        allow_platform_admin=True,
    )

    assert auth.authenticate_api_key(session, admin_token) is None
    context = auth.authenticate_api_key(session, rotated.token)
    assert context is not None
    assert context.is_platform_admin is True


def test_key_listing_is_scoped_to_the_workspace(session: Session) -> None:
    _issue_key(session, workspace_id=ACME)
    _issue_key(session, workspace_id=RIVAL)

    response = workspaces_service.list_workspace_api_keys(session, workspace_id=ACME)

    assert response.count == 1
    assert all(
        session.get(models.ApiKey, item.api_key_id).workspace_id == ACME
        for item in response.api_keys
    )


def test_a_new_workspace_is_created_with_a_working_key(session: Session) -> None:
    created = workspaces_service.create_workspace(
        session,
        request=schemas.WorkspaceCreate(name="Third Party", initial_key_name="production"),
    )

    context = auth.authenticate_api_key(session, created.api_key.token)

    assert context is not None
    assert context.workspace_id == created.workspace.workspace_id
    assert context.workspace_id not in {ACME, RIVAL}
    # A tenant key must not be able to mint further tenants.
    assert context.is_platform_admin is False


# --- Storage layout ----------------------------------------------------------------


def test_storage_keys_are_partitioned_by_workspace() -> None:
    storage = storage_service.StorageService(config.Settings())

    input_key = storage.input_key(workspace_id=ACME, document_id="file_1", filename="a.pdf")
    output_key = storage.output_key(workspace_id=ACME, job_id="job_1", filename="a.pdf")

    assert input_key.startswith(f"workspaces/{ACME}/inputs/file_1/")
    assert output_key.startswith(f"workspaces/{ACME}/outputs/job_1/")
