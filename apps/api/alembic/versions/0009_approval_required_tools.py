"""add agents.approval_required_tools (per-agent permission gates, #245)

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agentos"


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column("approval_required_tools", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("agents", "approval_required_tools", schema=SCHEMA)
