"""Resume reconciler (#411): the backstop that re-enqueues owed wakes.

The resolve endpoint commits the resolve-once CAS claim and audit row, then
enqueues the resume turn. If that enqueue fails (a Valkey blip), the record is
left ``resolved_at`` set but ``resumed_at`` NULL -- a stranded suspended
session. ``ResumeReconciler`` sweeps those rows on an interval and re-enqueues
the resume turn, setting ``resumed_at`` only AFTER a successful enqueue
(enqueue-first-then-mark), so a failed enqueue is retried on the next pass
rather than lost.

Three qualifications shape the design:

- **Grace window (load-bearing).** ``reconcile_once`` only considers records
  resolved at least ``grace_seconds`` ago. This is NOT approximate: the grace
  must exceed the worker's maximum single-turn processing time
  (``runner_total_timeout_s``, 600s) so the reconciler never re-enqueues while an
  inline-delivered resume turn is still live. The worker writes its done-marker
  only post-terminal, so a duplicate landing mid-turn is steered into that live
  turn and re-runs the approved action -- a too-small grace re-introduces exactly
  that. The two clocks it compares (``resolved_at`` is the DB ``func.now()``,
  ``resolved_before`` is this pod's clock) only add a small skew margin on top of
  a large grace, not a correctness dependency.
- **Absorption is TTL-bounded.** A duplicate enqueue is safe because the resume
  turn's ``event_id`` is deterministic per approval and the worker's done-marker
  dedupes it -- but only within ``idempotency_ttl_s`` (default 24h). In steady
  state the reconciler retries on the interval (seconds), far inside that
  window. The bound only bites for pre-fix historical rows, which the migration
  0011 backfill (``resumed_at = resolved_at``) excludes from the work-list.
- **Concurrency.** The done-marker CANNOT dedupe a concurrent double-enqueue
  (it is written only post-terminal), so two overlapping copies steer into one
  live turn. Two races, two guards: (1) *reconciler vs reconciler* (``api.replicas
  > 1``) -- a per-row ``SELECT ... FOR UPDATE SKIP LOCKED`` claim locks each
  candidate in its own short transaction, so two replicas never grab the same
  row; (2) *inline resolver vs reconciler* -- the grace above outlasts the max
  worker turn, so an inline-delivered turn is terminal (done-marked) before the
  reconciler would re-enqueue. Residual, NOT unconditional exactly-once: a worker
  retry loop can keep a turn live past the grace after an inline mark-failure; a
  fully airtight guarantee needs a worker-side in-flight lease (follow-up). The
  done-marker still dedupes strictly-sequential redeliveries within the 24h TTL.
  No leader election.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from . import crud
from .resumequeue import ResumeQueue, build_resume_turn

logger = logging.getLogger(__name__)


class ResumeReconciler:
    """Periodically re-enqueue resume turns for resolved-but-unresumed approvals."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        resume_queue: ResumeQueue,
        *,
        interval_seconds: int,
        grace_seconds: int,
        batch_limit: int,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._resume_queue = resume_queue
        self._interval_seconds = interval_seconds
        self._grace_seconds = grace_seconds
        self._batch_limit = batch_limit

    async def reconcile_once(self) -> int:
        """Re-enqueue every owed wake past the grace horizon; return the count.

        Candidates are read once (unlocked) past the grace horizon; then each is
        claimed atomically in its OWN short transaction via ``claim_resume_row``
        (``SELECT ... FOR UPDATE SKIP LOCKED``). A row a concurrent replica
        already holds is skipped this pass and retried next -- two replicas never
        both enqueue one record. Per-record failure is isolated: the enqueue and
        mark live inside ``session.begin()``, so a single-record Valkey blip
        rolls that row's transaction back (``resumed_at`` stays NULL for the next
        pass, preserving enqueue-first-then-mark durability) without aborting the
        batch. The row lock is held only for that one record's brief enqueue+mark,
        never across the whole batch.
        """

        resolved_before = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            seconds=self._grace_seconds
        )
        async with self._sessionmaker() as session:
            candidate_ids = await crud.list_resolved_unresumed(
                session, resolved_before=resolved_before, limit=self._batch_limit
            )

        count = 0
        for approval_id in candidate_ids:
            async with self._sessionmaker() as session:
                try:
                    async with session.begin():
                        approval = await crud.claim_resume_row(session, approval_id)
                        if approval is None:
                            # Another replica holds it, or it is already resumed;
                            # exit the txn block cleanly, releasing any lock.
                            continue
                        turn = build_resume_turn(approval)
                        await self._resume_queue.enqueue(turn)
                        approval.resumed_at = datetime.now(UTC).replace(tzinfo=None)
                    # session.begin() committed here, releasing the row lock.
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "approval %s resume re-enqueue failed, will retry next pass",
                        approval_id,
                        exc_info=True,
                    )
                    continue
                count += 1
                logger.info("approval %s resume turn re-enqueued", approval_id)
        return count

    async def run_forever(self) -> None:
        """Reconcile on the interval forever; never die from a reconcile error.

        The loop sleeps before each pass (including the first): the inline path
        handles the common case, so a just-started pod need not sweep instantly,
        and delaying the first pass keeps the backstop from firing inside a
        sub-interval process lifetime.
        """

        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("resume reconciler pass failed")
