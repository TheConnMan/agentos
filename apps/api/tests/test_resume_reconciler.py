"""Resume reconciler (#411): the backstop that re-enqueues owed wakes.

A resolved approval whose resume enqueue failed is left ``resolved_at`` set but
``resumed_at`` NULL -- a stranded suspended session. ``ResumeReconciler`` sweeps
those rows on an interval and re-enqueues the resume turn onto the runs stream,
setting ``resumed_at`` only AFTER a successful enqueue (enqueue-first-then-mark),
so a failed enqueue is retried on the next pass rather than lost.

Real Postgres + real Valkey from the compose dev stack -- never mocked; the only
injected failure is a wrapped ``enqueue`` that raises for a chosen record. Every
assertion is on a real outcome: the deterministic ``event_id`` present/absent on
the actual stream, the ``resumed_at`` NULL<->set transition read back from the
DB, and ``reconcile_once()``'s return count.

The ``runs_stream``/``valkey`` fixtures are duplicated from ``test_approvals.py``
on purpose: pytest fixtures are module-local, and copying them keeps that file's
definitions untouched (true verbatim reuse of behavior) without hoisting
Valkey-specific setup into the shared conftest.
"""

import asyncio
import json
import os
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest
import redis
import redis.asyncio as aioredis
from aci_protocol import QueuedTurn
from agentos_api.config import get_settings
from agentos_api.models import Approval, ApprovalStatus
from agentos_api.resumequeue import ResumeQueue
from agentos_api.resumereconciler import ResumeReconciler
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_VALKEY_HOST = os.environ.get("TEST_VALKEY_HOST", "localhost")
_VALKEY_PORT = int(os.environ.get("TEST_VALKEY_PORT", "26379"))
_VALKEY_PW = os.environ.get("TEST_VALKEY_PW", "valkeypass")


@pytest.fixture
def runs_stream() -> Iterator[str]:
    """A per-test runs stream so reconciler enqueues never feed the shared
    compose worker's real ``agentos:runs`` consumer group."""

    name = f"test:agentos:runs:{uuid.uuid4().hex}"
    os.environ["RUNS_STREAM"] = name
    get_settings.cache_clear()
    yield name
    os.environ.pop("RUNS_STREAM", None)
    get_settings.cache_clear()


@pytest.fixture
def valkey(runs_stream: str) -> Iterator[redis.Redis]:
    client = redis.Redis(
        host=_VALKEY_HOST,
        port=_VALKEY_PORT,
        password=_VALKEY_PW or None,
        decode_responses=True,
    )
    try:
        client.ping()
    except redis.exceptions.RedisError as exc:
        pytest.skip(f"Valkey not reachable: {exc}")
    yield client
    client.delete(runs_stream)
    client.close()


def _naive(seconds_ago: float = 0.0) -> datetime:
    """Naive UTC ``now - seconds_ago``, matching the DateTime columns."""

    return datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=seconds_ago)


def _run_async[T](
    steps: Callable[[async_sessionmaker[AsyncSession], ResumeQueue], Awaitable[T]],
    stream: str,
) -> T:
    """Drive an async ``steps(sessionmaker, queue)`` with fresh loop-bound
    resources. A fresh engine + async Valkey client (not the app's, which are
    bound to the TestClient portal loop) makes ``asyncio.run`` from the sync
    test body safe -- the same pattern conftest's ``_truncate`` uses."""

    async def _main() -> T:
        engine = create_async_engine(get_settings().database_url)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        client = aioredis.Redis(
            host=_VALKEY_HOST, port=_VALKEY_PORT, password=_VALKEY_PW or None
        )
        queue = ResumeQueue(client, stream=stream)
        try:
            return await steps(sessionmaker, queue)
        finally:
            await client.aclose()
            await engine.dispose()

    return asyncio.run(_main())


async def _insert_approval(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    status: str,
    resolved_at: datetime | None,
    resumed_at: datetime | None,
    resolved_by: str | None = "U9",
    reply_endpoint: str | None = None,
) -> uuid.UUID:
    """Insert an approval row in a chosen lifecycle state (bypassing the resolve
    endpoint), so a test can construct the exact stranded/settled/expired shapes
    the reconciler must include or exclude."""

    async with sessionmaker() as session:
        approval = Approval(
            conversation_id=f"th-{uuid.uuid4().hex[:8]}",
            author="U1",
            summary="Give ACME a 20% discount",
            reply_channel="C1",
            reply_placeholder="p-1",
            reply_endpoint=reply_endpoint,
            dedupe_key=uuid.uuid4().hex,
            status=status,
            resolved_by=resolved_by,
            resolved_at=resolved_at,
            resumed_at=resumed_at,
        )
        session.add(approval)
        await session.commit()
        await session.refresh(approval)
        return approval.id


