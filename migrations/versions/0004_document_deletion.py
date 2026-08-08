"""Record when a document's stored bytes were purged.

Revision ID: 0004_document_deletion
Revises: 0003_webhooks
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004_document_deletion"
down_revision: str | None = "0003_webhooks"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "deleted_at")
