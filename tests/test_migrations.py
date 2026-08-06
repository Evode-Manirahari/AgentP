import importlib

import pytest

for _module_name in ["alembic", "pydantic_settings", "sqlalchemy"]:
    pytest.importorskip(_module_name)

pytest.importorskip("app.models")
db = importlib.import_module("app.db")
migration_0001 = importlib.import_module("migrations.versions.0001_initial_schema")
migration_0002 = importlib.import_module("migrations.versions.0002_job_queue_id")


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

    assert set(created_tables) == set(db.Base.metadata.tables)


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
    )


def test_legacy_schema_patch_stamps_current_unversioned_schema() -> None:
    patch = db._legacy_schema_patch(
        table_names=_model_tables(),
        columns_by_table=_model_columns_by_table(),
        indexes_by_table={"jobs": {"ix_jobs_queue_job_id"}},
    )

    assert patch == db.LegacySchemaPatch(missing_columns={}, missing_indexes=set())


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