async def _resumed_at(
    sessionmaker: async_sessionmaker[AsyncSession], approval_id: uuid.UUID
) -> datetime | None:
    async with sessionmaker() as session:
        approval = await session.get(Approval, approval_id)
        assert approval is not None
        return approval.resumed_at


def test_reconciler_reenqueues_stranded_resolved_record(
    clean_db: None, valkey: redis.Redis, runs_stream: str
) -> None:
    """A resolved, past-grace, unresumed record is re-enqueued exactly once and
    marked resumed. This is the core AC1 outcome."""

    async def steps(
        sessionmaker: async_sessionmaker[AsyncSession], queue: ResumeQueue
    ) -> tuple[uuid.UUID, int, datetime | None]:
        approval_id = await _insert_approval(
            sessionmaker,
            status=ApprovalStatus.approved,
            resolved_at=_naive(120),
            resumed_at=None,
        )
        reconciler = ResumeReconciler(
            sessionmaker,
            queue,
            interval_seconds=30,
            grace_seconds=0,
            batch_limit=100,
        )
        count = await reconciler.reconcile_once()
        return approval_id, count, await _resumed_at(sessionmaker, approval_id)

    approval_id, count, resumed_at = _run_async(steps, runs_stream)

    assert count == 1
    entries = valkey.xrange(runs_stream)
    assert len(entries) == 1
    turn = QueuedTurn.model_validate(json.loads(entries[0][1]["payload"]))
    assert turn.event_id == f"approval-{approval_id}-resolved"
    assert resumed_at is not None


def test_reconciler_skips_already_resumed_record(
    clean_db: None, valkey: redis.Redis, runs_stream: str
) -> None:
    """A record whose wake was already delivered (``resumed_at`` set) is off the
    work-list -- no re-enqueue, no double wake."""

    async def steps(
        sessionmaker: async_sessionmaker[AsyncSession], queue: ResumeQueue
    ) -> tuple[int, datetime | None]:
        resolved = _naive(120)
        approval_id = await _insert_approval(
            sessionmaker,
            status=ApprovalStatus.approved,
            resolved_at=resolved,
            resumed_at=resolved,
        )
        reconciler = ResumeReconciler(
            sessionmaker, queue, interval_seconds=30, grace_seconds=0, batch_limit=100
        )
        count = await reconciler.reconcile_once()
        return count, await _resumed_at(sessionmaker, approval_id)

    count, resumed_at = _run_async(steps, runs_stream)

    assert count == 0
    assert valkey.xrange(runs_stream) == []
    assert resumed_at is not None


def test_reconciler_skips_expired_record(
    clean_db: None, valkey: redis.Redis, runs_stream: str
) -> None:
    """An expired record has ``resolved_at`` set but was never enqueued and must
    never be woken -- this pins the expired-exclusion in
    ``list_resolved_unresumed`` (out-of-scope expiry-stranding stays out)."""

    async def steps(
        sessionmaker: async_sessionmaker[AsyncSession], queue: ResumeQueue
    ) -> tuple[int, datetime | None]:
        approval_id = await _insert_approval(
            sessionmaker,
            status=ApprovalStatus.expired,
            resolved_at=_naive(120),
            resumed_at=None,
        )
        reconciler = ResumeReconciler(
            sessionmaker, queue, interval_seconds=30, grace_seconds=0, batch_limit=100
        )
        count = await reconciler.reconcile_once()
        return count, await _resumed_at(sessionmaker, approval_id)

    count, resumed_at = _run_async(steps, runs_stream)

    assert count == 0
    assert valkey.xrange(runs_stream) == []
    assert resumed_at is None


def test_reconciler_skips_pending_record(
    clean_db: None, valkey: redis.Redis, runs_stream: str
) -> None:
    """A still-pending record (no ``resolved_at``) is never on the work-list."""

    async def steps(
        sessionmaker: async_sessionmaker[AsyncSession], queue: ResumeQueue
    ) -> int:
        await _insert_approval(
            sessionmaker,
            status=ApprovalStatus.pending,
            resolved_at=None,
            resumed_at=None,
            resolved_by=None,
        )
        reconciler = ResumeReconciler(
            sessionmaker, queue, interval_seconds=30, grace_seconds=0, batch_limit=100
        )
        return await reconciler.reconcile_once()

    count = _run_async(steps, runs_stream)

    assert count == 0
    assert valkey.xrange(runs_stream) == []


