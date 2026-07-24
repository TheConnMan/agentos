"""Valkey markers for idempotency and crash-safe side-effect tracking.

Two markers, both keyed by the Slack event id (the idempotency key the dispatcher
assigned):

- ``done``: set once an event has been terminally handled (streamed to a final,
  or escalated). A redelivery (Slack retry that slipped the dispatcher guard, or
  a crash-recovery reclaim of an already-finished entry) sees it and is skipped.
- ``side_effect``: set the instant a ``side_effect_flag`` frame is observed, and
  therefore durable across a worker crash. The no-retry-after-side-effects rule
  needs this to survive process death: if a reclaimed event already executed a
  side effect but never reached ``done``, it must escalate, not silently re-run.
"""

from __future__ import annotations

from redis.asyncio import Redis

from .config import WorkerConfig


class Markers:
    """Idempotency and side-effect markers over Valkey."""

    def __init__(self, redis: Redis, config: WorkerConfig) -> None:
        self._redis = redis
        self._config = config

    async def is_done(self, event_id: str) -> bool:
        return bool(await self._redis.exists(self._config.done_key(event_id)))

    async def mark_done(self, event_id: str) -> None:
        await self._redis.set(
            self._config.done_key(event_id), "1", ex=self._config.idempotency_ttl_s
        )

    async def saw_side_effect(self, event_id: str) -> bool:
        return bool(await self._redis.exists(self._config.side_effect_key(event_id)))

    async def mark_side_effect(self, event_id: str) -> None:
        await self._redis.set(
            self._config.side_effect_key(event_id), "1", ex=self._config.idempotency_ttl_s
        )
