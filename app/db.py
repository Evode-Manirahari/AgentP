import time
from collections.abc import Generator
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)
ALEMBIC_CONFIG_PATH = Path(__file__).resolve().parent.parent / "alembic.ini"
LEGACY_SCHEMA_REVISION = "0002_job_queue_id"
LEGACY_MODEL_TABLES = {"audit_events", "documents", "job_inputs", "job_outputs", "jobs"}
LEGACY_WEBHOOK_TABLES = {"webhook_endpoints", "webhook_deliveries"}
WORKSPACE_TABLES = {"workspaces", "api_keys"}
ADOPTABLE_LEGACY_COLUMNS = {
    "jobs": {"idempotency_fingerprint", "queue_job_id"},
    "documents": {"deleted_at"},
}
# Columns introduced after each adoptable unversioned schema revision. Alembic will add these
# after we stamp the matching revision, so treating them as missing legacy columns would either
# patch them twice or stamp the database too far ahead.
POST_REVISION_COLUMNS = {
    "0002_job_queue_id": {
        "audit_events": {"workspace_id"},
        "documents": {"deleted_at", "workspace_id"},
        "jobs": {"workspace_id"},
    },
    "0003_webhooks": {
        "audit_events": {"workspace_id"},
        "documents": {"deleted_at", "workspace_id"},
        "jobs": {"workspace_id"},
        "webhook_endpoints": {"workspace_id"},
        "webhook_deliveries": {"workspace_id"},
    },
    "0004_document_deletion": {
        "audit_events": {"workspace_id"},
        "documents": {"workspace_id"},
        "jobs": {"workspace_id"},
        "webhook_endpoints": {"workspace_id"},
        "webhook_deliveries": {"workspace_id"},
    },
}
LEGACY_COLUMN_DDL = {
    ("jobs", "idempotency_fingerprint"): (
        "ALTER TABLE jobs ADD COLUMN idempotency_fingerprint VARCHAR(64)"
    ),
    ("jobs", "queue_job_id"): "ALTER TABLE jobs ADD COLUMN queue_job_id VARCHAR(128)",
    ("documents", "deleted_at"): "ALTER TABLE documents ADD COLUMN deleted_at TIMESTAMPTZ",
}
LEGACY_INDEX_DDL = {
    "ix_jobs_queue_job_id": (
        "CREATE INDEX IF NOT EXISTS ix_jobs_queue_job_id ON jobs (queue_job_id)"
    ),
}


class LegacySchemaPatch(NamedTuple):
    missing_columns: dict[str, set[str]]
    missing_indexes: set[str]
    stamp_revision: str


def _model_table_names() -> set[str]:
    return set(Base.metadata.tables)


def _model_columns_by_table() -> dict[str, set[str]]:
    return {
        table_name: {column.name for column in table.columns}
        for table_name, table in Base.metadata.tables.items()
    }


def _format_schema_items(items_by_table: dict[str, set[str]]) -> str:
    return ", ".join(
        f"{table}.{column}"
        for table, columns in sorted(items_by_table.items())
        for column in sorted(columns)
    )


