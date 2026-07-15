"""Approval-request tests (#244, ADR-0010): the runner half of the gate.

Covers the wire-level capture in translation (one seam for real and fake
models), the session's awaiting-approval final override, its precedence rules
(failure, interrupt, and budget outrank a pending approval), and the fake
model's offline approval script.
"""

from __future__ import annotations

import json

import anyio
from aci_protocol import Event, Final, SessionStatus
from agentos_runner.adapter import build_options
from agentos_runner.approval import (
    APPROVAL_TOOL_NAME,
    ApprovalGate,
    build_approval_server,
    build_can_use_tool,
    summarize_tool_call,
)
from agentos_runner.config import RunnerConfig
from agentos_runner.fake import (
    APPROVAL_MARKER,
    FakeModelSession,
    approval_turn,
    default_turn,
)
from agentos_runner.otel import RunTracer
from agentos_runner.session import SessionRunner, _apply_approval_override
from agentos_runner.side_effects import SideEffectClassifier
from agentos_runner.translate import TurnState, translate_message
from claude_agent_sdk import AssistantMessage, ToolUseBlock
from claude_agent_sdk.types import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)


def _event(text: str = "hello") -> Event:
    return Event(type="message", text=text, user="U1", ts="123.45")


def _runner(session: FakeModelSession) -> SessionRunner:
    return SessionRunner(
        session_factory=lambda: session,
        ceiling=10_000,
        tracer=RunTracer(None),
        classifier=SideEffectClassifier(),
        trace_name="test",
        session_id="s-1",
    )


async def _drain(runner: SessionRunner, text: str) -> list[dict[str, object]]:
    lines = [line async for line in runner.run_turn(_event(text))]
    return [json.loads(line) for line in lines]


# --- translation capture --------------------------------------------------------


def test_translate_captures_approval_summary() -> None:
    state = TurnState()
    message = AssistantMessage(
        content=[
            ToolUseBlock(
                id="t1", name=APPROVAL_TOOL_NAME, input={"summary": "Discount 20%"}
            )
        ],
        model="m",
    )
    events = translate_message(message, state, SideEffectClassifier(), None)

    assert state.approval_summary == "Discount 20%"
    # The approval tool is allowlisted read-only: no side_effect_flag, and the
    # tool call still surfaces as a tool note.
    types = [type(e).__name__ for e in events]
    assert types == ["ToolNote"]


def test_translate_ignores_empty_summary() -> None:
    state = TurnState()
    message = AssistantMessage(
        content=[ToolUseBlock(id="t1", name=APPROVAL_TOOL_NAME, input={"summary": "  "})],
        model="m",
    )
    translate_message(message, state, SideEffectClassifier(), None)
    assert state.approval_summary is None


# --- session override ------------------------------------------------------------


def test_approval_turn_ends_awaiting_approval() -> None:
    async def go() -> None:
        session = FakeModelSession(lambda: approval_turn("Discount 20% for ACME"))
        runner = _runner(session)
        await runner.start()
        frames = await _drain(runner, "please discount")

        final = frames[-1]
        assert final["type"] == "final"
        assert final["status"] == "awaiting-approval"
        assert final["approval_summary"] == "Discount 20% for ACME"
        assert runner.status is SessionStatus.AWAITING_APPROVAL
        # No side_effect_flag anywhere in the stream: the request itself is
        # not a real-world action.
        assert all(f["type"] != "side_effect_flag" for f in frames)

    anyio.run(go)


def test_plain_turn_still_ends_done() -> None:
    async def go() -> None:
        session = FakeModelSession(default_turn)
        runner = _runner(session)
        await runner.start()
        frames = await _drain(runner, "hello")
        assert frames[-1]["status"] == "done"
        assert frames[-1]["approval_summary"] is None

    anyio.run(go)


def test_budget_halt_outranks_approval() -> None:
    async def go() -> None:
        session = FakeModelSession(lambda: approval_turn("Anything"))
        runner = SessionRunner(
            session_factory=lambda: session,
            ceiling=1,  # the approval turn reports 8 output tokens; the halt trips
            tracer=RunTracer(None),
            classifier=SideEffectClassifier(),
            trace_name="test",
            session_id="s-1",
        )
        await runner.start()
        frames = await _drain(runner, "gate this")
        final = frames[-1]
        assert final["status"] == "classified-failure"

    anyio.run(go)


def test_only_a_done_final_is_overridden() -> None:
    # Precedence: an interrupt-reclassified (idle) or failed final outranks a
    # pending approval -- the turn did not complete cleanly, so suspending on
    # it would strand a broken run behind a human decision.
    state = TurnState(approval_summary="Anything")
    idle = Final(text="run interrupted", status=SessionStatus.IDLE_AWAITING_INPUT)
    assert _apply_approval_override(idle, state) == idle
    failed = Final(text="run failed", status=SessionStatus.CLASSIFIED_FAILURE)
    assert _apply_approval_override(failed, state) == failed
    done = Final(text="ok", status=SessionStatus.DONE)
    overridden = _apply_approval_override(done, state)
    assert overridden.status is SessionStatus.AWAITING_APPROVAL
    assert overridden.approval_summary == "Anything"


