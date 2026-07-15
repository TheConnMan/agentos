"""approval resumed_at work-list marker (#411, ADR-0010)

Adds approvals.resumed_at: set once the resume turn is enqueued onto the runs
stream, NULL on a resolved record means the wake is still owed (the resume
reconciler's work-list). Backfills pre-existing resolved rows so the first
reconciler pass after deploy treats settled history as delivered, never
re-waking long-finished threads whose worker done-marker has expired.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "agentos"


def upgrade() -> None:
    op.add_column(
        "approvals",
        sa.Column("resumed_at", sa.DateTime(), nullable=True),
        schema=SCHEMA,
    )
    # Windowed backfill: settle ONLY rows resolved longer ago than the worker's
    # done-marker TTL window (24h). Rows resolved within the last 24h stay NULL so
    # they remain recoverable -- within the live done-marker window a successful
    # delivery still dedupes a reconciler re-enqueue, while a failed (stranded)
    # delivery, the actual bug victim, is finally recovered by the reconciler. A
    # blanket backfill would settle those stranded-just-before-deploy rows too and
    # the reconciler could never recover them. Only ambiguous older-than-TTL
    # history is settled, to avoid re-delivering into long-finished threads whose
    # marker has already expired. The 24h literal mirrors the worker
    # ``idempotency_ttl_s`` DEFAULT (86400s). A migration cannot read the worker's
    # runtime env, so this is a known coupling: a deployment that overrides
    # ``IDEMPOTENCY_TTL_S`` must adjust this window to match (a longer TTL would
    # leave some safely-dedupable rows NULL for one redundant re-enqueue; a shorter
    # TTL risks re-waking a thread whose marker expired between the window and the
    # true TTL). Both are one-time, deploy-only effects; change the two together.
    op.execute(
        "UPDATE agentos.approvals SET resumed_at = resolved_at "
        "WHERE status IN ('approved','rejected') AND resolved_at IS NOT NULL "
        "AND resolved_at < now() - interval '24 hours'"
    )
    # Partial index for the reconciler's work-list query, which runs every
    # interval and filters resumed_at IS NULL + resolved_at. In steady state
    # nearly every historical row has resumed_at set, so a partial index keeps
    # the sweep proportional to the handful of owed wakes rather than scanning
    # all resolved history.
    op.create_index(
        "ix_approvals_resolved_unresumed",
        "approvals",
        ["resolved_at"],
        schema=SCHEMA,
        postgresql_where=sa.text("resumed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_approvals_resolved_unresumed", "approvals", schema=SCHEMA)
    op.drop_column("approvals", "resumed_at", schema=SCHEMA)
