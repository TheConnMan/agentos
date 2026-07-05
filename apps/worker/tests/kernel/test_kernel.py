"""Kernel rule tests: routing, steer, finish-race, interrupt, side-effect/retry.

Each rule is provoked against a real Valkey, the real G1 substrate, and a
scriptable in-process fake runner; only Slack and the model are faked.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable

from aci_protocol import (
    ErrorEvent,
    Final,
    SessionStatus,
    SideEffectFlag,
    TextDelta,
)
from agentos_dispatcher.queue import QueuedSlackEvent

DONE = SessionStatus.DONE
IDLE = SessionStatus.IDLE_AWAITING_INPUT
FAIL = SessionStatus.CLASSIFIED_FAILURE


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


def test_new_turn_streams_to_slack_and_acks(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [
                TextDelta(text="Hello "),
                TextDelta(text="world"),
                Final(text="Hello world", status=DONE),
            ]
            ev = _qevent("hi")
            await h.kernel.process_event(ev)

            assert h.runner.opened == ["hi"]
            assert h.sink.last_text == "Hello world"
            assert await h.async_redis.exists(h.config.done_key(ev.slack_event_id))

    asyncio.run(go())


def test_followup_steers_the_live_turn(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="working")]
            h.runner.tail = [Final(text="done", status=DONE)]

            e1 = _qevent("first", thread="tA")
            t1 = asyncio.create_task(h.kernel.process_event(e1))
            await _wait_until(lambda: h.runner.turn_active)

            # A follow-up on the same thread steers the live turn, not a new one.
            await h.kernel.process_event(_qevent("second", thread="tA"))
            assert h.runner.steers == ["second"]
            assert h.runner.opened == ["first"]

            hold.set()
            await t1

    asyncio.run(go())


def test_finish_race_falls_back_to_a_fresh_turn(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            # First turn completes; the sandbox stays live but idle (no turn).
            h.runner.default_script = [Final(text="one", status=DONE)]
            await h.kernel.process_event(_qevent("first", thread="tB"))

            # Follow-up: the steer hits 409 (no active turn) and the kernel opens
            # a fresh turn on the same idle sandbox.
            h.runner.default_script = [Final(text="two", status=DONE)]
            await h.kernel.process_event(_qevent("second", thread="tB"))

            assert h.runner.steers == []  # steer returned 409, not delivered
            assert h.runner.opened == ["first", "second"]
            assert h.sink.last_text == "two"

    asyncio.run(go())


def test_drop_mid_run_retries_then_succeeds(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            # Attempt 1 streams a delta then the stream ends with no final (a
            # mid-run drop). Attempt 2 completes.
            h.runner.turn_scripts = [
                [TextDelta(text="partial")],
                [TextDelta(text="full"), Final(text="full done", status=DONE)],
            ]
            ev = _qevent("go")
            await h.kernel.process_event(ev)

            assert h.runner.opened == ["go", "go"]  # retried
            assert h.sink.last_text == "full done"

    asyncio.run(go())


def test_side_effect_failure_escalates_without_retry(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            # A normally-retryable classification (runner-error) must NOT retry
            # once a side effect has executed.
            h.runner.default_script = [
                SideEffectFlag(tool="deploy"),
                ErrorEvent(message="boom", classification="runner-error"),
                Final(text="failed", status=FAIL),
            ]
            ev = _qevent("do it")
            await h.kernel.process_event(ev)

            assert h.runner.opened == ["do it"]  # exactly one attempt, no retry
            assert h.sink.last_text is not None and "human" in h.sink.last_text.lower()
            assert await h.async_redis.exists(h.config.side_effect_key(ev.slack_event_id))
            assert await h.async_redis.exists(h.config.done_key(ev.slack_event_id))

    asyncio.run(go())


def test_rate_limit_retries_then_succeeds(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            h.runner.turn_scripts = [
                [
                    ErrorEvent(message="rl", classification="rate-limit"),
                    Final(text="f", status=FAIL),
                ],
                [Final(text="recovered", status=DONE)],
            ]
            await h.kernel.process_event(_qevent("go"))

            assert h.runner.opened == ["go", "go"]
            assert h.sink.last_text == "recovered"

    asyncio.run(go())


def test_budget_exceeded_escalates_without_retry(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [
                ErrorEvent(message="over budget", classification="budget-exceeded"),
                Final(text="f", status=FAIL),
            ]
            await h.kernel.process_event(_qevent("go"))

            assert h.runner.opened == ["go"]  # budget-exceeded is not retryable
            assert h.sink.last_text is not None and "human" in h.sink.last_text.lower()

    asyncio.run(go())


def test_retries_are_bounded_then_escalate(make_harness) -> None:
    async def go() -> None:
        async with make_harness(max_attempts=3) as h:
            # rate-limit every attempt -> retried up to max_attempts, then escalate.
            h.runner.default_script = [
                ErrorEvent(message="rl", classification="rate-limit"),
                Final(text="f", status=FAIL),
            ]
            await h.kernel.process_event(_qevent("go"))

            assert len(h.runner.opened) == 3
            assert h.sink.last_text is not None and "human" in h.sink.last_text.lower()

    asyncio.run(go())


def test_interrupt_hard_stops_the_live_turn(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="thinking")]
            h.runner.tail = [Final(text="stopped", status=IDLE)]

            e1 = _qevent("start", thread="tI")
            t1 = asyncio.create_task(h.kernel.process_event(e1))
            await _wait_until(lambda: h.runner.turn_active)

            signalled = await h.kernel.interrupt_thread("tI", "user stop")
            assert signalled is True
            assert h.runner.interrupts == 1

            await t1
            assert h.sink.last_text == "stopped"

    asyncio.run(go())


def test_duplicate_event_is_idempotent(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [Final(text="one", status=DONE)]
            ev = _qevent("hi", event_id="dup-1")
            await h.kernel.process_event(ev)
            await h.kernel.process_event(ev)  # same event id

            assert h.runner.opened == ["hi"]  # processed exactly once

    asyncio.run(go())


def test_ordering_preserved_open_then_steer(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="w")]
            h.runner.tail = [Final(text="done", status=DONE)]

            e1 = _qevent("first", thread="tO", event_id="o1")
            e2 = _qevent("second", thread="tO", event_id="o2")
            t1 = asyncio.create_task(h.kernel.process_event(e1))
            # Let the first event acquire the FIFO critical section and open the
            # turn, so the concurrent second event steers rather than forking.
            await _wait_until(lambda: h.runner.turn_active)
            t2 = asyncio.create_task(h.kernel.process_event(e2))
            await _wait_until(lambda: bool(h.runner.steers))

            assert h.runner.opened == ["first"]
            assert h.runner.steers == ["second"]

            hold.set()
            await asyncio.gather(t1, t2)

    asyncio.run(go())
