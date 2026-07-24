"""settle historical expired approvals (#418, ADR-0010)

Data-only companion to 0011. #418 adds ``expired`` to the resume reconciler's
work-list, which makes every pre-existing expired row a candidate for the first
pass after deploy. This settles the ambiguous older-than-TTL ones, on the same
window and for the same reason 0011 settled the resolved rows.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Every expired row predating #418 has resolved_at set (expire_approval
    # stamps it in the same CAS) and resumed_at NULL, because nothing marked the
    # expiry paths' enqueues until now. 0011 left them NULL harmlessly: under the
    # #416 contract an expired row was never a reconciler candidate. Widening
    # _RESUMABLE_STATUSES makes them all candidates at once, so without this
    # backfill the first pass after deploy would enqueue an expiry wake for the
    # whole history.
    #
    # Windowed, not blanket, exactly as in 0011: settle ONLY rows expired longer
    # ago than the worker's done-marker TTL window (24h). Past that window the
    # marker has expired, so a re-enqueued wake is NOT deduped -- it re-delivers
    # into a long-finished thread and steers it down a timeout branch it already
    # took. Rows expired within the last 24h stay NULL because they are the
    # recoverable bug victims: if their enqueue succeeded the live done-marker
    # absorbs the reconciler's one redundant re-enqueue, and if it failed (the
    # #418 bug) the wake is finally delivered. A blanket settle would write off
    # precisely those stranded-just-before-deploy sessions this fix exists to
    # recover.
    #
    # The 24h literal mirrors the worker ``idempotency_ttl_s`` DEFAULT (86400s);
    # see 0011's comment for the full statement of that coupling. A migration
    # cannot read the worker's runtime env, so a deployment overriding
    # ``IDEMPOTENCY_TTL_S`` must adjust both windows together.
    #
    # The ``resumed_at IS NULL`` guard (absent from 0011 only because 0011
    # created the column in the same revision) makes this idempotent and keeps an
    # out-of-band re-run from overwriting rows the new runtime paths marked.
    op.execute(
        "UPDATE curie.approvals SET resumed_at = resolved_at "
        "WHERE status = 'expired' AND resolved_at IS NOT NULL "
        "AND resumed_at IS NULL "
        "AND resolved_at < now() - interval '24 hours'"
    )
    # No index work: 0011's ix_approvals_resolved_unresumed is keyed on
    # resolved_at with a resumed_at IS NULL predicate and is status-agnostic, so
    # it already serves the widened status IN (...) work-list query.


def downgrade() -> None:
    # Deliberate no-op. There is no schema element to drop, and settled history
    # cannot be selectively un-settled: this migration's writes are
    # indistinguishable from the marks the runtime expiry paths make, so
    # reverting them would misclassify genuinely-delivered wakes as owed.
    pass
