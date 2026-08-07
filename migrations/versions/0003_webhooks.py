"""webhooks

Revision ID: 0003_webhooks
Revises: 0002_job_queue_id
Create Date: 2026-08-06 00:00:02.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_webhooks"
down_revision: str | None = "0002_job_queue_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "webhook_endpoints",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("secret", sa.String(length=128), nullable=False),
        sa.Column("events", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_webhook_endpoints_active", "webhook_endpoints", ["active"])

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("endpoint_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["endpoint_id"], ["webhook_endpoints.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_webhook_deliveries_endpoint_created",
        "webhook_deliveries",
        ["endpoint_id", "created_at"],
    )
    op.create_index(
        "ix_webhook_deliveries_job_created",
        "webhook_deliveries",
        ["job_id", "created_at"],
    )
    op.create_index(
        "ix_webhook_deliveries_status_created",
        "webhook_deliveries",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_status_created", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_job_created", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_endpoint_created", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_webhook_endpoints_active", table_name="webhook_endpoints")
    op.drop_table("webhook_endpoints")
