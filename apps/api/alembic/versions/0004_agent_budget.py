"""add per-agent budget columns

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agentos"


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("max_usd_per_day", sa.Float(), nullable=True),
        schema=SCHEMA,
    )
    op.add_column(
        "agents",
        sa.Column("max_output_tokens_per_run", sa.Integer(), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("agents", "max_output_tokens_per_run", schema=SCHEMA)
    op.drop_column("agents", "max_usd_per_day", schema=SCHEMA)
