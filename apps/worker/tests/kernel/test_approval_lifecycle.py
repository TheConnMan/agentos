"""Approval-gate lifecycle tests (#244, ADR-0010): suspend on pending, resume
on resolve, durable across a worker restart.

Same harness discipline as the other kernel suites: real Valkey, the real
substrate over a fake Kubernetes client, an in-process fake ACI runner. Only
Slack, the model, and the approval API (a recording fake at the
``ApprovalCreator`` seam) are faked.
"""

from __future__ import annotations

import asyncio
import uuid

from aci_protocol import Final, QueuedTurn, ReplyHandle, SessionStatus, TextDelta
from agentos_worker.approvals import ApprovalBackendError, ApprovalRequest, CreatedApproval
from agentos_worker.sandbox.types import RouteState

DONE = SessionStatus.DONE
AWAITING = SessionStatus.AWAITING_APPROVAL


class RecordingApprovals:
    """An ApprovalCreator fake that records requests and mints stable ids."""

    def __init__(self, *, fail: bool = False) -> None:
        self.requests: list[ApprovalRequest] = []
        self.fail = fail

    async def create(self, request: ApprovalRequest) -> CreatedApproval:
        if self.fail:
            raise ApprovalBackendError("approval API unavailable")
        self.requests.append(request)
        return CreatedApproval(id=f"appr-{len(self.requests)}", status="pending")


def _qevent(
    text: str,
    *,
    thread: str = "th-appr",
    event_id: str | None = None,
    placeholder: str = "p-1",
) -> QueuedTurn:
    return QueuedTurn(
        event_id=event_id or uuid.uuid4().hex,
        conversation_id=thread,
        author="U1",
        text=text,
        reply_handle=ReplyHandle(channel="C1", placeholder=placeholder),
        received_at="2026-07-14T00:00:00+00:00",
    )


def _awaiting_script(summary: str) -> list:
    return [
        TextDelta(text="Requesting sign-off"),
        Final(text="Requesting sign-off", status=AWAITING, approval_summary=summary),
    ]


def test_awaiting_approval_creates_record_and_suspends(make_harness) -> None:
    async def go() -> None:
        approvals = RecordingApprovals()
        async with make_harness(approvals=approvals) as h:
            h.runner.default_script = _awaiting_script("Give ACME a 20% discount")
            ev = _qevent("please discount", event_id="ev-appr-1")
            await h.kernel.process_event(ev)

            # The durable record was created with the turn's identity: the
            # dedupe key is the event id and the reply handle rides along so a
            # resolution can resume into the same placeholder.
            assert len(approvals.requests) == 1
            req = approvals.requests[0]
            assert req.summary == "Give ACME a 20% discount"
            assert req.dedupe_key == "ev-appr-1"
            assert req.conversation_id == ev.conversation_id
            assert req.reply_channel == "C1"
            assert req.reply_placeholder == "p-1"
            assert req.author == "U1"

            # The sandbox was suspended and the route flipped to SUSPENDED.
            modes = [s.operating_mode for s in h.fake_k8s.sandboxes.values()]
            assert modes == ["Suspended"]
            record = h.substrate._affinity.get(ev.conversation_id)
            assert record is not None and record.state is RouteState.SUSPENDED

            # The placeholder carries the pending notice with the record id,
            # and the event is done (no retry loop).
            assert h.sink.last_text is not None
            assert "Awaiting approval (appr-1)" in h.sink.last_text
            assert "Give ACME a 20% discount" in h.sink.last_text
            assert await h.async_redis.exists(h.config.done_key(ev.event_id))

    asyncio.run(go())


