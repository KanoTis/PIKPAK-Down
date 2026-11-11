"""lineage traced items

Revision ID: 9a7c1e4d2b60
Revises: 54e3005887e8
Create Date: 2026-11-11 12:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "9a7c1e4d2b60"
down_revision: str | None = "54e3005887e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "lineage_graph_items",
        sa.Column("traced", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("lineage_graph_items", "traced")
