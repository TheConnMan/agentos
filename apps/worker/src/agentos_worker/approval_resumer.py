"""Resume a suspended session when its approval is resolved (#22, worker half).

Mirrors the kill-switch consumer (``killswitch.py``): the API publishes on
``agentos:approval-events`` when an approval is resolved, and this subscriber
wakes up and resumes the paused session. The durable ``Approval`` row is the
source of truth, so a resolve that happened while every worker was down (the
publish reached no subscriber) is caught by a periodic reconcile sweep.

The resume itself does NOT reach into the kernel. It enqueues a *synthetic turn*
onto the same runs stream the dispatcher feeds, so it flows through the proven
consumer -> route -> claim path: a turn for a thread whose sandbox is SUSPENDED
raises SuspendedThreadError and the kernel resumes (rehydrates from history) and
delivers the approval outcome. Reusing that path inherits the concurrency,
ordering, and crash-recovery guarantees instead of a second kernel entrypoint.

Exactly-once resume is the kernel's done-marker on the synthetic event's stable
``event_id``: a re-enqueue (pubsub racing the sweep, or a crash before
``mark_resumed``) is skipped downstream. ``mark_resumed`` then stops the sweep
from re-enqueuing a settled resume.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, cast

from aci_protocol import QueuedTurn, ReplyHandle
from agentos_dispatcher.queue import to_stream_fields
from redis.asyncio import Redis

from .approvals import ApprovalRecord, ApprovalStore
from .config import WorkerConfig

logger = logging.getLogger(__name__)

APPROVAL_CHANNEL = "agentos:approval-events"

# The synthetic resume event's author, and the stable event-id prefix that makes
# the kernel's done-marker dedupe a re-enqueued resume.
_RESUME_AUTHOR = "agentos-approvals"
_RESUME_EVENT_PREFIX = "approval-resume-"


class ApprovalResumer:
    """Subscribes to approval resolutions and resumes the paused sessions."""

    def __init__(
        self,
        *,
        redis: Redis,
        store: ApprovalStore,
        config: WorkerConfig,
    ) -> None:
        self._redis = redis
        self._store = store
        self._config = config
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Subscribe to resolutions and periodically reconcile until stopped.

        The first reconcile is deliberately deferred one interval rather than run
        at t=0: the runs consumer group is created at ``$`` (only new entries), so
        a synthetic turn enqueued before that group exists on a first-ever boot
        would be dropped. By one interval in, the consumer has created its group.
        On every later boot the group already exists, and the pubsub fast path
        covers the common case regardless.
        """
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(APPROVAL_CHANNEL)
        interval = self._config.approval_reconcile_interval_s
        last_sweep = 0.0
        elapsed = 0.0
        try:
            while not self._stop.is_set():
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message is not None:
                    await self._handle(message)
                elapsed += 1.0
                if elapsed - last_sweep >= interval:
                    last_sweep = elapsed
                    await self._reconcile_safe()
        finally:
            await pubsub.unsubscribe(APPROVAL_CHANNEL)
            await pubsub.aclose()  # type: ignore[no-untyped-call]

    async def _handle(self, message: dict[str, object]) -> None:
        try:
            payload = json.loads(_as_text(message["data"]))
            approval_id = uuid.UUID(payload["approval_id"])
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            logger.exception("malformed approval event: %r", message.get("data"))
            return
        record = await self._store.get(approval_id)
        if record is None:
            logger.warning("approval %s resolved but no record found", approval_id)
            return
        await self._resume(record)

    async def _reconcile_safe(self) -> None:
        # A sweep failure (a transient DB blip) must not tear down the subscriber
        # and miss every later resolution; log and let the next tick retry.
        try:
            await self.reconcile_once()
        except Exception:
            logger.exception("approval reconcile sweep failed")

    async def reconcile_once(self) -> int:
        """Resume every resolved-but-not-yet-resumed approval. Returns the count.

        The missed-message guard: a resolve that fired while all workers were down
        published to nobody, so nothing consumed the wake-up. This finds those
        durable rows and drives their resume.
        """
        records = await self._store.list_resolved_unresumed()
        for record in records:
            await self._resume(record)
        return len(records)

    async def _resume(self, record: ApprovalRecord) -> None:
        if record.status not in ("approved", "rejected"):
            return
        turn = QueuedTurn(
            event_id=f"{_RESUME_EVENT_PREFIX}{record.id}",
            conversation_id=record.conversation_id,
            author=_RESUME_AUTHOR,
            text=_resume_text(record),
            reply_handle=ReplyHandle(
                channel=record.channel,
                placeholder=record.reply_placeholder,
                endpoint=record.reply_endpoint,
            ),
            received_at=datetime.now(UTC).isoformat(),
        )
        # Enqueue before mark_resumed: a re-enqueue is idempotent downstream (the
        # kernel's done-marker on the stable event_id), whereas a mark that landed
        # before a failed enqueue would strand the resume.
        # redis-py types the fields arg as an invariant broad mapping, so a plain
        # dict[str, str] does not match; cast (the dispatcher's enqueue does the same).
        await self._redis.xadd(
            self._config.stream, cast("dict[Any, Any]", to_stream_fields(turn))
        )
        await self._store.mark_resumed(record.id)
        logger.info(
            "enqueued approval resume for %s (%s) on thread %s",
            record.id,
            record.status,
            record.conversation_id,
        )


def _resume_text(record: ApprovalRecord) -> str:
    if record.status == "approved":
        return (
            f"[approval approved] The request to use {record.tool} was approved. "
            "Proceed with it."
        )
    return (
        f"[approval rejected] The request to use {record.tool} was rejected. "
        "Do not proceed; acknowledge the rejection and continue without it."
    )


def _as_text(data: object) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8")
    return str(data)