def test_pending_state_survives_worker_restart_and_resumes_on_resolve(
    make_harness,
) -> None:
    """The epic's acceptance shape: suspend, replace every worker-side object
    (a fresh harness over the same Valkey routes), then deliver the resolution
    turn and watch the session resume and complete."""

    async def go() -> None:
        approvals = RecordingApprovals()
        thread = "th-restart"
        async with make_harness(approvals=approvals) as h:
            h.runner.default_script = _awaiting_script("Refund order 42")
            await h.kernel.process_event(_qevent("refund?", thread=thread))
            record = h.substrate._affinity.get(thread)
            assert record is not None and record.state is RouteState.SUSPENDED

        # "Restart": a brand-new kernel/substrate/runner (nothing in-process
        # survives) over the same Valkey affinity keys. The suspended route is
        # still there because it lives in Valkey, not worker memory.
        async with make_harness(approvals=approvals) as h2:
            record = h2.substrate._affinity.get(thread)
            assert record is not None and record.state is RouteState.SUSPENDED

            # The resolution turn (what the API enqueues on resolve): the
            # kernel must resume the suspended thread, boot a replacement
            # sandbox WITH the bound boot env, and run the turn to done.
            h2.runner.default_script = [
                Final(text="Refund processed.", status=DONE)
            ]
            resume_turn = _qevent(
                "[approval resolved] approved by U9", thread=thread, event_id="ev-resolve-1"
            )
            await h2.kernel.process_event(resume_turn)

            # The suspended claim was retired and a fresh one created; the
            # route is LIVE again and the reply landed.
            record = h2.substrate._affinity.get(thread)
            assert record is not None and record.state is RouteState.LIVE
            assert h2.sink.last_text == "Refund processed."
            assert h2.runner.opened == ["[approval resolved] approved by U9"]

    asyncio.run(go())


def test_resume_injects_boot_env_into_replacement_claim(make_harness) -> None:
    """The dormant-path fix: a resume must boot the replacement sandbox with
    the same bound env a fresh claim gets (bundle ref, budget), not a generic
    env -- the suspended pod is gone (ADR-0003) and env is all a boot has."""

    async def go() -> None:
        approvals = RecordingApprovals()
        async with make_harness(approvals=approvals) as h:
            thread = "th-envmerge"
            h.runner.default_script = _awaiting_script("Ship it")
            await h.kernel.process_event(_qevent("ship?", thread=thread))

            h.runner.default_script = [Final(text="Shipped.", status=DONE)]
            boot_env = {
                "AGENTOS_BUNDLE_REF": "bundles/agent-v7.tgz",
                "AGENTOS_BUDGET": '{"max_output_tokens_per_run": 1, "max_usd_per_day": 1.0}',
            }
            handle = await h.kernel._claim_or_resume(thread, boot_env)
            assert handle is not None

            resumed_env = h.fake_k8s.claim_envs[-1]
            assert resumed_env is not None
            assert resumed_env["AGENTOS_BUNDLE_REF"] == "bundles/agent-v7.tgz"
            assert "AGENTOS_BUDGET" in resumed_env
            # The substrate still guarantees session identity and a fresh
            # runner token on the replacement claim.
            assert resumed_env.get("AGENTOS_SESSION_ID")
            assert resumed_env.get("AGENTOS_RUNNER_TOKEN")

    asyncio.run(go())


class GrantBinding:
    """A binding stand-in that answers approval_grant_tool by event id (#430).

    resolve/boot_env behave like the routed double; approval_grant_tool returns
    the granted tool ONLY for the one resume event id it was configured with,
    mirroring the worker's real derivation from durable approval state.
    """

    def __init__(self, *, grant_event_id: str, grant_tool: str) -> None:
        self.grant_event_id = grant_event_id
        self.grant_tool = grant_tool
        self.agent_id = uuid.uuid4()

    async def resolve(self, channel: str):  # noqa: ANN201
        from agentos_worker.binding import ResolvedDeployment

        return ResolvedDeployment(
            agent_id=self.agent_id,
            version_id=uuid.uuid4(),
            version_label="v1",
            bundle_ref=None,
            max_usd_per_day=None,
            max_output_tokens_per_run=None,
        )

    def packs_for(self, resolved):  # noqa: ANN001, ANN201
        from agentos_worker.behaviorpacks import BehaviorPacks

        return BehaviorPacks.from_config(None)

    def budget_for(self, resolved):  # noqa: ANN001, ANN201
        from aci_protocol import Budget

        return Budget(max_output_tokens_per_run=1000, max_usd_per_day=1.0)

    def boot_env(self, resolved, thread_key):  # noqa: ANN001, ANN201
        return {"AGENTOS_SESSION_ID": f"s-{thread_key}"}

    async def approval_grant_tool(self, event_id: str, agent_id):  # noqa: ANN001, ANN201
        return self.grant_tool if event_id == self.grant_event_id else None


