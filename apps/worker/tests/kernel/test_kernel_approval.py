"""Kernel awaiting-approval lifecycle (#22): the sacred-module slice.

Against real Valkey, the real substrate, and the fake runner. The runner scripts
an ``awaiting-approval`` final carrying the gated tool call; the kernel must
persist a durable pending approval, suspend the session, and mark the event done
(so it is not replayed). The ApprovalStore is a recording double here on purpose:
this suite is Valkey-only by design (like the binding stub), and the store's real
Postgres SQL is exercised against a migrated database in the API suite
(test_approvals.py). What is under test is the kernel's orchestration, not the SQL.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from aci_protocol import (
    ApprovalRequest,
    Final,
    QueuedTurn,
    ReplyHandle,
    SessionStatus,
    TextDelta,
)
from agentos_worker.behaviorpacks import BehaviorPacks
from agentos_worker.binding import AGENT_ID_ENV, BUDGET_ENV, PLUGIN_DIR_ENV, ResolvedDeployment


class StubBinding:
    """A BindingResolver-shaped stub with canned per-channel resolutions."""

    def __init__(self, by_channel: dict[str, ResolvedDeployment]) -> None:
        self._by_channel = by_channel

    async def resolve(self, channel: str) -> ResolvedDeployment | None:
        return self._by_channel.get(channel)

    def boot_env(self, resolved: ResolvedDeployment, thread_key: str) -> dict[str, str]:
        return {
            BUDGET_ENV: '{"max_output_tokens_per_run":100000,"max_usd_per_day":10.0}',
            AGENT_ID_ENV: str(resolved.agent_id),
            PLUGIN_DIR_ENV: "/bundles/current",
        }

    def packs_for(self, resolved: ResolvedDeployment) -> BehaviorPacks:
        return BehaviorPacks.from_config(resolved.behavior_packs)


class FakeApprovalStore:
    """Records create_pending calls; the kernel's only store call on suspend."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    async def create_pending(self, **kwargs: Any) -> uuid.UUID:
        self.created.append(kwargs)
        return uuid.uuid4()


def _resolved(agent_id: uuid.UUID) -> ResolvedDeployment:
    return ResolvedDeployment(
        agent_id=agent_id,
        version_id=uuid.uuid4(),
        version_label="v1",
        bundle_ref="bundles/x.zip",
        max_usd_per_day=None,
        max_output_tokens_per_run=None,
    )


def _qevent(text: str, *, channel: str, thread: str, placeholder: str) -> QueuedTurn:
    return QueuedTurn(
        event_id=uuid.uuid4().hex,
        conversation_id=thread,
        author="U1",
        text=text,
        reply_handle=ReplyHandle(channel=channel, placeholder=placeholder),
        received_at="2026-07-13T00:00:00+00:00",
    )


def _awaiting_script() -> list[Any]:
    return [
        TextDelta(text="I need sign-off to apply this discount."),
        Final(
            text="Requesting approval.",
            status=SessionStatus.AWAITING_APPROVAL,
            session_id="sdk-sess-1",
            approval_request=ApprovalRequest(
                tool="apply_discount",
                tool_use_id="toolu_42",
                input_digest="sha256:abc",
                prompt="Apply a 30% discount to ACME-1?",
            ),
        ),
    ]


def test_awaiting_approval_persists_and_suspends(make_harness) -> None:
    async def go() -> None:
        agent_id = uuid.uuid4()
        store = FakeApprovalStore()
        binding = StubBinding({"C-bound": _resolved(agent_id)})
        async with make_harness(binding=binding, approvals=store) as h:
            h.runner.turn_scripts = [_awaiting_script()]
            ev = _qevent(
                "give acme a 30% discount",
                channel="C-bound",
                thread="th-appr",
                placeholder="p-appr",
            )
            await h.kernel.process_event(ev)

            # A durable pending approval was persisted with the gate + routing so
            # the resume path can rebuild the turn without the live sandbox.
            assert len(store.created) == 1
            rec = store.created[0]
            assert rec["agent_id"] == agent_id
            assert rec["conversation_id"] == "th-appr"
            assert rec["tool"] == "apply_discount"
            assert rec["tool_use_id"] == "toolu_42"
            assert rec["prompt"] == "Apply a 30% discount to ACME-1?"
            assert rec["session_id"] == "sdk-sess-1"
            assert rec["channel"] == "C-bound"
            assert rec["reply_placeholder"] == "p-appr"
            assert rec["requested_by"] == "U1"

            # The session was suspended: the pod flipped to Suspended in the fake
            # control plane (real suspend deletes it; the fake flips the mode).
            assert any(
                s.operating_mode == "Suspended" for s in h.fake_k8s.sandboxes.values()
            )

            # Terminally handled (marked done, not replayed); the placeholder shows
            # the paused state, never the model's interim text as a final answer.
            assert await h.async_redis.exists(h.config.done_key(ev.event_id))
            assert h.sink.last_text == h.config.awaiting_approval_text

    asyncio.run(go())


def test_awaiting_approval_without_store_escalates(make_harness) -> None:
    async def go() -> None:
        agent_id = uuid.uuid4()
        binding = StubBinding({"C-bound": _resolved(agent_id)})
        # No approvals store wired: the gate cannot be persisted, so the kernel
        # escalates to a human rather than silently dropping the request.
        async with make_harness(binding=binding, approvals=None) as h:
            h.runner.turn_scripts = [_awaiting_script()]
            ev = _qevent(
                "discount please", channel="C-bound", thread="th-x", placeholder="p-x"
            )
            await h.kernel.process_event(ev)

            assert h.sink.last_text is not None
            assert "approval" in h.sink.last_text.lower()
            assert "not configured" in h.sink.last_text.lower()
            # Not suspended, but still terminally handled (no infinite reclaim).
            assert all(
                s.operating_mode == "Running" for s in h.fake_k8s.sandboxes.values()
            )
            assert await h.async_redis.exists(h.config.done_key(ev.event_id))

    asyncio.run(go())
