import importlib

import pytest

for _module_name in ["alembic", "pydantic_settings", "sqlalchemy"]:
    pytest.importorskip(_module_name)

pytest.importorskip("app.models")
db = importlib.import_module("app.db")
migration_0001 = importlib.import_module("migrations.versions.0001_initial_schema")
migration_0002 = importlib.import_module("migrations.versions.0002_job_queue_id")
migration_0003 = importlib.import_module("migrations.versions.0003_webhooks")
migration_0005 = importlib.import_module("migrations.versions.0005_workspaces")

INITIAL_TABLES = {"audit_events", "documents", "job_inputs", "job_outputs", "jobs"}
WEBHOOK_TABLES = {"webhook_endpoints", "webhook_deliveries"}


def test_initial_migration_creates_model_tables() -> None:
    created_tables: list[str] = []
    original_create_table = migration_0001.op.create_table
    original_create_index = migration_0001.op.create_index

    def record_create_table(name: str, *args: object, **kwargs: object) -> None:
        created_tables.append(name)

    migration_0001.op.create_table = record_create_table
    migration_0001.op.create_index = lambda *args, **kwargs: None
    try:
        migration_0001.upgrade()
    finally:
        migration_0001.op.create_table = original_create_table
        migration_0001.op.create_index = original_create_index

    assert set(created_tables) == INITIAL_TABLES


def test_queue_id_migration_adds_column_and_index() -> None:
    added_columns: list[tuple[str, str]] = []
    created_indexes: list[tuple[str, str]] = []
    original_add_column = migration_0002.op.add_column
    original_create_index = migration_0002.op.create_index

    def record_add_column(table_name: str, column: object) -> None:
        added_columns.append((table_name, column.name))

    def record_create_index(index_name: str, table_name: str, columns: list[str]) -> None:
        created_indexes.append((table_name, index_name))

    migration_0002.op.add_column = record_add_column
    migration_0002.op.create_index = record_create_index
    try:
        migration_0002.upgrade()
    finally:
        migration_0002.op.add_column = original_add_column
        migration_0002.op.create_index = original_create_index

    assert added_columns == [("jobs", "queue_job_id")]
    assert created_indexes == [("jobs", "ix_jobs_queue_job_id")]


def test_webhook_migration_creates_tables_and_indexes() -> None:
    created_tables: list[str] = []
    created_indexes: list[tuple[str, str]] = []
    original_create_table = migration_0003.op.create_table
    original_create_index = migration_0003.op.create_index

    def record_create_table(name: str, *args: object, **kwargs: object) -> None:
        created_tables.append(name)

    def record_create_index(index_name: str, table_name: str, columns: list[str]) -> None:
        created_indexes.append((table_name, index_name))

    migration_0003.op.create_table = record_create_table
    migration_0003.op.create_index = record_create_index
    try:
        migration_0003.upgrade()
    finally:
        migration_0003.op.create_table = original_create_table
        migration_0003.op.create_index = original_create_index

    assert created_tables == ["webhook_endpoints", "webhook_deliveries"]
    assert ("webhook_endpoints", "ix_webhook_endpoints_active") in created_indexes
    assert (
        "webhook_deliveries",
        "ix_webhook_deliveries_endpoint_created",
    ) in created_indexes


def test_workspace_migration_creates_identity_tables_and_scopes_owned_tables() -> None:
    created_tables: list[str] = []
    added_columns: list[tuple[str, str]] = []
    original_create_table = migration_0005.op.create_table
    original_add_column = migration_0005.op.add_column
    original_execute = migration_0005.op.execute
    original_alter_column = migration_0005.op.alter_column
    original_create_foreign_key = migration_0005.op.create_foreign_key
    original_drop_constraint = migration_0005.op.drop_constraint
    original_drop_index = migration_0005.op.drop_index
    original_create_unique_constraint = migration_0005.op.create_unique_constraint
    original_create_index = migration_0005.op.create_index

    migration_0005.op.create_table = (
        lambda name, *args, **kwargs: created_tables.append(name)
    )
    migration_0005.op.add_column = (
        lambda table_name, column: added_columns.append((table_name, column.name))
    )
    migration_0005.op.execute = lambda *args, **kwargs: None
    migration_0005.op.alter_column = lambda *args, **kwargs: None
    migration_0005.op.create_foreign_key = lambda *args, **kwargs: None
    migration_0005.op.drop_constraint = lambda *args, **kwargs: None
    migration_0005.op.drop_index = lambda *args, **kwargs: None
    migration_0005.op.create_unique_constraint = lambda *args, **kwargs: None
    migration_0005.op.create_index = lambda *args, **kwargs: None
    try:
        migration_0005.upgrade()
    finally:
        migration_0005.op.create_table = original_create_table
        migration_0005.op.add_column = original_add_column
        migration_0005.op.execute = original_execute
        migration_0005.op.alter_column = original_alter_column
        migration_0005.op.create_foreign_key = original_create_foreign_key
        migration_0005.op.drop_constraint = original_drop_constraint
        migration_0005.op.drop_index = original_drop_index
        migration_0005.op.create_unique_constraint = original_create_unique_constraint
        migration_0005.op.create_index = original_create_index

    assert created_tables == ["workspaces", "api_keys"]
    assert set(added_columns) == {
        ("jobs", "workspace_id"),
        ("documents", "workspace_id"),
        ("audit_events", "workspace_id"),
        ("webhook_endpoints", "workspace_id"),
        ("webhook_deliveries", "workspace_id"),
    }