def test_resume_claim_injects_approval_grant_tool_env(make_harness) -> None:
    """#430: a resume claim for an approved permission-gate approval injects
    AGENTOS_APPROVAL_GRANT_TOOL into the boot env passed to the replacement
    claim; a fresh (non-approval) mention injects nothing (the gate re-arms)."""

    async def go() -> None:
        from agentos_api.resumequeue import resume_event_id

        grant_event = resume_event_id(uuid.uuid4())
        binding = GrantBinding(
            grant_event_id=grant_event, grant_tool="mcp__github__create_issue"
        )
        async with make_harness(binding=binding) as h:
            # The resume turn carries the approval resume event id -> the grant
            # for the approved tool lands in the boot env of the fresh claim.
            h.runner.default_script = [Final(text="Issue created.", status=DONE)]
            await h.kernel.process_event(
                _qevent(
                    "proceed with the approved action",
                    thread="th-grant",
                    event_id=grant_event,
                )
            )
            resumed_env = h.fake_k8s.claim_envs[-1]
            assert resumed_env is not None
            assert resumed_env.get("AGENTOS_APPROVAL_GRANT_TOOL") == "mcp__github__create_issue"

            # A fresh, unrelated mention has a different event id -> no grant env
            # (re-armed), so an adopted/warm follow-up cannot inherit an allowance.
            await h.kernel.process_event(
                _qevent("hello there", thread="th-fresh", event_id="ev-fresh-1")
            )
            fresh_env = h.fake_k8s.claim_envs[-1]
            assert fresh_env is not None
            assert "AGENTOS_APPROVAL_GRANT_TOOL" not in fresh_env

    asyncio.run(go())


