"""add per-agent model column

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agentos"


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("model", sa.String(), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("agents", "model", schema=SCHEMA)
