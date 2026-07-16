"""The Valkey Streams consumer: read, dispatch to the kernel, ack, and recover.

Uses a consumer group on the dispatcher's stream so multiple worker replicas
share the load and every entry is delivered to exactly one consumer. New entries
are read with ``XREADGROUP ... >``; entries that a consumer took but never acked
(a crash mid-run) are reclaimed with ``XAUTOCLAIM`` after an idle timeout and
reprocessed. Reprocessing is safe because the kernel is idempotent (the done
marker) and the side-effect marker blocks auto-retry of a half-run action.

Entries are dispatched concurrently across threads (bounded by a semaphore); the
kernel serializes within a thread. A successfully handled entry is acked; an
entry that raises is left pending for the next reclaim.

Delivery is **bounded** (#505). Reclaim-and-retry is not infinite: an entry that
has already been delivered ``max_delivery`` times and still failed is moved to a
dead-letter stream (``<stream>:dead`` by default) with its original fields plus
``dl_*`` failure metadata, then acked off the group. Without that cap a
permanently-failing entry (the motivating case: a reply POST to a CLI stub URL
that was persisted into a durable approval and is now a dead port) is reclaimed
and re-dispatched forever, silently stalling the whole consumer group. An
unparseable entry takes the same route (``reason="unparseable"``) so poison is
observable instead of being silently acked into the void.

The graveyard itself is capped (approximate ``MAXLEN``, ``dead_letter_maxlen``):
the unparseable path fires per INBOUND entry, so an unbounded graveyard would
grow at full ingest rate on the Valkey the kernel's locks and markers live on.
Dead-letter rows are therefore best-effort -- bounded loss traded against a
platform-wide OOM.

The delivery count is read from Valkey's pending-entries list on every pass and
is NEVER tracked in a process-local dict: the PEL counter is durable, so a
restarted or replacement worker still sees the accumulated count and still caps.
A process-local counter would reset on restart and let a crash-looping worker
retry a poison entry forever -- the exact stall this cap exists to end.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
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

# XPENDING page size for the over-cap scan in ``_dead_letter_over_cap``.
# Deliberately NOT ``read_count``: that knob bounds how many entries the READ
# loop dispatches concurrently, whereas this scan dispatches nothing -- it is a
# pure metadata read that discards every under-cap row. Paging it at dispatch
# granularity costs one sequential round-trip per ``read_count`` entries, so a
# large backlog pages for no reason. A healthy tick is unaffected either way:
# the IDLE filter plus the empty-page early-out make it a single round-trip.
_CAP_SCAN_PAGE = 1000


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
                logger.exception("unparseable stream entry %s; dead-lettering", entry_id)
                await self._dead_letter(
                    entry_id,
                    fields,
                    reason="unparseable",
                    delivery_count=await self._pending_delivery_count(entry_id),
                )
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

    async def _pending_delivery_count(self, entry_id: str) -> int:
        """This entry's CURRENT delivery count, read from the PEL.

        The unparseable path reaches here from the read loop OR from a reclaim:
        an entry can be delivered, have its worker crash before it ever parses,
        and be reclaimed -- so its count is 2+ by the time it is dead-lettered.
        Hardcoding 1 would fabricate the graveyard's ``dl_delivery_count``
        precisely during crash recovery, when the evidence matters most. Read
        from the PEL, never a process-local counter, for the same durability
        reason the cap does. Falls back to 1 only if the row has vanished (it was
        delivered to us at least once), which is the honest floor rather than a
        guess.
        """
        rows = await self._redis.xpending_range(
            self._config.stream,
            self._config.consumer_group,
            min=entry_id,
            max=entry_id,
            count=1,
        )
        return int(rows[0]["times_delivered"]) if rows else 1

    # -- dead letter ----------------------------------------------------------

    @property
    def _dead_letter_stream(self) -> str:
        return self._config.dead_letter_stream_name()

    async def _dead_letter(
        self,
        entry_id: str,
        fields: dict[str, str] | None,
        *,
        reason: str,
        delivery_count: int,
    ) -> None:
        """Move an entry to the graveyard and ack it off the main group.

        ``fields`` are the original entry's fields, kept verbatim so a human or a
        replay tool can inspect them; ``None`` writes a metadata-only row (the
        entry was pending but its message had been trimmed off the stream).

        The ``dl_`` prefix is a CONVENTION, not a guarantee: the unparseable path
        accepts arbitrary malformed field maps, so an entry may already carry its
        own ``dl_original_id``. A plain copy-then-update would let the payload
        forge (or be silently clobbered by) the graveyard's own metadata. Original
        keys already starting with ``dl_`` are therefore escaped by doubling the
        prefix (``dl_reason`` -> ``dl_dl_reason``) before the metadata is written
        last. The escape is injective -- escaped keys always start with ``dl_dl_``
        and so can never equal a metadata key, and un-escaping strips exactly one
        ``dl_`` -- so the metadata always wins AND the original is fully
        recoverable.

        XADD before XACK, deliberately: a crash between the two leaves the entry
        pending, so it is re-reclaimed and re-dead-lettered -- a duplicate
        graveyard row, which is strictly safer than the XACK-first ordering's
        failure mode (a lost entry). Two replicas racing the same over-cap entry
        produce the same acceptable duplicate.

        The XADD is bounded by an approximate ``dead_letter_maxlen``, so
        graveyard rows are BEST-EFFORT: under a flood the oldest rows are evicted
        and those failures are lost. That loss is deliberate. The unparseable
        path dead-letters per inbound entry, so a wire-format drift would grow an
        unbounded graveyard at full ingest rate on the same Valkey that holds the
        kernel's locks and side-effect markers -- bounded record loss is traded
        against a platform-wide OOM. ``approximate=True`` lets Valkey trim on
        node boundaries, so the stream is bounded at *at least* the configured
        length, not exactly it.
        """
        target = self._dead_letter_stream
        # Escape the original's own ``dl_*`` keys (see above) so the metadata
        # written last always wins and the original stays recoverable.
        payload: dict[str, str] = {
            (f"dl_{k}" if k.startswith("dl_") else k): v for k, v in (fields or {}).items()
        }
        payload.update(
            {
                "dl_original_id": entry_id,
                "dl_delivery_count": str(delivery_count),
                "dl_reason": reason,
                "dl_dead_lettered_at": datetime.now(UTC).isoformat(),
            }
        )
        await self._redis.xadd(
            target,
            payload,
            maxlen=self._config.dead_letter_maxlen,
            approximate=True,
        )
        logger.error(
            "dead-lettered entry %s after %d deliveries (reason=%s) -> %s",
            entry_id,
            delivery_count,
            reason,
            target,
        )
        await self._ack(entry_id)

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
        """Reclaim entries pending too long from any (dead) consumer and retry.

        Entries that have already exhausted their delivery budget are
        dead-lettered first, so they are never claimed or re-dispatched again.
        XAUTOCLAIM still claims an over-cap entry whose dead-letter failed (it is
        still pending), so the ids it reports are skipped rather than dispatched:
        the cap binds even when the graveyard is unwritable.
        """
        over_cap = await self._dead_letter_over_cap()
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
                if entry_id in over_cap:
                    continue  # budget spent; never dispatch it again
                reclaimed += 1
                await self._dispatch(entry_id, fields)
            if cursor in ("0-0", "0"):
                break
        return reclaimed

    async def _dead_letter_over_cap(self) -> set[str]:
        """Dead-letter pending entries that have exhausted their delivery budget.

        Returns every over-cap id seen this pass -- including ones whose
        dead-letter failed -- so the caller never re-dispatches them.

        Read the delivery count with XPENDING *before* XAUTOCLAIM rather than
        after, because XAUTOCLAIM increments the counter as it claims: the
        pre-claim value is the number of deliveries ALREADY made, so an entry at
        ``>= max_delivery`` has had its full budget and must not be claimed
        again. Reading post-claim would fold in the current claim's own bump and
        kill the entry one delivery early.

        The scan PAGES THROUGH THE WHOLE pending list (``min`` advanced past the
        last id seen), because XAUTOCLAIM below pages through all of it too: a
        single ``COUNT _CAP_SCAN_PAGE`` page would cap-check only the head of the
        list while XAUTOCLAIM happily claimed and dispatched the over-cap tail,
        so the bound would silently not hold at backlog scale.

        The IDLE filter matches XAUTOCLAIM's ``min_idle_time`` so both see the
        same candidate set and an entry that is not yet reclaim-eligible is never
        prematurely dead-lettered.

        A failure to dead-letter ONE entry is logged and isolated: it must not
        stop the other entries being cap-checked, nor XAUTOCLAIM, nor
        ``reap_orphans``. This is the first await of the maintenance tick, and an
        unguarded raise here would kill crash recovery for the whole group on
        every tick -- #505's own stall class.
        """
        over_cap: set[str] = set()
        page_size = _CAP_SCAN_PAGE
        cursor = "-"
        while True:
            pending = await self._redis.xpending_range(
                self._config.stream,
                self._config.consumer_group,
                min=cursor,
                max="+",
                count=page_size,
                idle=self._config.reclaim_min_idle_ms,
            )
            if not pending:
                break
            for row in pending:
                entry_id = str(row["message_id"])
                # An entry in flight on THIS consumer is not an orphan: it is
                # being worked right now, and its count must not be judged. This
                # guard stays ahead of the cap check.
                if entry_id in self._inflight_ids:
                    continue
                delivered = int(row["times_delivered"])
                if delivered < self._config.max_delivery:
                    continue
                over_cap.add(entry_id)
                try:
                    await self._dead_letter_over_cap_entry(entry_id, delivered)
                except Exception:
                    logger.exception(
                        "dead-lettering entry %s failed; left pending, not dispatched",
                        entry_id,
                    )
            if len(pending) < page_size:
                break
            # Exclusive lower bound: resume after the last id of this page.
            cursor = f"({pending[-1]['message_id']}"
        return over_cap

    async def _dead_letter_over_cap_entry(self, entry_id: str, delivered: int) -> None:
        # XPENDING returns no fields; fetch the original payload. The id can be
        # pending while its message was trimmed off the stream, in which case
        # XRANGE comes back empty -> a metadata-only graveyard row, and the XACK
        # still happens (skipping it would leave the stall in place).
        found = await self._redis.xrange(self._config.stream, min=entry_id, max=entry_id)
        fields = cast("list[StreamEntry]", found)[0][1] if found else None
        await self._dead_letter(
            entry_id, fields, reason="max delivery exceeded", delivery_count=delivered
        )
