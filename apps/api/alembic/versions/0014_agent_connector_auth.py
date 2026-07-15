"""add agents.secrets (per-agent connector secrets, ADR-0009 / #429)

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agentos"


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("secrets", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("agents", "secrets", schema=SCHEMA)
