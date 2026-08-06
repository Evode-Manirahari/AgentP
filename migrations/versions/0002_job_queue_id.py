"""job queue id

Revision ID: 0002_job_queue_id
Revises: 0001_initial_schema
Create Date: 2026-08-06 00:00:01.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_job_queue_id"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("queue_job_id", sa.String(length=128), nullable=True))
    op.create_index("ix_jobs_queue_job_id", "jobs", ["queue_job_id"])


def downgrade() -> None:
    op.drop_index("ix_jobs_queue_job_id", table_name="jobs")
    op.drop_column("jobs", "queue_job_id")
