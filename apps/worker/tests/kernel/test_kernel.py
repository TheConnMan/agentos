"""Kernel rule tests: routing, steer, finish-race, interrupt, side-effect/retry.

Each rule is provoked against a real Valkey, the real G1 substrate, and a
scriptable in-process fake runner; only Slack and the model are faked.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable

from aci_protocol import (
    ErrorEvent,
    Final,
    QueuedTurn,
    ReplyHandle,
    SessionStatus,
    SideEffectFlag,
    TextDelta,
)
from agentos_worker.behaviorpacks import BehaviorPacks, NavPack

DONE = SessionStatus.DONE
IDLE = SessionStatus.IDLE_AWAITING_INPUT
FAIL = SessionStatus.CLASSIFIED_FAILURE


def _qevent(
    text: str,
    *,
    thread: str = "th-1",
    event_id: str | None = None,
    placeholder: str = "p-1",
    endpoint: str | None = None,
) -> QueuedTurn:
    return QueuedTurn(
        event_id=event_id or uuid.uuid4().hex,
        conversation_id=thread,
        author="U1",
        text=text,
        reply_handle=ReplyHandle(channel="C1", placeholder=placeholder, endpoint=endpoint),
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
            assert await h.async_redis.exists(h.config.done_key(ev.event_id))

    asyncio.run(go())


def test_shimmer_clears_status_when_the_turn_ends(make_harness) -> None:
    # With shimmer on, the kernel clears the assistant-thread status the
    # dispatcher set, on the turn's terminal exit (a plain success here).
    async def go() -> None:
        async with make_harness(shimmer=True) as h:
            h.runner.default_script = [Final(text="done", status=DONE)]
            await h.kernel.process_event(_qevent("hi", thread="tS"))
            assert ("C1", "tS") in h.sink.status_clears

    asyncio.run(go())


def test_no_status_clear_when_shimmer_is_off(make_harness) -> None:
    # Default (shimmer off): the kernel never touches the assistant status.
    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [Final(text="done", status=DONE)]
            await h.kernel.process_event(_qevent("hi"))
            assert h.sink.status_clears == []

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
            assert await h.async_redis.exists(h.config.side_effect_key(ev.event_id))
            assert await h.async_redis.exists(h.config.done_key(ev.event_id))

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


def test_turn_start_failure_is_retryable_not_a_stall(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            # The first /v1/event returns 500 (transient runner error / not ready).
            # This must be turned into a bounded retry, not escape and leave the
            # entry pending for the long reclaim window.
            h.runner.event_fail_times = 1
            h.runner.default_script = [Final(text="recovered", status=DONE)]

            await h.kernel.process_event(_qevent("go"))

            assert h.runner.opened == ["go", "go"]  # failed start, then retried
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


def test_ordering_preserved_under_concurrent_sends(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="w")]
            h.runner.tail = [Final(text="done", status=DONE)]

            # Both events for the same thread are dispatched concurrently, with no
            # pre-sequencing: the FIFO in-process lock must make the first-created
            # event open the turn and the second steer into it. Without that lock
            # the order (and whether a second turn is forked) would be a race, so
            # this asserts the ordering guarantee, not just that steering works.
            e1 = _qevent("first", thread="tO", event_id="o1")
            e2 = _qevent("second", thread="tO", event_id="o2")
            t1 = asyncio.create_task(h.kernel.process_event(e1))
            t2 = asyncio.create_task(h.kernel.process_event(e2))
            await _wait_until(lambda: h.runner.turn_active and bool(h.runner.steers))

            assert h.runner.opened == ["first"]  # exactly one turn, the first event
            assert h.runner.steers == ["second"]  # the second folded in as a steer

            hold.set()
            await asyncio.gather(t1, t2)

    asyncio.run(go())


def test_prior_side_effect_marker_escalates_without_running(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            ev = _qevent("retry me", event_id="se-1")
            # A prior attempt executed a side effect then the worker crashed: the
            # marker is set but the event never reached done. It must escalate,
            # never re-run the non-idempotent action.
            await h.async_redis.set(h.config.side_effect_key(ev.event_id), "1")

            await h.kernel.process_event(ev)

            assert h.runner.opened == []  # no turn was ever opened
            assert h.sink.last_text is not None and "human" in h.sink.last_text.lower()
            assert await h.async_redis.exists(h.config.done_key(ev.event_id))

    asyncio.run(go())


def test_suspended_thread_is_resumed_not_forked(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [Final(text="one", status=DONE)]
            await h.kernel.process_event(_qevent("first", thread="tR"))

            # Suspend the thread (records a rehydrate ref on the route).
            await asyncio.to_thread(h.substrate.suspend, "tR", history_ref="hist-1")

            # A new event on a suspended thread must resume (carry the history)
            # rather than silently fork a fresh, history-less session.
            h.runner.default_script = [Final(text="resumed", status=DONE)]
            await h.kernel.process_event(_qevent("second", thread="tR"))

            assert h.runner.opened == ["first", "second"]
            assert h.sink.last_text == "resumed"

    asyncio.run(go())


async def _route_key(async_redis, thread: str) -> str:
    keys = [k async for k in async_redis.scan_iter(match=f"*:route:{thread}")]
    assert len(keys) == 1, f"expected one route key for {thread}, found {keys}"
    return keys[0]


def test_live_route_reuse_refreshes_ttl(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            # First event creates a live route with the substrate's route TTL.
            h.runner.default_script = [Final(text="one", status=DONE)]
            await h.kernel.process_event(_qevent("first", thread="tTTL"))

            route_key = await _route_key(h.async_redis, "tTTL")
            # Simulate time passing by dropping the TTL low.
            await h.async_redis.expire(route_key, 5)
            assert await h.async_redis.ttl(route_key) <= 5

            # A second event reuses the live route; routing through claim() must
            # refresh the TTL (a regression to lookup() would leave it at ~5 and
            # let the reaper delete a busy thread's sandbox).
            h.runner.default_script = [Final(text="two", status=DONE)]
            await h.kernel.process_event(_qevent("second", thread="tTTL"))
            assert await h.async_redis.ttl(route_key) > 5

    asyncio.run(go())


def test_steered_followup_placeholder_is_retired(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="w")]
            h.runner.tail = [Final(text="done", status=DONE)]

            e1 = _qevent("first", thread="tPH", placeholder="ph-1")
            t1 = asyncio.create_task(h.kernel.process_event(e1))
            await _wait_until(lambda: h.runner.turn_active)

            # The follow-up carries its own placeholder; once steered, that
            # placeholder must be retired (not left stuck on "working").
            e2 = _qevent("second", thread="tPH", placeholder="ph-2")
            await h.kernel.process_event(e2)

            folded = [u for u in h.sink.updates if u[1] == "ph-2"]
            assert folded, "the steered follow-up's placeholder was never updated"
            assert "folded" in folded[-1][2].lower()

            hold.set()
            await t1

    asyncio.run(go())


def test_order_lock_map_evicts_after_processing(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [Final(text="ok", status=DONE)]
            await h.kernel.process_event(_qevent("hi", thread="tEV"))
            # Ref-counted eviction: no per-thread lock entry lingers once the last
            # holder releases (a regression would leak one entry per thread seen).
            assert h.kernel._order_locks == {}

    asyncio.run(go())


# --- Per-sandbox runner token delivery, end-to-end (issue #63) ----------------


class _FakeResolved:
    """The minimal resolved deployment the kernel reads (agent_id only; shimmer
    off, so packs are never sampled)."""

    def __init__(self, agent_id: uuid.UUID) -> None:
        self.agent_id = agent_id


class _TokenBinding:
    """A binding whose boot_env injects a known runner token into the claim env,
    so the test can assert the exact value the worker delivers as the Bearer
    header. The claim-time minting itself is covered by the binding unit tests;
    this proves the claim->handle->kernel->runner delivery path."""

    def __init__(self, token: str, agent_id: uuid.UUID) -> None:
        self._token = token
        self._agent_id = agent_id

    async def resolve(self, _channel: str) -> _FakeResolved:
        return _FakeResolved(self._agent_id)

    def boot_env(self, _resolved: object, _thread_key: str) -> dict[str, str]:
        return {"AGENTOS_RUNNER_TOKEN": self._token}

    def packs_for(self, _resolved: object) -> BehaviorPacks:
        return BehaviorPacks()


def test_kernel_delivers_claim_token_as_bearer_header(make_harness) -> None:
    async def go() -> None:
        binding = _TokenBinding("tok-24", uuid.uuid4())
        async with make_harness(binding=binding) as h:
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="w")]
            h.runner.tail = [Final(text="done", status=DONE)]

            e1 = _qevent("first", thread="tTok")
            t1 = asyncio.create_task(h.kernel.process_event(e1))
            await _wait_until(lambda: h.runner.turn_active)

            # Event path: the opening /v1/event carried the claim-minted token.
            assert h.runner.event_headers
            assert h.runner.event_headers[-1].get("Authorization") == "Bearer tok-24"

            # Steer path: a follow-up folded into the live turn carries it too.
            await h.kernel.process_event(_qevent("second", thread="tTok"))
            assert h.runner.steer_headers
            assert h.runner.steer_headers[-1].get("Authorization") == "Bearer tok-24"

            # Interrupt path: the explicit hard stop carries it as well.
            await h.kernel.interrupt_thread("tTok", "user stop")
            assert h.runner.interrupt_headers
            assert h.runner.interrupt_headers[-1].get("Authorization") == "Bearer tok-24"

            hold.set()
            await t1

    asyncio.run(go())


# --- #31: no-edit streaming mode ----------------------------------------------

_MULTI_DELTA = [
    TextDelta(text="a"),
    TextDelta(text="b"),
    TextDelta(text="c"),
    Final(text="abc final", status=DONE),
]


def test_no_edit_streaming_edits_placeholder_once(make_harness) -> None:
    async def go() -> None:
        async with make_harness(slack_no_edit_streaming=True) as h:
            # Multiple TextDeltas stream, but in no-edit mode the placeholder is
            # edited EXACTLY once -- the final. No intermediate chat.update calls.
            h.runner.default_script = list(_MULTI_DELTA)
            await h.kernel.process_event(_qevent("go"))

            assert len(h.sink.updates) == 1
            assert h.sink.last_text == "abc final"

    asyncio.run(go())


def test_default_streaming_edits_more_than_once(make_harness) -> None:
    async def go() -> None:
        # Deletion-test guard: with no-edit OFF (default; conftest sets
        # slack_edit_min_interval_s=0.0) the SAME multi-delta script produces
        # more than one edit, proving the flag actually changes behavior.
        async with make_harness() as h:
            h.runner.default_script = list(_MULTI_DELTA)
            await h.kernel.process_event(_qevent("go"))

            assert len(h.sink.updates) > 1
            assert h.sink.last_text == "abc final"

    asyncio.run(go())


def test_booting_state_edits_placeholder_before_answer(make_harness) -> None:
    # A fresh-claim turn edits the placeholder to the booting caption at the very
    # start of the attempt, before the sandbox-claim wait, so the "booting a
    # runner" state is visible ahead of the streamed answer on the same message.
    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [
                TextDelta(text="Hello "),
                TextDelta(text="world"),
                Final(text="Hello world", status=DONE),
            ]
            ev = _qevent("hi", thread="tBOOT", placeholder="ph-boot")
            await h.kernel.process_event(ev)

            booting = h.config.booting_text
            on_ph = [
                (i, u)
                for i, u in enumerate(h.sink.updates)
                if u[0] == ev.reply_handle.channel and u[1] == ev.reply_handle.placeholder
            ]
            booting_idxs = [i for i, u in on_ph if u[2] == booting]
            answer_idxs = [i for i, u in on_ph if u[2] != booting]
            assert booting_idxs, "the booting caption was never edited onto the placeholder"
            assert answer_idxs, "no streamed-answer update landed on the placeholder"
            assert min(booting_idxs) < min(answer_idxs), (
                "the booting caption must precede the first streamed-answer update"
            )

    asyncio.run(go())


def test_reply_endpoint_is_threaded_to_the_sink(make_harness) -> None:
    # Issue #19: a turn carrying a per-turn reply endpoint must route every sink
    # edit for that turn through that endpoint (not the worker default), so a
    # no-Slack CLI stub and a real workspace can coexist on one worker.
    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [
                TextDelta(text="working "),
                Final(text="done", status=DONE),
            ]
            await h.kernel.process_event(
                _qevent("hi", thread="tEP", endpoint="http://stub:8155/api/")
            )

            assert h.sink.last_text == "done"
            # Every recorded update for this turn carried the per-turn endpoint.
            assert h.sink.update_endpoints, "no sink update recorded"
            assert set(h.sink.update_endpoints) == {"http://stub:8155/api/"}

    asyncio.run(go())


def test_reply_endpoint_defaults_to_none_for_the_worker_default(make_harness) -> None:
    # A turn with no per-turn endpoint threads None, so the sink uses its worker
    # default (the pre-#19 behavior is preserved for real-Slack ingress).
    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [Final(text="ok", status=DONE)]
            await h.kernel.process_event(_qevent("hi", thread="tEPNONE"))
            assert set(h.sink.update_endpoints) == {None}

    asyncio.run(go())


def test_booting_update_failure_never_fails_the_turn(make_harness) -> None:
    # The booting edit is best-effort: if the Slack update for the booting caption
    # raises, the turn still runs to its normal terminal answer. Inject a failure
    # on the first booting-caption update and prove both that it fired and that the
    # turn completed anyway.
    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [Final(text="all good", status=DONE)]

            booting = h.config.booting_text
            original_update = h.sink.update
            fired = {"n": 0}

            async def flaky_update(
                *,
                channel: str,
                ts: str,
                text: str,
                nav: NavPack | None = None,
                endpoint: str | None = None,
                best_effort_unreachable: bool = False,
            ) -> None:
                if text == booting and fired["n"] == 0:
                    fired["n"] += 1
                    raise RuntimeError("injected Slack failure on booting update")
                await original_update(
                    channel=channel,
                    ts=ts,
                    text=text,
                    nav=nav,
                    endpoint=endpoint,
                    best_effort_unreachable=best_effort_unreachable,
                )

            h.sink.update = flaky_update  # type: ignore[method-assign]

            ev = _qevent("hi", thread="tBOOTFAIL", placeholder="ph-boot-fail")
            await h.kernel.process_event(ev)

            assert fired["n"] > 0, "the booting update was never attempted"
            assert h.sink.last_text == "all good"
            assert await h.async_redis.exists(h.config.done_key(ev.event_id))

    asyncio.run(go())


def test_release_thread_force_releases_a_live_route(make_harness) -> None:
    """#713: an operator can force-release a thread's sandbox even though it
    has a live (not suspended, not dead) route -- the whole point is to evict
    a sandbox that is up and answering but running stale env, not just one
    that already died on its own (that path -- claim()'s stale-sandbox
    eviction -- already existed)."""

    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [Final(text="hi", status=DONE)]
            await h.kernel.process_event(_qevent("hi", thread="tRelease"))
            assert h.substrate.lookup("tRelease") is not None  # the route is live

            released = await h.kernel.release_thread("tRelease")
            assert released is True
            assert h.substrate.lookup("tRelease") is None  # gone: next claim is fresh

    asyncio.run(go())


def test_release_thread_interrupts_a_live_turn_first(make_harness) -> None:
    """Releasing a thread mid-turn interrupts it first rather than yanking the
    claim out from under a running turn silently."""

    async def go() -> None:
        async with make_harness() as h:
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="thinking")]
            h.runner.tail = [Final(text="stopped", status=IDLE)]

            e1 = _qevent("start", thread="tReleaseMidTurn")
            t1 = asyncio.create_task(h.kernel.process_event(e1))
            await _wait_until(lambda: h.runner.turn_active)

            released = await h.kernel.release_thread("tReleaseMidTurn")
            assert released is True
            assert h.runner.interrupts == 1  # interrupted, not silently abandoned

            hold.set()
            await t1

    asyncio.run(go())


def test_release_thread_with_no_route_is_a_noop(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:
            released = await h.kernel.release_thread("never-seen-thread")
            assert released is False

    asyncio.run(go())


def test_claim_latency_is_logged(make_harness, caplog) -> None:
    """#718: the claim wait (cold sandbox boot vs. an adopted warm one) is
    logged separately from the model turn's own duration -- the runner's own
    per-turn logging starts only once its process is already up, so it has no
    visibility into how long the worker waited to get it there. Both a fresh
    claim and a steer onto a live turn go through the same timed call, so both
    are covered by one assertion on the log line's presence and shape."""

    async def go() -> None:
        async with make_harness() as h:
            h.runner.default_script = [Final(text="hi", status=DONE)]
            with caplog.at_level(logging.INFO, logger="agentos_worker.kernel"):
                await h.kernel.process_event(_qevent("hi", thread="tLatency"))

            matches = [
                r.getMessage()
                for r in caplog.records
                if "claim latency for tLatency" in r.getMessage()
            ]
            assert matches, caplog.text
            # "claim latency for tLatency: <N> ms" -- non-negative integer duration.
            ms = int(matches[0].rsplit(":", 1)[1].strip().split()[0])
            assert ms >= 0

    asyncio.run(go())
