"""clear stale restore timestamps

Revision ID: 0b5f7c9d1e23
Revises: 9a7c1e4d2b60
Create Date: 2026-11-12 04:06:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "0b5f7c9d1e23"
down_revision: str | None = "9a7c1e4d2b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Handoff success used to leave restore_started_at set, which then pinned
    # every later retry to the same account. Only an in-flight RESTORING task
    # actually needs this marker.
    op.execute(
        "UPDATE download_tasks SET restore_started_at = NULL "
        "WHERE restore_started_at IS NOT NULL AND status != 'RESTORING'"
    )


def downgrade() -> None:
    pass
