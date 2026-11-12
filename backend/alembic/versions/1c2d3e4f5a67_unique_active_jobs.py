"""unique active jobs

Revision ID: 1c2d3e4f5a67
Revises: 0b5f7c9d1e23
Create Date: 2026-11-12 05:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "1c2d3e4f5a67"
down_revision: str | None = "0b5f7c9d1e23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Keep the newest active job per share/target before enforcing uniqueness.
    op.execute(
        """
        UPDATE download_jobs
        SET status = 'CANCELLED',
            finished_at = COALESCE(finished_at, CURRENT_TIMESTAMP)
        WHERE status IN ('ENUMERATING', 'READY')
          AND id NOT IN (
              SELECT keep_id FROM (
                  SELECT id AS keep_id,
                         ROW_NUMBER() OVER (
                             PARTITION BY share_id, target_subdir
                             ORDER BY created_at DESC, id DESC
                         ) AS rn
                  FROM download_jobs
                  WHERE status IN ('ENUMERATING', 'READY')
              ) AS ranked
              WHERE rn = 1
          )
        """
    )
    op.create_index(
        "uq_download_job_active_share_target",
        "download_jobs",
        ["share_id", "target_subdir"],
        unique=True,
        sqlite_where=sa.text("status IN ('ENUMERATING', 'READY')"),
        postgresql_where=sa.text("status IN ('ENUMERATING', 'READY')"),
    )


def downgrade() -> None:
    op.drop_index("uq_download_job_active_share_target", table_name="download_jobs")