def _legacy_schema_patch(
    table_names: set[str],
    columns_by_table: dict[str, set[str]],
    indexes_by_table: dict[str, set[str]],
) -> LegacySchemaPatch | None:
    if "alembic_version" in table_names:
        return None

    model_tables = _model_table_names()
    existing_model_tables = model_tables & table_names
    if not existing_model_tables:
        return None

    missing_legacy_tables = LEGACY_MODEL_TABLES - table_names
    if missing_legacy_tables:
        missing = ", ".join(sorted(missing_legacy_tables))
        raise RuntimeError(
            "Cannot adopt existing unversioned database schema; missing model tables: "
            f"{missing}."
        )

    adopts_whole_model = model_tables <= table_names
    if adopts_whole_model:
        stamp_revision = "head"
        tables_to_check = model_tables
        post_revision_columns: dict[str, set[str]] = {}
    else:
        incomplete_workspace_tables = WORKSPACE_TABLES & table_names
        if incomplete_workspace_tables:
            present = ", ".join(sorted(incomplete_workspace_tables))
            raise RuntimeError(
                "Cannot adopt existing unversioned database schema; workspace migration is "
                f"incomplete (found: {present})."
            )

        present_webhook_tables = LEGACY_WEBHOOK_TABLES & table_names
        if present_webhook_tables and present_webhook_tables != LEGACY_WEBHOOK_TABLES:
            present = ", ".join(sorted(present_webhook_tables))
            raise RuntimeError(
                "Cannot adopt existing unversioned database schema; webhook migration is "
                f"incomplete (found: {present})."
            )

        if present_webhook_tables == LEGACY_WEBHOOK_TABLES:
            tables_to_check = LEGACY_MODEL_TABLES | LEGACY_WEBHOOK_TABLES
            if "deleted_at" in columns_by_table.get("documents", set()):
                stamp_revision = "0004_document_deletion"
            else:
                stamp_revision = "0003_webhooks"
        else:
            if "deleted_at" in columns_by_table.get("documents", set()):
                raise RuntimeError(
                    "Cannot adopt existing unversioned database schema; documents.deleted_at "
                    "exists but the earlier webhook tables are missing."
                )
            stamp_revision = LEGACY_SCHEMA_REVISION
            tables_to_check = LEGACY_MODEL_TABLES
        post_revision_columns = POST_REVISION_COLUMNS[stamp_revision]

    model_columns = _model_columns_by_table()
    missing_columns = {
        table_name: model_columns[table_name] - columns_by_table.get(table_name, set())
        for table_name in tables_to_check
    }
    if not adopts_whole_model:
        # Later migrations still have to run after the detected structural revision.
        missing_columns = {
            table_name: columns - post_revision_columns.get(table_name, set())
            for table_name, columns in missing_columns.items()
        }
    missing_columns = {table: columns for table, columns in missing_columns.items() if columns}
    unsupported_missing_columns = {
        table: columns - ADOPTABLE_LEGACY_COLUMNS.get(table, set())
        for table, columns in missing_columns.items()
    }
    unsupported_missing_columns = {
        table: columns for table, columns in unsupported_missing_columns.items() if columns
    }
    if unsupported_missing_columns:
        raise RuntimeError(
            "Cannot adopt existing unversioned database schema; missing unexpected model columns: "
            f"{_format_schema_items(unsupported_missing_columns)}."
        )

    has_queue_job_column = "queue_job_id" in columns_by_table.get(
        "jobs", set()
    ) or "queue_job_id" in missing_columns.get(
        "jobs", set()
    )
    missing_indexes: set[str] = set()
    if has_queue_job_column:
        if "ix_jobs_queue_job_id" not in indexes_by_table.get("jobs", set()):
            missing_indexes.add("ix_jobs_queue_job_id")

    return LegacySchemaPatch(
        missing_columns=missing_columns,
        missing_indexes=missing_indexes,
        stamp_revision=stamp_revision,
    )


def _inspect_legacy_schema(connection: Connection) -> LegacySchemaPatch | None:
    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())
    model_tables = _model_table_names()
    columns_by_table = {
        table_name: {column["name"] for column in inspector.get_columns(table_name)}
        for table_name in model_tables & table_names
    }
    indexes_by_table = {
        table_name: {
            index["name"] for index in inspector.get_indexes(table_name) if index.get("name")
        }
        for table_name in model_tables & table_names
    }
    return _legacy_schema_patch(table_names, columns_by_table, indexes_by_table)


def _apply_legacy_schema_patch(connection: Connection, patch: LegacySchemaPatch) -> None:
    for table_name, columns in sorted(patch.missing_columns.items()):
        for column_name in sorted(columns):
            ddl = LEGACY_COLUMN_DDL.get((table_name, column_name))
            if ddl is None:
                raise RuntimeError(
                    "Cannot adopt existing unversioned database schema; no DDL is defined for "
                    f"{table_name}.{column_name}."
                )
            connection.execute(text(ddl))

    for index_name in sorted(patch.missing_indexes):
        connection.execute(text(LEGACY_INDEX_DDL[index_name]))


def _adopt_legacy_schema(command: object, config: object) -> bool:
    with engine.begin() as connection:
        patch = _inspect_legacy_schema(connection)
        if patch is None:
            return False
        _apply_legacy_schema_patch(connection, patch)

    command.stamp(config, patch.stamp_revision)
    return True


def run_migrations() -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(ALEMBIC_CONFIG_PATH))
    if _adopt_legacy_schema(command, config):
        command.upgrade(config, "head")
        return
    command.upgrade(config, "head")


def create_schema() -> None:
    if ALEMBIC_CONFIG_PATH.exists():
        run_migrations()
    else:
        Base.metadata.create_all(bind=engine)


def init_db(*, attempts: int = 30, delay_seconds: float = 1.0) -> None:
    import app.models  # noqa: F401

    for attempt in range(1, attempts + 1):
        try:
            create_schema()
            from app.services.auth import ensure_bootstrap_identity

            ensure_bootstrap_identity(get_settings())
            return
        except OperationalError:
            if attempt == attempts:
                raise
            time.sleep(delay_seconds)


def get_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
