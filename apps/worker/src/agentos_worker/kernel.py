"""The concurrency kernel: route one Slack event to a runner turn, get every
failure mode right.

Rules implemented here (detailed-architecture section 2b):

1. One live session per thread. A follow-up to a thread with a live turn is a
   *steer* into that turn, not a new turn. The per-thread lock plus opening the
   new turn *before* releasing the lock guarantees a thread never has two turns.
2. The finish race. A steer that arrives as the turn ends returns 409; the kernel
   then opens a fresh turn on the same (idle) sandbox. This check-and-fall-back
   is the compare-and-swap the worker owns.
3. Steer vs interrupt. Default is steer; ``interrupt_thread`` is the explicit
   hard stop (a Slack :stop: affordance would call it). We never keyword-guess.
5. No auto-retry after side effects. A failed run that emitted ``side_effect_flag``
   escalates to a human instead of retrying; the flag is persisted the instant it
   is seen so a crash mid-side-effect still escalates on reclaim. Flag-clean
   failures retry by error classification (rate-limit / runner-error are
   transient; budget-exceeded and everything else escalate).

Idempotency: the Slack event id gates a ``done`` marker so a redelivered or
reclaimed event that already finished is skipped.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import aiohttp
from aci_protocol import (
    ErrorEvent,
    Event,
    Final,
    OutboundEvent,
    SessionStatus,
    SideEffectFlag,
    TextDelta,
    ToolNote,
)
from agentos_dispatcher.queue import QueuedSlackEvent

from .config import WorkerConfig
from .markers import Markers
from .runner_client import RunnerClient, TurnStream
from .sandbox import SandboxSubstrate
from .sandbox.types import SandboxHandle, SuspendedThreadError
from .slack_sink import SlackSink
from .threadlock import ThreadLock

logger = logging.getLogger(__name__)

# Failure classifications that are worth retrying (transient). Everything else
# (budget-exceeded, model/server errors) escalates rather than looping.
RETRYABLE_CLASSIFICATIONS = frozenset({"rate-limit", "runner-error"})


@dataclass
class TurnOutcome:
    """The result of streaming one turn, feeding the retry/escalate decision."""

    terminal_ok: bool
    saw_side_effect: bool = False
    classification: str | None = None
    text: str = ""
    status: SessionStatus | None = None
    steered: bool = False


@dataclass
class _RouteResult:
    steered: bool
    handle: SandboxHandle | None = None
    turn: TurnStream | None = None


@dataclass
class _LockEntry:
    """A per-thread in-process lock plus a holder/waiter refcount so the entry
    can be evicted when idle (otherwise the map grows one entry per thread ever
    seen, an unbounded leak in a long-running worker)."""

    lock: asyncio.Lock
    refs: int = 0


@dataclass
class _StreamAccumulator:
    text_parts: list[str] = field(default_factory=list)
    saw_side_effect: bool = False
    classification: str | None = None
    status: SessionStatus | None = None
    final_text: str | None = None

    def rendered(self) -> str:
        return self.final_text if self.final_text is not None else "".join(self.text_parts)


class _ThrottledReply:
    """Coalesces chat.update edits while streaming; always flushes the final."""

    def __init__(self, sink: SlackSink, *, channel: str, ts: str, min_interval_s: float) -> None:
        self._sink = sink
        self._channel = channel
        self._ts = ts
        self._min_interval_s = min_interval_s
        self._last = 0.0
        self._last_text: str | None = None

    async def stream(self, text: str) -> None:
        if not text or text == self._last_text:
            return
        now = time.monotonic()
        if now - self._last < self._min_interval_s:
            return
        self._last = now
        self._last_text = text
        await self._sink.update(channel=self._channel, ts=self._ts, text=text)

    async def finalize(self, text: str) -> None:
        if text == self._last_text:
            return
        self._last_text = text
        await self._sink.update(channel=self._channel, ts=self._ts, text=text or "(no response)")


class Kernel:
    """Routes events to runner turns and enforces the concurrency rules."""

    def __init__(
        self,
        *,
        substrate: SandboxSubstrate,
        runner: RunnerClient,
        sink: SlackSink,
        lock: ThreadLock,
        markers: Markers,
        config: WorkerConfig,
    ) -> None:
        self._substrate = substrate
        self._runner = runner
        self._sink = sink
        self._lock = lock
        self._markers = markers
        self._config = config
        # In-process per-thread lock over the route/start critical section only.
        # asyncio.Lock is FIFO, so same-thread events from one worker open/steer
        # the runner in arrival order (ordering preserved under concurrent sends).
        # The cross-worker guarantee is the Valkey ThreadLock; this adds
        # deterministic ordering within a process without blocking steering,
        # because it is released before the stream is consumed.
        self._order_locks: dict[str, _LockEntry] = {}

    async def process_event(self, qevent: QueuedSlackEvent) -> None:
        """Handle one queued Slack event to a terminal state (success or escalate).

        Returns normally once the event is terminally handled; the consumer then
        acks it. Raising leaves the entry pending for crash-recovery reclaim.
        """
        event_id = qevent.slack_event_id
        thread = qevent.thread_ts

        # Acquire the per-thread order lock BEFORE any await, so concurrent
        # same-thread events queue in task-arrival order (asyncio.Lock is FIFO and
        # an uncontended acquire does not yield). It is released as soon as this
        # event's turn is started or steered (``_release_order`` in _attempt), so
        # streaming and steering are never blocked; holding it across the marker
        # checks is what keeps those awaits from reordering arrivals.
        entry = self._acquire_order_entry(thread)
        await entry.lock.acquire()
        release_state = {"done": False}

        def release_order() -> None:
            if not release_state["done"]:
                release_state["done"] = True
                entry.lock.release()
                self._release_order_entry(thread, entry)

        try:
            if await self._markers.is_done(event_id):
                logger.info("event %s already done; skipping", event_id)
                return

            # Crash-safety: a prior attempt executed a side effect but never
            # reached done (worker died mid-run). Do not auto-retry the action.
            if await self._markers.saw_side_effect(event_id):
                await self._escalate(
                    qevent,
                    "A prior attempt started an action before the worker restarted; "
                    "not retrying automatically. Flagging for a human.",
                )
                await self._markers.mark_done(event_id)
                return

            attempt = 0
            while True:
                attempt += 1
                outcome = await self._attempt(qevent, release_order)

                if outcome.terminal_ok:
                    await self._markers.mark_done(event_id)
                    return

                if outcome.saw_side_effect:
                    await self._escalate(
                        qevent,
                        f"The run hit an error ({outcome.classification or 'unknown'}) after "
                        "starting an action; not retrying automatically. Flagging for a human.",
                    )
                    await self._markers.mark_done(event_id)
                    return

                retryable = outcome.classification in RETRYABLE_CLASSIFICATIONS
                if not retryable or attempt >= self._config.max_attempts:
                    await self._escalate(
                        qevent,
                        f"The run failed ({outcome.classification or 'unknown'}) after "
                        f"{attempt} attempt(s). Flagging for a human.",
                    )
                    await self._markers.mark_done(event_id)
                    return

                await asyncio.sleep(self._backoff(attempt))
        finally:
            release_order()

    def _acquire_order_entry(self, thread: str) -> _LockEntry:
        entry = self._order_locks.get(thread)
        if entry is None:
            entry = _LockEntry(asyncio.Lock())
            self._order_locks[thread] = entry
        entry.refs += 1
        return entry

    def _release_order_entry(self, thread: str, entry: _LockEntry) -> None:
        entry.refs -= 1
        if entry.refs == 0 and self._order_locks.get(thread) is entry:
            del self._order_locks[thread]

    async def reap_orphans(self) -> list[str]:
        """Periodic tick: delete substrate claims no live route references."""
        return await asyncio.to_thread(self._substrate.reap_orphans)

    async def interrupt_thread(self, thread_key: str, reason: str) -> bool:
        """Hard-stop the thread's live turn. True if a live runner was signalled."""
        handle = await asyncio.to_thread(self._substrate.lookup, thread_key)
        if handle is None:
            return False
        await self._runner.interrupt(handle.base_url, reason)
        return True

    # -- internals ------------------------------------------------------------

    async def _attempt(
        self, qevent: QueuedSlackEvent, release_order: Callable[[], None]
    ) -> TurnOutcome:
        thread = qevent.thread_ts
        event = self._to_event(qevent)

        # Critical section: decide steer-vs-new-turn and, if new, open the turn so
        # it is active before we release the Valkey lock (rule 1: no two live
        # turns per thread across workers). Then release the order lock so the
        # next same-thread event can route, and release the Valkey lock before
        # streaming so a follow-up can steer.
        async with self._lock.hold(self._config.lock_key(thread)):
            route = await self._route_and_start(thread, event)
        release_order()

        if route.steered:
            # Delivered into the thread's live turn; that turn streams the output
            # onto its own placeholder. Retire this follow-up's placeholder so it
            # does not sit stuck on "working" in the thread.
            await self._sink.update(
                channel=qevent.channel,
                ts=qevent.placeholder_ts,
                text="Folded into the in-progress reply above.",
            )
            return TurnOutcome(terminal_ok=True, steered=True)

        assert route.handle is not None and route.turn is not None
        return await self._consume(qevent, route.turn)

    async def _route_and_start(self, thread: str, event: Event) -> _RouteResult:
        # claim() adopts the thread's live sandbox and refreshes its route TTL
        # (so a busy thread past route_ttl is not reaped), or claims a warm one /
        # resumes a suspended one. Then try to steer: a live turn takes the
        # follow-up; otherwise (fresh sandbox, or the finish-race where the turn
        # just ended -> 409) we open a new turn on that same sandbox.
        handle = await self._claim_or_resume(thread)
        if await self._runner.steer(handle.base_url, event):
            return _RouteResult(steered=True)
        turn = await self._runner.start_turn(handle.base_url, event)
        return _RouteResult(steered=False, handle=handle, turn=turn)

    async def _claim_or_resume(self, thread: str) -> SandboxHandle:
        try:
            return await asyncio.to_thread(self._substrate.claim, thread)
        except SuspendedThreadError:
            return await asyncio.to_thread(self._substrate.resume, thread)

    async def _consume(self, qevent: QueuedSlackEvent, turn: TurnStream) -> TurnOutcome:
        acc = _StreamAccumulator()
        reply = _ThrottledReply(
            self._sink,
            channel=qevent.channel,
            ts=qevent.placeholder_ts,
            min_interval_s=self._config.slack_edit_min_interval_s,
        )
        try:
            # ``async with`` releases the aiohttp response on every exit path
            # (normal end, apply-frame error, or a mid-stream transport drop), so
            # the connection is never leaked.
            async with turn:
                async for frame in turn:
                    await self._apply_frame(frame, acc, reply, qevent.slack_event_id)
        except (aiohttp.ClientError, TimeoutError) as exc:
            # Stream dropped mid-run (sandbox killed, network fault). No final.
            logger.warning("turn stream dropped for %s: %s", qevent.slack_event_id, exc)
            return TurnOutcome(
                terminal_ok=False,
                saw_side_effect=acc.saw_side_effect,
                classification=acc.classification or "runner-error",
                text=acc.rendered(),
            )

        return await self._finish(acc, reply)

    async def _apply_frame(
        self,
        frame: OutboundEvent,
        acc: _StreamAccumulator,
        reply: _ThrottledReply,
        event_id: str,
    ) -> None:
        if isinstance(frame, TextDelta):
            acc.text_parts.append(frame.text)
            await reply.stream(acc.rendered())
        elif isinstance(frame, ToolNote):
            # Surfaced for context but not part of the answer buffer.
            await reply.stream(acc.rendered())
        elif isinstance(frame, SideEffectFlag):
            acc.saw_side_effect = True
            # Persist immediately so a crash before done still blocks auto-retry.
            await self._markers.mark_side_effect(event_id)
        elif isinstance(frame, ErrorEvent):
            acc.classification = frame.classification or acc.classification
        elif isinstance(frame, Final):
            acc.status = frame.status
            acc.final_text = frame.text

    async def _finish(self, acc: _StreamAccumulator, reply: _ThrottledReply) -> TurnOutcome:
        if acc.status in (SessionStatus.DONE, SessionStatus.IDLE_AWAITING_INPUT):
            text = acc.rendered()
            await reply.finalize(text)
            return TurnOutcome(
                terminal_ok=True,
                saw_side_effect=acc.saw_side_effect,
                text=text,
                status=acc.status,
            )
        # classified-failure, or the stream ended with no final at all.
        return TurnOutcome(
            terminal_ok=False,
            saw_side_effect=acc.saw_side_effect,
            classification=acc.classification or "runner-error",
            text=acc.rendered(),
            status=acc.status,
        )

    async def _escalate(self, qevent: QueuedSlackEvent, message: str) -> None:
        logger.warning("escalating event %s: %s", qevent.slack_event_id, message)
        await self._sink.update(channel=qevent.channel, ts=qevent.placeholder_ts, text=message)

    def _backoff(self, attempt: int) -> float:
        raw: float = self._config.retry_backoff_base_s * (2 ** (attempt - 1))
        return min(self._config.retry_backoff_max_s, raw)

    @staticmethod
    def _to_event(qevent: QueuedSlackEvent) -> Event:
        return Event(type="message", text=qevent.text, user=qevent.user, ts=qevent.thread_ts)
