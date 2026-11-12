"""folder attempts

Revision ID: 2d3e4f5a6b78
Revises: 1c2d3e4f5a67
Create Date: 2026-11-12 05:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "2d3e4f5a6b78"
down_revision: str | None = "1c2d3e4f5a67"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "download_folders",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("download_folders", "attempts")