# --- fake model marker path ------------------------------------------------------


def test_fake_default_script_reacts_to_marker() -> None:
    async def go() -> None:
        session = FakeModelSession()
        runner = _runner(session)
        await runner.start()

        frames = await _drain(runner, f"{APPROVAL_MARKER} Give ACME 20% off")
        final = frames[-1]
        assert final["status"] == "awaiting-approval"
        assert final["approval_summary"] == "Give ACME 20% off"

        # A plain query keeps the canned default turn.
        frames = await _drain(runner, "hello again")
        assert frames[-1]["status"] == "done"

    anyio.run(go)


# --- the in-process MCP server ----------------------------------------------------


def test_approval_server_config_shape() -> None:
    config = build_approval_server()
    assert config["type"] == "sdk"
    assert config["name"] == "agentos"
    # The qualified tool name translation matches on.
    assert APPROVAL_TOOL_NAME == "mcp__agentos__request_approval"


# --- the permission gate (#245): canUseTool over approval-required tools --------


def test_can_use_tool_denies_configured_tool_and_records_block() -> None:
    async def go() -> None:
        gate = ApprovalGate(required=frozenset({"Bash"}))
        callback = build_can_use_tool(gate)

        result = await callback(
            "Bash", {"command": "rm -rf /tmp/x"}, ToolPermissionContext()
        )
        assert isinstance(result, PermissionResultDeny)
        assert "requires human approval" in result.message
        assert gate.pending_summary is not None
        assert gate.pending_summary.startswith("Tool call awaiting approval: Bash")
        assert "rm -rf /tmp/x" in gate.pending_summary

    anyio.run(go)


def test_can_use_tool_allows_unconfigured_tools() -> None:
    async def go() -> None:
        gate = ApprovalGate(required=frozenset({"Bash"}))
        callback = build_can_use_tool(gate)

        result = await callback("Read", {"file_path": "/etc/hosts"}, ToolPermissionContext())
        assert isinstance(result, PermissionResultAllow)
        assert gate.pending_summary is None

    anyio.run(go)


def test_first_blocked_call_wins_the_summary() -> None:
    async def go() -> None:
        gate = ApprovalGate(required=frozenset({"Bash"}))
        callback = build_can_use_tool(gate)
        await callback("Bash", {"command": "first"}, ToolPermissionContext())
        await callback("Bash", {"command": "second retry"}, ToolPermissionContext())
        assert gate.pending_summary is not None
        assert "first" in gate.pending_summary
        assert "second retry" not in gate.pending_summary

    anyio.run(go)


def test_summary_truncates_oversized_tool_input() -> None:
    summary = summarize_tool_call("Bash", {"command": "x" * 5000})
    assert len(summary) < 500
    assert summary.endswith("... (truncated)")


def test_gate_reset_clears_the_block() -> None:
    gate = ApprovalGate(required=frozenset({"Bash"}))
    gate.block("Bash", {"command": "x"})
    assert gate.pending_summary is not None
    gate.reset()
    assert gate.pending_summary is None


def test_blocked_turn_ends_awaiting_approval() -> None:
    """The session half: a turn during which the callback blocked a call ends
    awaiting-approval, carrying the blocked-call summary. The script factory
    sets the gate as a side effect, standing in for the SDK invoking the
    callback mid-turn."""

    async def go() -> None:
        gate = ApprovalGate(required=frozenset({"Bash"}))
        turns = {"n": 0}

        def factory() -> list:
            # Only the first turn's script blocks a call, so the second turn
            # also proves the per-turn reset (a stale block never leaks).
            turns["n"] += 1
            if turns["n"] == 1:
                gate.block("Bash", {"command": "echo hi"})
            return default_turn()

        session = FakeModelSession(factory)
        runner = SessionRunner(
            session_factory=lambda: session,
            ceiling=10_000,
            tracer=RunTracer(None),
            classifier=SideEffectClassifier(),
            trace_name="test",
            session_id="s-1",
            approval_gate=gate,
        )
        await runner.start()
        frames = await _drain(runner, "run echo hi")

        final = frames[-1]
        assert final["status"] == "awaiting-approval"
        assert final["approval_summary"] is not None
        assert final["approval_summary"].startswith("Tool call awaiting approval: Bash")

        # The block is per-turn: a clean next turn ends done again.
        frames = await _drain(runner, "hello")
        assert frames[-1]["status"] == "done"

    anyio.run(go)


def test_policy_gate_summary_outranks_gate_block() -> None:
    """When the model explicitly called request_approval AND a permission gate
    blocked a call in the same turn, the model-authored summary wins (it is
    the intentional, richer statement of what needs approval)."""

    async def go() -> None:
        gate = ApprovalGate(required=frozenset({"Bash"}))

        def factory() -> list:
            gate.block("Bash", {"command": "x"})
            return approval_turn("Explicit policy summary")

        session = FakeModelSession(factory)
        runner = SessionRunner(
            session_factory=lambda: session,
            ceiling=10_000,
            tracer=RunTracer(None),
            classifier=SideEffectClassifier(),
            trace_name="test",
            session_id="s-1",
            approval_gate=gate,
        )
        await runner.start()
        frames = await _drain(runner, "gate this")
        assert frames[-1]["status"] == "awaiting-approval"
        assert frames[-1]["approval_summary"] == "Explicit policy summary"

    anyio.run(go)