def _model_tables() -> set[str]:
    return set(db.Base.metadata.tables)


def _model_columns_by_table() -> dict[str, set[str]]:
    return {
        table_name: {column.name for column in table.columns}
        for table_name, table in db.Base.metadata.tables.items()
    }


def test_legacy_schema_patch_adds_known_missing_columns_and_index() -> None:
    table_names = _model_tables()
    columns_by_table = _model_columns_by_table()
    columns_by_table["jobs"] = columns_by_table["jobs"] - {
        "idempotency_fingerprint",
        "queue_job_id",
    }

    patch = db._legacy_schema_patch(
        table_names=table_names,
        columns_by_table=columns_by_table,
        indexes_by_table={"jobs": {"ix_jobs_idempotency_key", "ix_jobs_status_created"}},
    )

    assert patch == db.LegacySchemaPatch(
        missing_columns={"jobs": {"idempotency_fingerprint", "queue_job_id"}},
        missing_indexes={"ix_jobs_queue_job_id"},
        stamp_revision="head",
    )


def test_legacy_schema_patch_stamps_current_unversioned_schema() -> None:
    patch = db._legacy_schema_patch(
        table_names=_model_tables(),
        columns_by_table=_model_columns_by_table(),
        indexes_by_table={"jobs": {"ix_jobs_queue_job_id"}},
    )

    assert patch == db.LegacySchemaPatch(
        missing_columns={},
        missing_indexes=set(),
        stamp_revision="head",
    )


def test_legacy_schema_patch_stamps_legacy_tables_before_new_migrations() -> None:
    table_names = INITIAL_TABLES
    columns_by_table = {
        table_name: columns - db.POST_REVISION_COLUMNS["0002_job_queue_id"].get(
            table_name, set()
        )
        for table_name, columns in _model_columns_by_table().items()
        if table_name in INITIAL_TABLES
    }
    patch = db._legacy_schema_patch(
        table_names=table_names,
        columns_by_table=columns_by_table,
        indexes_by_table={"jobs": {"ix_jobs_queue_job_id"}},
    )

    assert patch == db.LegacySchemaPatch(
        missing_columns={},
        missing_indexes=set(),
        stamp_revision="0002_job_queue_id",
    )


@pytest.mark.parametrize(
    ("has_deleted_at", "expected_revision"),
    [
        (False, "0003_webhooks"),
        (True, "0004_document_deletion"),
    ],
)
def test_legacy_schema_patch_detects_previous_unversioned_releases(
    has_deleted_at: bool,
    expected_revision: str,
) -> None:
    table_names = INITIAL_TABLES | WEBHOOK_TABLES
    columns_by_table = {
        table_name: columns
        - db.POST_REVISION_COLUMNS[expected_revision].get(table_name, set())
        for table_name, columns in _model_columns_by_table().items()
        if table_name in table_names
    }
    if not has_deleted_at:
        columns_by_table["documents"].discard("deleted_at")

    patch = db._legacy_schema_patch(
        table_names=table_names,
        columns_by_table=columns_by_table,
        indexes_by_table={"jobs": {"ix_jobs_queue_job_id"}},
    )

    assert patch == db.LegacySchemaPatch(
        missing_columns={},
        missing_indexes=set(),
        stamp_revision=expected_revision,
    )


def test_legacy_schema_patch_rejects_an_out_of_order_deletion_column() -> None:
    columns_by_table = {
        table_name: columns - {"workspace_id"}
        for table_name, columns in _model_columns_by_table().items()
        if table_name in INITIAL_TABLES
    }

    with pytest.raises(RuntimeError, match="webhook tables are missing"):
        db._legacy_schema_patch(
            table_names=INITIAL_TABLES,
            columns_by_table=columns_by_table,
            indexes_by_table={"jobs": {"ix_jobs_queue_job_id"}},
        )


def test_legacy_schema_patch_ignores_versioned_schema() -> None:
    patch = db._legacy_schema_patch(
        table_names=_model_tables() | {"alembic_version"},
        columns_by_table={},
        indexes_by_table={},
    )

    assert patch is None


def test_legacy_schema_patch_rejects_unexpected_missing_columns() -> None:
    columns_by_table = _model_columns_by_table()
    columns_by_table["documents"] = columns_by_table["documents"] - {"storage_key"}

    with pytest.raises(RuntimeError, match="documents.storage_key"):
        db._legacy_schema_patch(
            table_names=_model_tables(),
            columns_by_table=columns_by_table,
            indexes_by_table={},
        )
