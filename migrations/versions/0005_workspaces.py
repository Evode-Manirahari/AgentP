"""Add workspace ownership and revocable API keys.

Revision ID: 0005_workspaces
Revises: 0004_document_deletion
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005_workspaces"
down_revision: str | None = "0004_document_deletion"
branch_labels: str | None = None
depends_on: str | None = None

DEFAULT_WORKSPACE_ID = "ws_default"
OWNED_TABLES = (
    "jobs",
    "documents",
    "audit_events",
    "webhook_endpoints",
    "webhook_deliveries",
)


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        sa.text("INSERT INTO workspaces (id, name) VALUES (:id, :name)").bindparams(
            id=DEFAULT_WORKSPACE_ID,
            name="Default Workspace",
        )
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=32), nullable=False),
        sa.Column(
            "is_platform_admin",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_api_keys_token_hash"),
    )
    op.create_index(
        "ix_api_keys_workspace_revoked",
        "api_keys",
        ["workspace_id", "revoked_at"],
    )

    for table_name in OWNED_TABLES:
        op.add_column(
            table_name,
            sa.Column("workspace_id", sa.String(length=64), nullable=True),
        )
        op.execute(
            sa.text(f"UPDATE {table_name} SET workspace_id = :workspace_id").bindparams(
                workspace_id=DEFAULT_WORKSPACE_ID
            )
        )
        op.alter_column(table_name, "workspace_id", nullable=False)
        op.create_foreign_key(
            f"fk_{table_name}_workspace_id",
            table_name,
            "workspaces",
            ["workspace_id"],
            ["id"],
        )

    op.drop_constraint("uq_jobs_idempotency_key", "jobs", type_="unique")
    op.drop_index("ix_jobs_idempotency_key", table_name="jobs")
    op.create_unique_constraint(
        "uq_jobs_workspace_idempotency_key",
        "jobs",
        ["workspace_id", "idempotency_key"],
    )
    op.create_index(
        "ix_jobs_workspace_status_created",
        "jobs",
        ["workspace_id", "status", "created_at"],
    )
    op.create_index(
        "ix_documents_workspace_created",
        "documents",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_audit_events_workspace_created",
        "audit_events",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_webhook_endpoints_workspace_active",
        "webhook_endpoints",
        ["workspace_id", "active"],
    )
    op.create_index(
        "ix_webhook_deliveries_workspace_created",
        "webhook_deliveries",
        ["workspace_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_webhook_deliveries_workspace_created",
        table_name="webhook_deliveries",
    )
    op.drop_index(
        "ix_webhook_endpoints_workspace_active",
        table_name="webhook_endpoints",
    )
    op.drop_index("ix_audit_events_workspace_created", table_name="audit_events")
    op.drop_index("ix_documents_workspace_created", table_name="documents")
    op.drop_index("ix_jobs_workspace_status_created", table_name="jobs")
    op.drop_constraint("uq_jobs_workspace_idempotency_key", "jobs", type_="unique")
    op.create_index("ix_jobs_idempotency_key", "jobs", ["idempotency_key"])
    op.create_unique_constraint(
        "uq_jobs_idempotency_key",
        "jobs",
        ["idempotency_key"],
    )

    for table_name in reversed(OWNED_TABLES):
        op.drop_constraint(
            f"fk_{table_name}_workspace_id",
            table_name,
            type_="foreignkey",
        )
        op.drop_column(table_name, "workspace_id")

    op.drop_index("ix_api_keys_workspace_revoked", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_table("workspaces")
