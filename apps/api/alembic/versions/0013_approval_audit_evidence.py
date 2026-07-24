"""approval audit membership evidence (#420, ADR-0010)

Adds approval_audit_entries.evidence: the membership facts the authorizer
decided on, so the trail answers "why did this actor count" and not merely
"which implementation refused them". Nullable and additive: writers that make no
membership decision (the expiry sweeper) leave it NULL, and rows written before
this column read back as NULL rather than being backfilled with a verdict
nobody actually made.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "curie"


def upgrade() -> None:
    op.add_column(
        "approval_audit_entries",
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_column("approval_audit_entries", "evidence", schema=SCHEMA)
