"""Kernel-level F2 tests: deployment binding + kill switch, against real Valkey,
the real substrate, and a fake runner. The Postgres resolution SQL is tested
separately (tests/binding); here a stub binding supplies canned resolutions so
the kernel behaviors are exercised deterministically."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable

from aci_protocol import Final, SessionStatus, TextDelta
from agentos_dispatcher.queue import QueuedSlackEvent
from agentos_worker.behaviorpacks import BehaviorPacks
from agentos_worker.binding import (
    AGENT_ID_ENV,
    BUDGET_ENV,
    BUNDLE_REF_ENV,
    PLUGIN_DIR_ENV,
    ResolvedDeployment,
)
from agentos_worker.killswitch import kill_key

DONE = SessionStatus.DONE
IDLE = SessionStatus.IDLE_AWAITING_INPUT


class StubBinding:
    """A BindingResolver-shaped stub with canned per-channel resolutions."""

    def __init__(self, by_channel: dict[str, ResolvedDeployment]) -> None:
        self._by_channel = by_channel

    async def resolve(self, channel: str) -> ResolvedDeployment | None:
        return self._by_channel.get(channel)

    def boot_env(self, resolved: ResolvedDeployment, thread_key: str) -> dict[str, str]:
        env = {
            BUDGET_ENV: '{"max_output_tokens_per_run":100000,"max_usd_per_day":10.0}',
            AGENT_ID_ENV: str(resolved.agent_id),
            PLUGIN_DIR_ENV: "/bundles/current",
        }
        if resolved.bundle_ref is not None:
            env[BUNDLE_REF_ENV] = resolved.bundle_ref
        return env

    def packs_for(self, resolved: ResolvedDeployment) -> BehaviorPacks:
        return BehaviorPacks.from_config(resolved.behavior_packs)


def _resolved(agent_id: uuid.UUID, *, bundle: str | None = "bundles/x.zip") -> ResolvedDeployment:
    return ResolvedDeployment(
        agent_id=agent_id,
        version_id=uuid.uuid4(),
        version_label="v1",
        bundle_ref=bundle,
        max_usd_per_day=None,
        max_output_tokens_per_run=None,
    )


def _qevent(text: str, *, channel: str, thread: str = "th-1") -> QueuedSlackEvent:
    return QueuedSlackEvent(
        slack_event_id=uuid.uuid4().hex,
        thread_ts=thread,
        channel=channel,
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


def test_unmapped_channel_is_a_polite_drop(make_harness) -> None:
    async def go() -> None:
        async with make_harness(binding=StubBinding({})) as h:
            h.runner.default_script = [Final(text="hi", status=DONE)]
            ev = _qevent("hello", channel="C-unknown")
            await h.kernel.process_event(ev)

            assert h.runner.opened == []  # no turn ever opened
            assert h.sink.last_text is not None and "no agent" in h.sink.last_text.lower()
            assert await h.async_redis.exists(h.config.done_key(ev.slack_event_id))

    asyncio.run(go())


def test_bound_channel_claims_sandbox_with_boot_env(make_harness) -> None:
    async def go() -> None:
        agent_id = uuid.uuid4()
        resolved = _resolved(agent_id, bundle="bundles/x.zip")
        binding = StubBinding({"C-bound": resolved})
        async with make_harness(binding=binding) as h:
            h.runner.default_script = [Final(text="answer", status=DONE)]
            await h.kernel.process_event(_qevent("hi", channel="C-bound", thread="th-1"))

            assert h.runner.opened == ["hi"]
            assert h.sink.last_text == "answer"
            # The sandbox was claimed WITH exactly the resolved boot env
            # (BUNDLE_REF / PLUGIN_DIR / BUDGET / AGENT_ID), unmodified.
            env = h.fake_k8s.claim_envs[-1]
            assert env == binding.boot_env(resolved, "th-1")
            assert env is not None
            assert env[BUNDLE_REF_ENV] == "bundles/x.zip"
            assert env[AGENT_ID_ENV] == str(agent_id)
            assert env[PLUGIN_DIR_ENV] == "/bundles/current"
            assert "max_usd_per_day" in env[BUDGET_ENV]

    asyncio.run(go())


def test_killed_agent_refuses_new_runs(make_harness) -> None:
    async def go() -> None:
        agent_id = uuid.uuid4()
        binding = StubBinding({"C-bound": _resolved(agent_id)})
        async with make_harness(binding=binding, with_killswitch=True) as h:
            await h.async_redis.set(kill_key(agent_id), "1")  # operator killed it
            h.runner.default_script = [Final(text="answer", status=DONE)]

            await h.kernel.process_event(_qevent("hi", channel="C-bound"))

            assert h.runner.opened == []  # refused before opening a turn
            assert h.sink.last_text is not None and "paused" in h.sink.last_text.lower()

    asyncio.run(go())


class _StubKillSwitch:
    """Scripted is_killed: returns the sequence in order (last value repeats)."""

    def __init__(self, killed_sequence: list[bool]) -> None:
        self._seq = list(killed_sequence)
        self.calls = 0

    async def is_killed(self, _agent_id: uuid.UUID) -> bool:
        value = self._seq[min(self.calls, len(self._seq) - 1)]
        self.calls += 1
        return value


def test_kill_between_precheck_and_register_is_caught(make_harness) -> None:
    async def go() -> None:
        agent_id = uuid.uuid4()
        binding = StubBinding({"C-bound": _resolved(agent_id)})
        async with make_harness(binding=binding) as h:
            # Precheck sees the agent alive; by the time the turn is registered the
            # kill has landed. The post-register recheck must interrupt it.
            h.kernel.attach_killswitch(_StubKillSwitch([False, True]))
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="working")]
            h.runner.tail = [Final(text="stopped", status=IDLE)]

            await h.kernel.process_event(_qevent("hi", channel="C-bound", thread="tRace"))

            assert h.runner.interrupts == 1  # the just-opened turn was interrupted

    asyncio.run(go())


def test_kill_interrupts_a_live_turn(make_harness) -> None:
    async def go() -> None:
        agent_id = uuid.uuid4()
        binding = StubBinding({"C-bound": _resolved(agent_id)})
        async with make_harness(binding=binding, with_killswitch=True) as h:
            hold = asyncio.Event()
            h.runner.hold = hold
            h.runner.default_script = [TextDelta(text="working")]
            h.runner.tail = [Final(text="stopped", status=IDLE)]

            ev = _qevent("hi", channel="C-bound", thread="tK")
            t1 = asyncio.create_task(h.kernel.process_event(ev))
            await _wait_until(lambda: h.runner.turn_active)

            # Killing the agent interrupts its registered live turn.
            signalled = await h.kernel.interrupt_agent(agent_id)
            assert signalled == 1
            assert h.runner.interrupts == 1

            await t1
            assert h.sink.last_text == "stopped"

    asyncio.run(go())


def _resolved_with_packs(behavior_packs: dict) -> ResolvedDeployment:
    return ResolvedDeployment(
        agent_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        version_label="v1",
        bundle_ref="bundles/x.zip",
        max_usd_per_day=None,
        max_output_tokens_per_run=None,
        behavior_packs=behavior_packs,
    )


def test_shimmer_caption_uses_the_agents_load_pack(make_harness) -> None:
    # Connector: with shimmer on, the kernel sets the assistant status to the
    # agent's sampled load line (+ tip), not the dispatcher's generic text.
    async def go() -> None:
        packs = {
            "load": {"enabled": True, "lines": ["Crunching the numbers..."]},
            "tips": {"enabled": True, "tips": ["I can rank leaks by $"]},
        }
        binding = StubBinding({"C-bound": _resolved_with_packs(packs)})
        async with make_harness(binding=binding, shimmer=True) as h:
            h.runner.default_script = [Final(text="done", status=DONE)]
            await h.kernel.process_event(_qevent("hi", channel="C-bound", thread="tSh"))
            assert h.sink.status_sets, "expected a shimmer caption to be set"
            _, thread_ts, caption = h.sink.status_sets[-1]
            assert thread_ts == "tSh"
            assert caption == "Crunching the numbers...\n\nTip: I can rank leaks by $"

    asyncio.run(go())


def test_shimmer_no_caption_when_agent_has_no_load_or_tips(make_harness) -> None:
    # Shimmer on but the agent enables neither pack: the kernel sets no caption,
    # so the dispatcher's generic status stays.
    async def go() -> None:
        binding = StubBinding({"C-bound": _resolved_with_packs({})})
        async with make_harness(binding=binding, shimmer=True) as h:
            h.runner.default_script = [Final(text="done", status=DONE)]
            await h.kernel.process_event(_qevent("hi", channel="C-bound"))
            assert h.sink.status_sets == []


def test_shimmer_off_never_sets_a_caption(make_harness) -> None:
    async def go() -> None:
        packs = {"load": {"enabled": True, "lines": ["Working..."]}}
        binding = StubBinding({"C-bound": _resolved_with_packs(packs)})
        async with make_harness(binding=binding) as h:  # shimmer defaults off
            h.runner.default_script = [Final(text="done", status=DONE)]
            await h.kernel.process_event(_qevent("hi", channel="C-bound"))
            assert h.sink.status_sets == []

    asyncio.run(go())
