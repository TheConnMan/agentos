"""add approvals (durable human-in-the-loop approval records, #22 / ADR-0010)

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agentos"


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("reply_placeholder", sa.String(), nullable=False),
        sa.Column("reply_endpoint", sa.String(), nullable=True),
        sa.Column("tool", sa.String(), nullable=False),
        sa.Column("tool_use_id", sa.String(), nullable=False),
        sa.Column("input_digest", sa.String(), nullable=False),
        sa.Column("prompt", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column("resolved_by", sa.String(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resumed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], [f"{SCHEMA}.agents.id"], ondelete="CASCADE"
        ),
        # Idempotent re-suspend: a reclaimed turn that re-emits the same gate for
        # the same tool call must not create a second pending record.
        sa.UniqueConstraint(
            "agent_id",
            "conversation_id",
            "tool_use_id",
            name="uq_approval_agent_conv_tooluse",
        ),
        schema=SCHEMA,
    )
    op.create_index("ix_approvals_agent_id", "approvals", ["agent_id"], schema=SCHEMA)
    # The reconcile sweep filters resolved-but-not-resumed records by status.
    op.create_index("ix_approvals_status", "approvals", ["status"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_table("approvals", schema=SCHEMA)