def test_build_options_permission_posture() -> None:
    # Without a callback the historical bypass posture is preserved verbatim;
    # with one, the session runs in default mode and the callback decides.
    plain = build_options(
        plugins=[], model=None, system_prompt=None, max_turns=1,
        max_budget_usd=None, resume=None,
    )
    assert plain.permission_mode == "bypassPermissions"
    assert plain.can_use_tool is None

    gate = ApprovalGate(required=frozenset({"Bash"}))
    gated = build_options(
        plugins=[], model=None, system_prompt=None, max_turns=1,
        max_budget_usd=None, resume=None, can_use_tool=build_can_use_tool(gate),
    )
    assert gated.permission_mode == "default"
    assert gated.can_use_tool is not None


def test_runner_config_parses_approval_required_tools() -> None:
    base = {
        "AGENTOS_PLUGIN_DIR": "/plugin",
        "AGENTOS_SESSION_ID": "s",
        "AGENTOS_SANDBOX_ID": "b",
        "AGENTOS_BUDGET": '{"max_output_tokens_per_run": 1, "max_usd_per_day": 1.0}',
    }
    config = RunnerConfig.from_env(
        {**base, "AGENTOS_APPROVAL_REQUIRED_TOOLS": "Bash, mcp__github__create_issue ,"}
    )
    assert config.approval_required_tools == ["Bash", "mcp__github__create_issue"]
    assert RunnerConfig.from_env(base).approval_required_tools is None


# --- the manifest approval policy + routes (#247) --------------------------------


def test_load_approval_policy_reads_manifest_gates(tmp_path) -> None:
    import json as _json

    from agentos_runner.approval import load_approval_policy

    plugin = tmp_path / ".claude-plugin"
    plugin.mkdir()
    (plugin / "plugin.json").write_text(
        _json.dumps(
            {
                "name": "deal-desk",
                "approvalPolicy": {
                    "gates": [
                        {"gate": "Bash", "route": "managers"},
                        {"gate": "mcp__crm__send_contract", "route": "legal"},
                    ]
                },
            }
        )
    )
    assert load_approval_policy(str(tmp_path)) == {
        "Bash": "managers",
        "mcp__crm__send_contract": "legal",
    }
    # No dir / no manifest / no policy all yield the empty map.
    assert load_approval_policy(None) == {}
    assert load_approval_policy(str(tmp_path / "missing")) == {}


def test_gate_block_records_the_declared_route() -> None:
    gate = ApprovalGate(
        required=frozenset({"Bash", "Read"}), route_by_tool={"Bash": "managers"}
    )
    gate.block("Bash", {"command": "x"})
    assert gate.pending_route == "managers"
    gate.reset()
    # An env-configured tool with no manifest route carries no route.
    gate.block("Read", {"file_path": "/x"})
    assert gate.pending_route is None


def test_blocked_turn_final_carries_the_route() -> None:
    async def go() -> None:
        gate = ApprovalGate(
            required=frozenset({"Bash"}), route_by_tool={"Bash": "managers"}
        )
        turns = {"n": 0}

        def factory() -> list:
            turns["n"] += 1
            if turns["n"] == 1:
                gate.block("Bash", {"command": "echo hi"})
            return default_turn()

        session = FakeModelSession(factory)
        runner = SessionRunner(
            session_factory=lambda: session,
            ceiling=10_000,
            tracer=RunTracer(None),
            classifier=SideEffectClassifier(),
            trace_name="test",
            session_id="s-1",
            approval_gate=gate,
        )
        await runner.start()
        frames = await _drain(runner, "run echo hi")
        assert frames[-1]["status"] == "awaiting-approval"
        assert frames[-1]["approval_route"] == "managers"

    anyio.run(go)


def test_policy_tool_route_param_reaches_the_final() -> None:
    async def go() -> None:
        session = FakeModelSession(
            lambda: approval_turn("Discount for ACME", route="managers")
        )
        runner = _runner(session)
        await runner.start()
        frames = await _drain(runner, "discount please")
        final = frames[-1]
        assert final["status"] == "awaiting-approval"
        assert final["approval_route"] == "managers"

    anyio.run(go)


def test_fake_routed_marker_names_the_route() -> None:
    async def go() -> None:
        session = FakeModelSession()
        runner = _runner(session)
        await runner.start()
        frames = await _drain(
            runner, "[fake:request-approval:managers] Give ACME 20% off"
        )
        final = frames[-1]
        assert final["status"] == "awaiting-approval"
        assert final["approval_summary"] == "Give ACME 20% off"
        assert final["approval_route"] == "managers"
        # The un-routed marker still works, with no route.
        frames = await _drain(runner, f"{APPROVAL_MARKER} plain request")
        assert frames[-1]["approval_route"] is None

    anyio.run(go)
