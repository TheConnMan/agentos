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
from agentos_runner.approval import APPROVAL_TOOL_NAME, build_approval_server
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
