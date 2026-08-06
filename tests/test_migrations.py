import importlib

import pytest

for _module_name in ["alembic", "pydantic_settings", "sqlalchemy"]:
    pytest.importorskip(_module_name)

db = importlib.import_module("app.db")
migration = importlib.import_module("migrations.versions.0001_initial_schema")


def test_initial_migration_creates_model_tables() -> None:
    created_tables: list[str] = []
    original_create_table = migration.op.create_table
    original_create_index = migration.op.create_index

    def record_create_table(name: str, *args: object, **kwargs: object) -> None:
        created_tables.append(name)

    migration.op.create_table = record_create_table
    migration.op.create_index = lambda *args, **kwargs: None
    try:
        migration.upgrade()
    finally:
        migration.op.create_table = original_create_table
        migration.op.create_index = original_create_index

    assert set(created_tables) == set(db.Base.metadata.tables)