def test_no_backend_escalates_instead_of_stranding(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:  # no approvals client wired
            h.runner.default_script = _awaiting_script("Anything")
            ev = _qevent("gate this")
            await h.kernel.process_event(ev)

            assert h.sink.last_text is not None
            assert "no approval backend" in h.sink.last_text
            # Not suspended: a pause nothing could resume would strand the thread.
            modes = [s.operating_mode for s in h.fake_k8s.sandboxes.values()]
            assert modes == ["Running"]
            assert await h.async_redis.exists(h.config.done_key(ev.event_id))

    asyncio.run(go())


def test_backend_failure_escalates_and_does_not_suspend(make_harness) -> None:
    async def go() -> None:
        approvals = RecordingApprovals(fail=True)
        async with make_harness(approvals=approvals) as h:
            h.runner.default_script = _awaiting_script("Anything")
            ev = _qevent("gate this")
            await h.kernel.process_event(ev)

            assert h.sink.last_text is not None
            assert "could not be created" in h.sink.last_text
            modes = [s.operating_mode for s in h.fake_k8s.sandboxes.values()]
            assert modes == ["Running"]
            assert await h.async_redis.exists(h.config.done_key(ev.event_id))

    asyncio.run(go())


def test_pause_posts_the_approval_card(make_harness) -> None:
    """#246: pausing posts a Block Kit card into the approval's thread whose
    buttons carry the record id, alongside the placeholder notice."""

    async def go() -> None:
        approvals = RecordingApprovals()
        async with make_harness(approvals=approvals) as h:
            h.runner.default_script = _awaiting_script("Give ACME a 20% discount")
            ev = _qevent("please discount", thread="th-card")
            await h.kernel.process_event(ev)

            assert len(h.sink.posts) == 1
            channel, fallback, blocks, thread_ts = h.sink.posts[0]
            assert channel == "C1"
            assert thread_ts == "th-card"
            assert "Give ACME a 20% discount" in fallback
            assert blocks is not None
            actions = blocks[-1]
            assert actions["type"] == "actions"
            assert [e["value"] for e in actions["elements"]] == ["appr-1", "appr-1"]

    asyncio.run(go())


def test_escalation_paths_post_no_card(make_harness) -> None:
    async def go() -> None:
        async with make_harness() as h:  # no approvals backend wired
            h.runner.default_script = _awaiting_script("Anything")
            await h.kernel.process_event(_qevent("gate this"))
            assert h.sink.posts == []

    asyncio.run(go())


def _awaiting_routed_script(summary: str, route: str) -> list:
    return [
        TextDelta(text="Requesting sign-off"),
        Final(
            text="Requesting sign-off",
            status=AWAITING,
            approval_summary=summary,
            approval_route=route,
        ),
    ]


class RoutedBinding:
    """A minimal binding stand-in: one channel -> one agent with route bindings."""

    def __init__(self, routes: dict | None) -> None:
        self.routes = routes
        self.agent_id = uuid.uuid4()

    async def resolve(self, channel: str):  # noqa: ANN201
        from agentos_worker.binding import ResolvedDeployment

        return ResolvedDeployment(
            agent_id=self.agent_id,
            version_id=uuid.uuid4(),
            version_label="v1",
            bundle_ref=None,
            max_usd_per_day=None,
            max_output_tokens_per_run=None,
            approval_routes=self.routes,
        )

    def packs_for(self, resolved):  # noqa: ANN001, ANN201
        from agentos_worker.behaviorpacks import BehaviorPacks

        return BehaviorPacks.from_config(None)

    def budget_for(self, resolved):  # noqa: ANN001, ANN201
        from aci_protocol import Budget

        return Budget(max_output_tokens_per_run=1000, max_usd_per_day=1.0)

    def boot_env(self, resolved, thread_key):  # noqa: ANN001, ANN201
        return {"AGENTOS_SESSION_ID": f"s-{thread_key}"}


def test_routed_approval_cards_go_to_the_bound_channel(make_harness) -> None:
    """#247: the manifest route resolves through the agent's bindings; the card
    lands in the bound channel (top-level, no foreign thread) and the record
    carries route + card_channel so the authorizer counts THAT channel."""

    async def go() -> None:
        approvals = RecordingApprovals()
        binding = RoutedBinding({"managers": {"channel": "C_MGRS"}})
        async with make_harness(approvals=approvals, binding=binding) as h:
            h.runner.default_script = _awaiting_routed_script(
                "Discount for ACME", "managers"
            )
            await h.kernel.process_event(_qevent("discount?", thread="th-routed"))

            req = approvals.requests[0]
            assert req.route == "managers"
            assert req.card_channel == "C_MGRS"
            # Card posted to the bound channel, top-level (no thread there).
            channel, _fallback, blocks, thread_ts = h.sink.posts[0]
            assert channel == "C_MGRS"
            assert thread_ts is None
            assert blocks is not None

    asyncio.run(go())


def test_unbound_route_falls_back_to_requesting_channel(make_harness) -> None:
    async def go() -> None:
        approvals = RecordingApprovals()
        binding = RoutedBinding(None)  # agent has no bindings at all
        async with make_harness(approvals=approvals, binding=binding) as h:
            h.runner.default_script = _awaiting_routed_script("Anything", "managers")
            await h.kernel.process_event(_qevent("gate", thread="th-unbound"))

            req = approvals.requests[0]
            assert req.route == "managers"
            assert req.card_channel == "C1"  # fell back to the requesting channel
            channel, _f, _b, thread_ts = h.sink.posts[0]
            assert channel == "C1"
            assert thread_ts == "th-unbound"  # same channel keeps the thread

    asyncio.run(go())


def test_routeless_approval_keeps_prior_behavior(make_harness) -> None:
    async def go() -> None:
        approvals = RecordingApprovals()
        async with make_harness(approvals=approvals) as h:
            h.runner.default_script = _awaiting_script("Plain request")
            await h.kernel.process_event(_qevent("gate", thread="th-plain"))

            req = approvals.requests[0]
            assert req.route is None
            assert req.card_channel == "C1"

    asyncio.run(go())