def test_reconciler_ignores_backfilled_historical_row(
    clean_db: None, valkey: redis.Redis, runs_stream: str
) -> None:
    """A pre-migration resolution is backfilled to ``resumed_at = resolved_at``;
    the reconciler must treat it as settled and never auto-wake it after deploy.

    This pins the Change 1 backfill contract: without it, the first pass after
    deploy would re-deliver stale resume turns into long-finished threads (the
    worker done-marker's 24h TTL means an old wake is re-delivered, not absorbed).
    """

    async def steps(
        sessionmaker: async_sessionmaker[AsyncSession], queue: ResumeQueue
    ) -> int:
        resolved = _naive(7200)
        await _insert_approval(
            sessionmaker,
            status=ApprovalStatus.approved,
            resolved_at=resolved,
            resumed_at=resolved,
        )
        reconciler = ResumeReconciler(
            sessionmaker, queue, interval_seconds=30, grace_seconds=0, batch_limit=100
        )
        return await reconciler.reconcile_once()

    count = _run_async(steps, runs_stream)

    assert count == 0
    assert valkey.xrange(runs_stream) == []


def test_reconciler_respects_grace_window(
    clean_db: None, valkey: redis.Redis, runs_stream: str
) -> None:
    """A record resolved within the grace window is skipped (avoids racing the
    inline enqueue path); the same record, once past grace, is picked up."""

    async def steps(
        sessionmaker: async_sessionmaker[AsyncSession], queue: ResumeQueue
    ) -> tuple[int, int, datetime | None]:
        approval_id = await _insert_approval(
            sessionmaker,
            status=ApprovalStatus.approved,
            resolved_at=_naive(0),
            resumed_at=None,
        )
        within = ResumeReconciler(
            sessionmaker,
            queue,
            interval_seconds=30,
            grace_seconds=3600,
            batch_limit=100,
        )
        count_within = await within.reconcile_once()

        # Backdate the resolution well past the grace horizon.
        async with sessionmaker() as session:
            await session.execute(
                update(Approval)
                .where(Approval.id == approval_id)
                .values(resolved_at=_naive(7200))
            )
            await session.commit()

        past = ResumeReconciler(
            sessionmaker,
            queue,
            interval_seconds=30,
            grace_seconds=3600,
            batch_limit=100,
        )
        count_past = await past.reconcile_once()
        return count_within, count_past, await _resumed_at(sessionmaker, approval_id)

    count_within, count_past, resumed_at = _run_async(steps, runs_stream)

    assert count_within == 0
    assert count_past == 1
    assert len(valkey.xrange(runs_stream)) == 1
    assert resumed_at is not None


def test_reconciler_is_idempotent_across_runs(
    clean_db: None, valkey: redis.Redis, runs_stream: str
) -> None:
    """After one successful pass marks a record resumed, a second pass finds
    nothing to do and enqueues nothing new."""

    async def steps(
        sessionmaker: async_sessionmaker[AsyncSession], queue: ResumeQueue
    ) -> tuple[int, int]:
        await _insert_approval(
            sessionmaker,
            status=ApprovalStatus.approved,
            resolved_at=_naive(120),
            resumed_at=None,
        )
        reconciler = ResumeReconciler(
            sessionmaker, queue, interval_seconds=30, grace_seconds=0, batch_limit=100
        )
        first = await reconciler.reconcile_once()
        second = await reconciler.reconcile_once()
        return first, second

    first, second = _run_async(steps, runs_stream)

    assert first == 1
    assert second == 0
    assert len(valkey.xrange(runs_stream)) == 1


