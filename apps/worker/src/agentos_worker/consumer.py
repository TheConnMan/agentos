"""The Valkey Streams consumer: read, dispatch to the kernel, ack, and recover.

Uses a consumer group on the dispatcher's stream so multiple worker replicas
share the load and every entry is delivered to exactly one consumer. New entries
are read with ``XREADGROUP ... >``; entries that a consumer took but never acked
(a crash mid-run) are reclaimed with ``XAUTOCLAIM`` after an idle timeout and
reprocessed. Reprocessing is safe because the kernel is idempotent (the done
marker) and the side-effect marker blocks auto-retry of a half-run action.

Entries are dispatched concurrently across threads (bounded by a semaphore); the
kernel serializes within a thread. A successfully handled entry is acked; an
entry that raises is left pending for the next reclaim; an unparseable entry is
acked (a poison message must not be reclaimed forever).
"""

from __future__ import annotations

import asyncio
import logging
from typing import cast

from agentos_dispatcher.queue import from_stream_fields
from redis.asyncio import Redis

from .config import WorkerConfig
from .kernel import Kernel
from .stream_consumer import ReadLoopSpec, StreamConsumer, StreamEntry

logger = logging.getLogger(__name__)

# Pause before retrying the blocking stream read after a transient transport
# error, so a briefly-unreachable Valkey does not spin the read loop hot.
_READ_ERROR_BACKOFF_S = 0.5


class Consumer(StreamConsumer):
    """Runs the read loop and the periodic reclaim/reap maintenance loop."""

    def __init__(
        self,
        *,
        redis: Redis,
        kernel: Kernel,
        config: WorkerConfig,
        max_concurrency: int = 16,
    ) -> None:
        super().__init__(redis)
        self._kernel = kernel
        self._config = config
        self._sem = asyncio.Semaphore(max_concurrency)
        self._inflight: set[asyncio.Task[None]] = set()
        # Entry ids currently being handled by THIS consumer. XAUTOCLAIM would
        # otherwise reclaim our own long-running (still-pending) entries and
        # re-dispatch a duplicate handler that steers the same prompt into its
        # own live turn; skipping these ids prevents that self-reclaim.
        self._inflight_ids: set[str] = set()

    async def ensure_group(self) -> None:
        """Create the consumer group (and the stream) if it does not exist.

        Created at ``$`` (the stream's current tail) so a first boot against a
        stream that already carries entries does NOT replay the whole backlog:
        a persistent Valkey that accumulated stale Slack mentions while no worker
        ran would otherwise storm every one of them into a live turn the moment
        the group is created. Only entries produced after the group exists are
        delivered; crash-recovery of in-flight entries is unaffected (it works
        off the pending list, not the group's start id). An existing group is
        left untouched.
        """
        await self._ensure_group(
            self._config.stream, self._config.consumer_group, start_id="$"
        )

    async def run(self) -> None:
        await self.ensure_group()
        await asyncio.gather(self._read_loop(), self._maintenance_loop())
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)

    # -- read loop ------------------------------------------------------------

    async def _read_loop(self) -> None:
        await self._consume(
            ReadLoopSpec(
                stream=self._config.stream,
                group=self._config.consumer_group,
                consumer=self._config.consumer_name,
                count=self._config.read_count,
                block_ms=self._config.read_block_ms,
                backoff_s=_READ_ERROR_BACKOFF_S,
                timeout_msg="stream read timed out (idle); retrying: %s",
                connection_msg="stream read failed transiently; retrying: %s",
                logger=logger,
            ),
            self._dispatch,
        )

    async def _dispatch(self, entry_id: str, fields: dict[str, str]) -> None:
        if entry_id in self._inflight_ids:
            return  # already being handled by this consumer
        # Acquire a capacity slot BEFORE spawning the handler so a burst larger
        # than max_concurrency exerts backpressure on the read loop instead of
        # claiming the whole backlog into this consumer's local queue (which would
        # starve other replicas and make a crash wait out the reclaim window).
        await self._sem.acquire()
        self._inflight_ids.add(entry_id)
        task = asyncio.create_task(self._handle(entry_id, fields))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _handle(self, entry_id: str, fields: dict[str, str]) -> None:
        try:
            try:
                qevent = from_stream_fields(fields)
            except Exception:
                logger.exception("unparseable stream entry %s; acking as poison", entry_id)
                await self._ack(entry_id)
                return
            try:
                await self._kernel.process_event(qevent)
            except Exception:
                # Leave the entry pending: XAUTOCLAIM will reclaim and retry.
                logger.exception("processing failed for entry %s; left pending", entry_id)
                return
            await self._ack(entry_id)
        finally:
            self._inflight_ids.discard(entry_id)
            self._sem.release()

    async def _ack(self, entry_id: str) -> None:
        await self._xack(self._config.stream, self._config.consumer_group, entry_id)

    # -- maintenance loop -----------------------------------------------------

    async def _maintenance_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._reclaim_once()
                await self._kernel.reap_orphans()
            except Exception:
                logger.exception("maintenance tick failed")
            await self._sleep_or_stop(self._config.reclaim_interval_s)

    async def _reclaim_once(self) -> int:
        """Reclaim entries pending too long from any (dead) consumer and retry."""
        reclaimed = 0
        cursor: str = "0-0"
        while not self._stop.is_set():
            raw = await self._redis.xautoclaim(
                self._config.stream,
                self._config.consumer_group,
                self._config.consumer_name,
                min_idle_time=self._config.reclaim_min_idle_ms,
                start_id=cursor,
                count=self._config.read_count,
            )
            cursor = str(raw[0])
            entries = cast("list[StreamEntry]", raw[1])
            for entry_id, fields in entries:
                if entry_id in self._inflight_ids:
                    continue  # still being processed here; not an orphan
                reclaimed += 1
                await self._dispatch(entry_id, fields)
            if cursor in ("0-0", "0"):
                break
        return reclaimed
