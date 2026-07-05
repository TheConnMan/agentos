"""Consumer tests: end-to-end stream consumption and crash-recovery reclaim,
against the real Valkey stream + consumer group.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable

from aci_protocol import Final, SessionStatus, TextDelta
from agentos_dispatcher.queue import QueuedSlackEvent
from agentos_worker.consumer import Consumer

DONE = SessionStatus.DONE


def _qevent(text: str, *, thread: str = "th-1", event_id: str | None = None) -> QueuedSlackEvent:
    return QueuedSlackEvent(
        slack_event_id=event_id or uuid.uuid4().hex,
        thread_ts=thread,
        channel="C1",
        user="U1",
        text=text,
        placeholder_ts="p-1",
        received_at="2026-07-05T00:00:00+00:00",
    )


async def _wait_until(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def test_consumes_stream_entry_end_to_end_and_acks(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [TextDelta(text="hi "), Final(text="answer", status=DONE)]
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            qe = _qevent("hello", thread="tc1", event_id="c1")
            await h.async_redis.xadd(h.config.stream, qe.to_stream_fields())

            task = asyncio.create_task(consumer.run())
            await _wait_until(lambda: h.sink.last_text == "answer")
            consumer.request_stop()
            await task

            assert h.runner.opened == ["hello"]
            summary = await h.async_redis.xpending(h.config.stream, h.config.consumer_group)
            assert summary["pending"] == 0  # the entry was acked

    asyncio.run(go())


def test_reclaims_and_reprocesses_a_dead_consumers_pending_entry(make_harness) -> None:
    async def go() -> None:
        async with make_harness(reclaim_min_idle_ms=0) as h:
            h.runner.default_script = [Final(text="recovered", status=DONE)]
            consumer = Consumer(redis=h.async_redis, kernel=h.kernel, config=h.config)
            await consumer.ensure_group()

            qe = _qevent("orphan", thread="tr1", event_id="r1")
            await h.async_redis.xadd(h.config.stream, qe.to_stream_fields())

            # A different (now "dead") consumer takes delivery but never acks,
            # leaving the entry pending — the crash mid-run case.
            dead = await h.async_redis.xreadgroup(
                h.config.consumer_group, "dead-consumer", {h.config.stream: ">"}, count=1
            )
            assert dead

            # Our consumer reclaims the pending entry and reprocesses it.
            reclaimed = await consumer._reclaim_once()
            assert reclaimed == 1
            await _wait_until(lambda: h.sink.last_text == "recovered")
            await asyncio.gather(*list(consumer._inflight))

            assert h.runner.opened == ["orphan"]
            summary = await h.async_redis.xpending(h.config.stream, h.config.consumer_group)
            assert summary["pending"] == 0  # reclaimed entry acked after reprocessing

    asyncio.run(go())