def test_reconciler_isolates_per_record_failure(
    clean_db: None, valkey: redis.Redis, runs_stream: str
) -> None:
    """One record's enqueue failure must not abort the batch nor mark that
    record resumed; the other record is still enqueued and marked.

    Marking-before-enqueue would leave the failing record marked -- this asserts
    the enqueue-first-then-mark ordering by requiring the failing record's
    ``resumed_at`` to stay NULL for the next pass.
    """

    async def steps(
        sessionmaker: async_sessionmaker[AsyncSession], queue: ResumeQueue
    ) -> tuple[uuid.UUID, uuid.UUID, int, datetime | None, datetime | None]:
        fail_id = await _insert_approval(
            sessionmaker,
            status=ApprovalStatus.approved,
            resolved_at=_naive(120),
            resumed_at=None,
        )
        ok_id = await _insert_approval(
            sessionmaker,
            status=ApprovalStatus.approved,
            resolved_at=_naive(120),
            resumed_at=None,
        )

        real_enqueue = queue.enqueue
        fail_event = f"approval-{fail_id}-resolved"

        async def flaky(turn: QueuedTurn) -> str:
            if turn.event_id == fail_event:
                raise RuntimeError("valkey blip for one record")
            return await real_enqueue(turn)

        queue.enqueue = flaky  # type: ignore[method-assign]

        reconciler = ResumeReconciler(
            sessionmaker, queue, interval_seconds=30, grace_seconds=0, batch_limit=100
        )
        count = await reconciler.reconcile_once()
        return (
            fail_id,
            ok_id,
            count,
            await _resumed_at(sessionmaker, fail_id),
            await _resumed_at(sessionmaker, ok_id),
        )

    fail_id, ok_id, count, resumed_fail, resumed_ok = _run_async(steps, runs_stream)

    assert count == 1
    assert resumed_fail is None
    assert resumed_ok is not None
    entries = valkey.xrange(runs_stream)
    assert len(entries) == 1
    turn = QueuedTurn.model_validate(json.loads(entries[0][1]["payload"]))
    assert turn.event_id == f"approval-{ok_id}-resolved"


def test_reconciler_skips_row_locked_by_concurrent_claim(
    clean_db: None, valkey: redis.Redis, runs_stream: str
) -> None:
    """A row a concurrent replica already holds under ``FOR UPDATE`` is skipped by
    this pass's ``SELECT ... FOR UPDATE SKIP LOCKED`` claim -- never double-enqueued
    -- and picked up on the next pass once the lock releases. Two API replicas'
    overlapping reconcile passes therefore never both re-run one approved action.

    The whole interleave runs on one event loop across two distinct engines (two
    real Postgres connections that genuinely contend). A ``statement_timeout`` on
    the reconciler's engine keeps the pre-fix path -- which selects the locked row
    and then blocks forever on its post-enqueue ``mark_approval_resumed`` UPDATE
    against the held lock -- from deadlocking the suite: today it surfaces the bug
    as a non-zero locked count and a non-empty stream instead of hanging.
    """

    async def coro() -> tuple[uuid.UUID, int, int, int, datetime | None]:
        engine = create_async_engine(
            get_settings().database_url,
            connect_args={"server_settings": {"statement_timeout": "3000"}},
        )
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        lock_engine = create_async_engine(get_settings().database_url)
        lock_sessionmaker = async_sessionmaker(lock_engine, expire_on_commit=False)
        client = aioredis.Redis(
            host=_VALKEY_HOST, port=_VALKEY_PORT, password=_VALKEY_PW or None
        )
        queue = ResumeQueue(client, stream=runs_stream)
        try:
            approval_id = await _insert_approval(
                sessionmaker,
                status=ApprovalStatus.approved,
                resolved_at=_naive(120),
                resumed_at=None,
            )
            reconciler = ResumeReconciler(
                sessionmaker,
                queue,
                interval_seconds=30,
                grace_seconds=0,
                batch_limit=100,
            )

            # A concurrent replica holds the row under FOR UPDATE, uncommitted.
            async with lock_sessionmaker() as holder:
                await holder.execute(
                    select(Approval)
                    .where(Approval.id == approval_id)
                    .with_for_update()
                )
                # This pass must skip the locked row (0, nothing enqueued).
                try:
                    count_locked = await reconciler.reconcile_once()
                except Exception:  # noqa: BLE001
                    # Pre-fix: the locked row is selected, enqueued, then the mark
                    # UPDATE blocks on the held lock until statement_timeout fires.
                    count_locked = -1
                locked_len = len(await client.xrange(runs_stream))
                await holder.rollback()

            # Lock released: the next pass claims and enqueues it exactly once.
            count_free = await reconciler.reconcile_once()
            resumed_at = await _resumed_at(sessionmaker, approval_id)
            return approval_id, count_locked, locked_len, count_free, resumed_at
        finally:
            await client.aclose()
            await engine.dispose()
            await lock_engine.dispose()

    approval_id, count_locked, locked_len, count_free, resumed_at = asyncio.run(coro())

    # While the row was locked, the pass skipped it: no work, nothing enqueued.
    assert count_locked == 0
    assert locked_len == 0

    # Once the lock released, the same row is picked up exactly once and marked.
    assert count_free == 1
    entries = valkey.xrange(runs_stream)
    assert len(entries) == 1
    turn = QueuedTurn.model_validate(json.loads(entries[0][1]["payload"]))
    assert turn.event_id == f"approval-{approval_id}-resolved"
    assert resumed_at is not None
