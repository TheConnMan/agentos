"""Shared Valkey consumer-group read-loop mechanics.

The runs consumer (``consumer.py``) and the eval consumer (``eval/stream.py``)
both drive the same Valkey ``XREADGROUP`` loop: ensure the group exists, do a
blocking group read, skip an empty/timeout response, survive a transient
transport fault (a blocking-read ``TimeoutError`` is the routine idle case ->
DEBUG; a ``ConnectionError`` is a real fault -> WARNING), back off and retry, and
ack a handled entry. That shared plumbing lives here once; each consumer keeps
its own stream/group/consumer names, backoff constant, log-message prefixes, and
per-message business logic and passes them in.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from redis.asyncio import Redis
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
)
from redis.exceptions import (
    ResponseError,
)
from redis.exceptions import (
    TimeoutError as RedisTimeoutError,
)

# One stream entry as redis returns it with decode_responses=True.
StreamEntry = tuple[str, dict[str, str]]

# An async per-message handler: (entry_id, fields) -> None.
EntryHandler = Callable[[str, dict[str, str]], Awaitable[None]]


@dataclass(frozen=True)
class ReadLoopSpec:
    """The per-consumer knobs the shared read loop needs. Everything here is
    deliberately passed in (not hard-coded) so each consumer's stream, group,
    backoff, and log output stay exactly what they were before the extraction."""

    stream: str
    group: str
    consumer: str
    count: int
    block_ms: int
    backoff_s: float
    # Log messages kept per-consumer so log output is byte-identical; each takes
    # a single ``%s`` for the exception. The logger is the owning module's logger
    # so records carry that module's name (tests assert on it).
    timeout_msg: str
    connection_msg: str
    logger: logging.Logger


class StreamConsumer:
    """Base for a Valkey consumer-group reader.

    Owns the transport-level plumbing (group create, blocking read loop with the
    Timeout->DEBUG / Connection->WARNING split and backoff, ack, stop-aware
    sleep). Subclasses supply their stream config via a :class:`ReadLoopSpec`,
    their per-message handler, and their own maintenance/reclaim loops and
    business logic.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def _ensure_group(self, stream: str, group: str, *, start_id: str) -> None:
        """Create the consumer group (and the stream) if it does not exist.

        ``start_id`` is the group's read start position and is per-consumer (see
        each subclass's ``ensure_group`` for why it picks ``$`` vs ``0``). An
        existing group is left untouched.
        """
        try:
            await self._redis.xgroup_create(stream, group, id=start_id, mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _consume(self, spec: ReadLoopSpec, handler: EntryHandler) -> None:
        """Blocking-read loop: read the group, dispatch each entry to ``handler``,
        and survive transient transport faults until stop is requested."""
        while not self._stop.is_set():
            try:
                resp = await self._redis.xreadgroup(
                    spec.group,
                    spec.consumer,
                    {spec.stream: ">"},
                    count=spec.count,
                    block=spec.block_ms,
                )
            except RedisTimeoutError as exc:
                # A blocking-read timeout is the routine idle case (no entries
                # arrived within block_ms plus the socket timeout margin), not a
                # fault -- log at DEBUG so an idle worker doesn't flood WARNING.
                # Still back off + retry rather than letting it kill the loop.
                spec.logger.debug(spec.timeout_msg, exc)
                await self._sleep_or_stop(spec.backoff_s)
                continue
            except RedisConnectionError as exc:
                # A real connection fault (a Valkey failover, pod-to-pod blip):
                # transient but worth a WARNING. Back off and retry; redis-py
                # reconnects on the next attempt.
                spec.logger.warning(spec.connection_msg, exc)
                await self._sleep_or_stop(spec.backoff_s)
                continue
            if not resp:
                continue
            streams = cast("list[tuple[str, list[StreamEntry]]]", resp)
            for _stream, entries in streams:
                for entry_id, fields in entries:
                    await handler(entry_id, fields)

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _xack(self, stream: str, group: str, entry_id: str) -> None:
        await self._redis.xack(stream, group, entry_id)
