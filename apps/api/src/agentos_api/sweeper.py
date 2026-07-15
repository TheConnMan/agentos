"""Approval SLA expiry sweeper (#412): resume the session an expiry stranded.

A pending approval whose ``expires_at`` lapses used to dead-end the suspended
session. The only prior expiry path lived inside the resolve endpoint (#244,
ADR-0010): a late resolver flipped the record to ``expired`` (410) and enqueued
NOTHING, so if no resolver ever arrived the session waited forever. This module
closes that gap with a periodic sweeper that (1) flips lapsed ``pending``
approvals to ``expired`` via the existing compare-and-set, (2) appends an
``expired`` audit row consistent with #247, and (3) enqueues a platform-authored
resume turn onto the same runs stream the resolve path uses, so the suspended
session resumes down its timeout branch (ADR-0003) and the channel placeholder
updates.

Concurrency and idempotency (why unattended sweeping is safe): the flip is
``crud.expire_approval``'s conditional UPDATE guarded on ``status = pending``, so
for one record exactly one writer wins -- one replica's sweeper, or a racing
resolver -- and every loser gets None back and neither audits nor enqueues. This
pending-guarded CAS is what guarantees a single wakeup: only the flip winner
ever enqueues.

Warning for anyone adding a re-enqueue path (retry, durable outbox, reconciler):
the worker's done-marker (``markers.py``) only skips an event that was already
handled to a TERMINAL point (streamed to a final, or escalated). It does NOT
collapse a duplicate that lands while the resumed turn is still in flight --
that duplicate would steer the live turn instead of being absorbed. The shared
``resume_event_id`` (see ``resumequeue.build_expiry_resume_turn``) prevents a
redelivery of an already-finished turn from re-running; it does not make a
re-enqueue free, so do not rely on it as a mid-turn dedupe.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import crud
from .resumequeue import ResumeQueue, build_expiry_resume_turn

logger = logging.getLogger(__name__)


async def sweep_expired_approvals(
    session: AsyncSession,
    resume_queue: ResumeQueue,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> int:
    """One sweep pass: expire every lapsed pending approval and wake its session.

    ``now`` defaults to naive UTC (matching ``expires_at`` and the router's
    ``_expired`` comparison); tests pass an explicit ``now`` to avoid real
    sleeps. Returns the number of records this pass successfully flipped.

    Per record, the order is flip -> audit -> enqueue, each guarded by a
    per-record try/except so one poisoned record cannot block the rest of the
    batch. Flip-first makes the DB the single arbiter of the flip/resolve race:
    only a successful CAS (``expire_approval`` returning the record) audits and
    enqueues; a None means a concurrent resolver or another replica's sweeper
    already claimed it, so this pass skips it silently.
    """

    now = now or datetime.now(UTC).replace(tzinfo=None)
    lapsed = await crud.list_expired_pending_approvals(session, now=now, limit=limit)
    # Read the ids into plain values up front: the per-record rollback below
    # expires every ORM instance in this shared session, so reading record.id
    # lazily in a later iteration would trigger an implicit reload (MissingGreenlet
    # under the async session).
    approval_ids = [record.id for record in lapsed]

    flipped = 0
    for approval_id in approval_ids:
        try:
            expired = await crud.expire_approval(session, approval_id)
            if expired is None:
                # A concurrent resolver or another replica's sweeper won the CAS;
                # the winner owns the audit and enqueue. No side effects here.
                continue
            await crud.append_approval_audit(
                session,
                approval_id=expired.id,
                action="expired",
                actor="system",
                actor_channel=None,
                decision="",
                authorizer="ExpirySweeper",
                authorized=True,
                reason=f"approval expired at {expired.expires_at}",
            )
            stream_id = await resume_queue.enqueue(build_expiry_resume_turn(expired))
            flipped += 1
            logger.info(
                "approval %s expired by sweeper; resume turn enqueued (%s)",
                expired.id,
                stream_id,
            )
        except Exception:
            # Reset the shared session so a failed commit on this record cannot
            # poison the rest of the batch (PendingRollbackError); mirrors the
            # rollback-after-DB-error convention in the routers. If the flip
            # already committed, the audit/enqueue loss means the wakeup may be
            # dropped (documented durability gap; a durable outbox is future work).
            await session.rollback()
            logger.exception(
                "expiry sweep failed for approval %s; wakeup may be lost", approval_id
            )
            continue
    return flipped


async def run_expiry_sweeper(
    sessionmaker: async_sessionmaker[AsyncSession],
    resume_queue: ResumeQueue,
    interval_s: float,
    stop: asyncio.Event,
) -> None:
    """Periodic loop driving ``sweep_expired_approvals`` until ``stop`` is set.

    Mirrors the worker heartbeat's sleep-or-stop shape (an
    ``asyncio.wait_for(stop.wait(), timeout=interval_s)`` that wakes early on
    shutdown) but INVERTS it to wait-FIRST: no sweep at t=0. That is deliberate,
    both boot hygiene (no DB query racing app startup) and a test-safety
    guarantee -- with a double-digit-second interval, no sweep fires inside any
    integration test's window, so the sweeper cannot leak into the frozen
    resolve-path expiry contract.

    A maintenance loop must never take down the API, so each pass runs inside a
    broad try/except: a failed pass (DB down, etc.) is logged and retried next
    interval rather than crashing the process.
    """

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except TimeoutError:
            pass
        if stop.is_set():
            break
        try:
            async with sessionmaker() as session:
                await sweep_expired_approvals(session, resume_queue)
        except Exception:
            logger.exception("expiry sweep pass failed; retrying next interval")
