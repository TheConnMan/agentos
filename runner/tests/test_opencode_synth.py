"""Offline regression for the OpenCode harness shim (issue #25).

These tests exercise the OpenCode-frame -> TurnEvent synthesis and drive it
through the *real* runner core (translate + session + conformance) with zero
network: scripted ``/event`` frames stand in for a live SSE stream, the same way
``fake.py`` scripts TurnEvents. They pin the mapping (text deltas, tool calls,
usage, terminal result, the ignored text-part bookends, the busy->idle gate) and
prove a scripted OpenCode turn passes the frozen ACI conformance suite.

The live path (a real ``opencode serve`` + a real OpenRouter turn) is validated
separately by ``agentos_runner.opencode.conformance`` -- not here, so this file
stays fast and deterministic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anyio
from aci_protocol import Event, Interrupt, SessionStatus, parse_ndjson, run_conformance
from agentos_runner import CLAUDE_READONLY_TOOLS
from agentos_runner.budget import BUDGET_CLASSIFICATION
from agentos_runner.events import AssistantText, ToolCall, TurnResult
from agentos_runner.opencode.synth import TurnSynthesizer
from agentos_runner.otel import RunTracer
from agentos_runner.session import SessionRunner
from agentos_runner.side_effects import SideEffectClassifier

SID = "ses_test"
MSG = "msg_1"


# --- scripted OpenCode /event frame builders (real v1.17.17 wire shapes) --------


def busy() -> dict[str, Any]:
    return {"type": "session.status", "properties": {"sessionID": SID, "status": {"type": "busy"}}}


def idle() -> dict[str, Any]:
    return {"type": "session.status", "properties": {"sessionID": SID, "status": {"type": "idle"}}}


def text_delta(delta: str, part_id: str = "prt_text") -> dict[str, Any]:
    return {
        "type": "message.part.delta",
        "properties": {
            "sessionID": SID,
            "messageID": MSG,
            "partID": part_id,
            "field": "text",
            "delta": delta,
        },
    }


def text_part_bookend(text: str, part_id: str = "prt_text") -> dict[str, Any]:
    # The text-start (empty) and text-end (full snapshot) part updates the runner
    # must ignore so streamed deltas are never re-emitted.
    return {
        "type": "message.part.updated",
        "properties": {
            "sessionID": SID,
            "part": {"id": part_id, "type": "text", "text": text},
            "time": 1,
        },
    }


def tool_running(call_id: str, tool: str, inp: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "message.part.updated",
        "properties": {
            "sessionID": SID,
            "part": {
                "id": f"prt_{call_id}",
                "type": "tool",
                "callID": call_id,
                "tool": tool,
                "state": {"status": "running", "input": inp},
            },
            "time": 1,
        },
    }


def step_finish(
    input_tokens: int, output_tokens: int, part_id: str = "prt_step"
) -> dict[str, Any]:
    # Each step-finish part carries a distinct id on the live wire and reports
    # that step's tokens only (reset on the next step); vary ``part_id`` to model
    # a multi-step turn whose true output total is the sum of the steps.
    return {
        "type": "message.part.updated",
        "properties": {
            "sessionID": SID,
            "part": {
                "id": part_id,
                "type": "step-finish",
                "tokens": {"input": input_tokens, "output": output_tokens, "reasoning": 0},
            },
            "time": 2,
        },
    }


def message_updated(model_id: str, input_tokens: int, output_tokens: int) -> dict[str, Any]:
    return {
        "type": "message.updated",
        "properties": {
            "sessionID": SID,
            "info": {
                "modelID": model_id,
                "tokens": {"input": input_tokens, "output": output_tokens},
            },
        },
    }


def session_error(message: str) -> dict[str, Any]:
    return {
        "type": "session.error",
        "properties": {"sessionID": SID, "error": {"data": {"message": message}}},
    }


def reasoning_part(part_id: str = "prt_reason") -> dict[str, Any]:
    # The message.part.updated snapshot that announces a reasoning part; its
    # deltas arrive on field="text" just like a text part's, so only this
    # snapshot distinguishes the thinking channel from the answer channel.
    return {
        "type": "message.part.updated",
        "properties": {
            "sessionID": SID,
            "part": {"id": part_id, "type": "reasoning"},
            "time": 1,
        },
    }


def reasoning_then_text_frames() -> list[dict[str, Any]]:
    """A reasoning-forward turn: thinking streams first, then the answer.

    Mirrors a live gpt-oss-style turn where reasoning deltas precede the text
    answer and both carry field="text"; only the answer must reach the ACI.
    """

    return [
        busy(),
        reasoning_part("prt_r"),
        text_delta("Let me think about the request. ", "prt_r"),
        text_delta("I will keep it terse.", "prt_r"),
        text_part_bookend("", "prt_t"),
        text_delta("pong", "prt_t"),
        text_part_bookend("pong", "prt_t"),
        step_finish(30, 4),
        message_updated("openai/gpt-oss-120b", 30, 4),
        idle(),
    ]


# Global bus noise the reader would drop / the synth must ignore.
NOISE = [
    {"type": "server.connected", "properties": {}},
    {"type": "plugin.added", "properties": {}},
    {"type": "session.updated", "properties": {"sessionID": SID}},
    {"type": "session.diff", "properties": {"sessionID": SID}},
]


def default_turn_frames() -> list[dict[str, Any]]:
    """A representative live-shaped turn: text, a side-effecting tool, more text."""

    return [
        busy(),
        text_part_bookend(""),
        text_delta("Looking into it"),
        tool_running("call_1", "Bash", {"command": "echo hi"}),
        text_delta(" all done"),
        text_part_bookend("Looking into it all done"),
        step_finish(20, 8),
        message_updated("z-ai/glm-4.6", 20, 8),
        idle(),
    ]


def _run_synth(frames: list[dict[str, Any]]) -> tuple[list[Any], TurnSynthesizer]:
    synth = TurnSynthesizer()
    out: list[Any] = []
    for frame in frames:
        out.extend(synth.ingest(frame))
    return out, synth


# --- a scripted (offline) ModelSession that replays frames through the synth ----


class ScriptedOpenCodeSession:
    """ModelSession replaying scripted OpenCode frames through the live synth.

    Structurally the live ``OpenCodeModelSession``'s ``receive_turn`` without a
    subprocess or SSE: it feeds canned frames to a fresh ``TurnSynthesizer``, so
    it exercises the exact OpenCode->SDK synthesis the runner core consumes.
    """

    def __init__(self, frames_factory: Any = None) -> None:
        self._frames_factory = frames_factory or default_turn_frames
        self.queries: list[str] = []
        self.interrupts = 0

    async def connect(self) -> None:
        return None

    async def query(self, text: str) -> None:
        self.queries.append(text)

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def receive_turn(self) -> AsyncIterator[Any]:
        synth = TurnSynthesizer()
        for frame in self._frames_factory():
            for message in synth.ingest(frame):
                yield message

    async def close(self) -> None:
        return None


def _build_runner() -> SessionRunner:
    return SessionRunner(
        session_factory=ScriptedOpenCodeSession,
        ceiling=0,
        tracer=RunTracer(None),
        classifier=SideEffectClassifier(CLAUDE_READONLY_TOOLS),
        trace_name="opencode-offline",
    )


def _offline_producer(message: Event | Interrupt) -> list[str]:
    lines: list[str] = []

    async def _run() -> None:
        runner = _build_runner()
        await runner.start()
        try:
            async for line in runner.run_inbound(message):
                lines.append(line)
        finally:
            await runner.close()

    anyio.run(_run)
    return lines


def _turn_ndjson(frames_factory: Any) -> list[str]:
    """NDJSON for one turn whose session replays ``frames_factory()``."""

    lines: list[str] = []

    async def _run() -> None:
        runner = SessionRunner(
            session_factory=lambda: ScriptedOpenCodeSession(frames_factory),
            ceiling=0,
            tracer=RunTracer(None),
            classifier=SideEffectClassifier(CLAUDE_READONLY_TOOLS),
            trace_name="opencode-offline",
        )
        await runner.start()
        try:
            async for line in runner.run_turn(
                Event(type="message", text="x", user="U1", ts="1.0")
            ):
                lines.append(line)
        finally:
            await runner.close()

    anyio.run(_run)
    return lines


# --- synth unit tests -----------------------------------------------------------


def test_synth_maps_text_tool_and_terminal_result() -> None:
    messages, synth = _run_synth(default_turn_frames())
    assert synth.done

    # Filter on m.text so zero-text usage carriers (the mid-turn budget signal)
    # are not mistaken for answer text.
    texts = [m.text for m in messages if isinstance(m, AssistantText) and m.text]
    assert texts == ["Looking into it", " all done"], texts

    tools = [m for m in messages if isinstance(m, ToolCall)]
    assert len(tools) == 1
    # ToolCall in the union carries the tool name and id (what the runner uses to
    # note and side-effect-flag the call); the raw tool input is not part of it.
    assert tools[0].name == "Bash"
    assert tools[0].id == "call_1"

    results = [m for m in messages if isinstance(m, TurnResult)]
    assert len(results) == 1
    result = results[0]
    assert result.is_error is False
    assert result.text == "Looking into it all done"
    assert result.usage == {"input_tokens": 20, "output_tokens": 8}


def test_synth_carries_model_onto_streamed_messages() -> None:
    # model_id is only known after message.updated; a turn whose message.updated
    # precedes text should stamp the model on the text messages.
    frames = [
        busy(),
        message_updated("z-ai/glm-4.6", 5, 2),
        text_part_bookend(""),
        text_delta("hi"),
        idle(),
    ]
    messages, _ = _run_synth(frames)
    text_msgs = [m for m in messages if isinstance(m, AssistantText) and m.text]
    assert text_msgs and text_msgs[0].model == "z-ai/glm-4.6"


def test_synth_ignores_bookends_and_bus_noise() -> None:
    # Interleaving noise + bookends must not duplicate or drop text.
    frames = [busy(), *NOISE, text_part_bookend(""), text_delta("one"), *NOISE,
              text_delta(" two"), text_part_bookend("one two"), idle()]
    messages, _ = _run_synth(frames)
    texts = [m.text for m in messages if isinstance(m, AssistantText)]
    assert texts == ["one", " two"], texts


def test_synth_session_error_yields_error_result() -> None:
    frames = [busy(), text_delta("partial"), session_error("provider exploded")]
    messages, synth = _run_synth(frames)
    assert synth.done
    results = [m for m in messages if isinstance(m, TurnResult)]
    assert len(results) == 1
    assert results[0].is_error is True
    assert results[0].text == "provider exploded"


def test_synth_drops_reasoning_deltas_keeps_text() -> None:
    # A delta's field is "text" even for a reasoning part; only the text part's
    # content (resolved by partID from the snapshots) reaches the TurnEvents.
    messages, synth = _run_synth(reasoning_then_text_frames())
    # Filter on m.text so zero-text usage carriers are excluded from answer text.
    texts = [m.text for m in messages if isinstance(m, AssistantText) and m.text]
    assert texts == ["pong"], texts
    results = [m for m in messages if isinstance(m, TurnResult)]
    assert results and results[0].text == "pong"


def test_synth_pre_turn_idle_does_not_terminate() -> None:
    # A stale idle with no preceding activity must not end a turn (the busy/
    # activity gate). Only the idle after real activity terminates.
    synth = TurnSynthesizer()
    assert synth.ingest(idle()) == []
    assert synth.done is False
    synth.ingest(busy())
    synth.ingest(text_delta("go"))
    out = synth.ingest(idle())
    assert synth.done is True
    assert any(isinstance(m, TurnResult) for m in out)


def test_synth_interrupted_iteration_still_has_no_partial_duplicate() -> None:
    # Feeding a turn missing its idle leaves the synth not-done and emits no
    # terminal result -- the runner then supplies its own final (its contract).
    frames = default_turn_frames()[:-1]  # drop the terminal idle
    messages, synth = _run_synth(frames)
    assert synth.done is False
    assert not any(isinstance(m, TurnResult) for m in messages)


# --- offline end-to-end through the real runner core ----------------------------


def test_scripted_opencode_turn_streams_wellformed_ndjson() -> None:
    lines = _offline_producer(Event(type="message", text="do it", user="U1", ts="1.0"))
    events = parse_ndjson("".join(lines))
    types = [e.type for e in events]
    assert types[-1] == "final", types
    assert "text_delta" in types
    # Bash is non-idempotent -> the runner flags one side effect.
    assert "side_effect_flag" in types
    final = events[-1]
    assert final.status == "done"
    assert final.text == "Looking into it all done"


def test_reasoning_content_absent_from_ndjson_stream() -> None:
    lines = _turn_ndjson(reasoning_then_text_frames)
    events = parse_ndjson("".join(lines))
    text_deltas = [e.text for e in events if e.type == "text_delta"]
    assert text_deltas == ["pong"], text_deltas
    final = events[-1]
    assert final.type == "final"
    assert final.text == "pong"
    # No thinking-channel content leaks anywhere in the emitted stream.
    blob = "".join(lines)
    assert "think about" not in blob
    assert "keep it terse" not in blob


def test_scripted_opencode_backend_passes_aci_conformance() -> None:
    report = run_conformance(_offline_producer)
    assert report.passed, report.summary() + " :: " + str(
        [(c.name, c.detail) for c in report.checks if not c.passed]
    )
    assert any(c.name == "producer_stream" and c.passed for c in report.checks)


# --- mid-turn usage carriers + budget enforcement (issue #311 AC2) --------------


def usage_increment_frames() -> list[dict[str, Any]]:
    """Three per-step token reports that each RESET (real per-step wire shape).

    Each step reports only its own output (20, 11, 28), and message.updated
    echoes the same per-step number under the constant message id. The turn's
    true output total is the SUM (59), so the mid-turn deltas are [20, 11, 28].
    """

    return [
        busy(),
        step_finish(50, 20, "prt_s1"),  # step 1: total 0 -> 20, delta 20
        message_updated("z-ai/glm-4.6", 50, 20),  # redundant per-step echo, no carrier
        step_finish(60, 11, "prt_s2"),  # step 2: total 20 -> 31, delta 11
        message_updated("z-ai/glm-4.6", 60, 11),  # redundant per-step echo, no carrier
        step_finish(70, 28, "prt_s3"),  # step 3: total 31 -> 59, delta 28
        idle(),
    ]


def _usage_carriers(messages: list[Any]) -> list[AssistantText]:
    return [m for m in messages if isinstance(m, AssistantText) and m.usage is not None]


def test_synth_emits_monotonic_positive_usage_deltas() -> None:
    # The mid-turn budget signal: each step's per-step output is summed into a
    # running turn total, and each rise in that total is emitted as a zero-text
    # AssistantText carrying only the positive delta, so the runner can fold and
    # halt before the terminal. The redundant message.updated echo of the same
    # per-step number emits nothing (it is not a per-step key; summing it would
    # double-count).
    messages, _ = _run_synth(usage_increment_frames())
    carriers = _usage_carriers(messages)
    assert [c.usage["output_tokens"] for c in carriers] == [20, 11, 28]
    # Carriers are text-free so translate emits no ACI event for them.
    assert all(c.text == "" for c in carriers)
    # The terminal result carries the last-seen input with the SUMMED per-step
    # output (59), which the deltas also sum to -- so the mid-turn signal and the
    # authoritative terminal total agree (no double count in the runner's fold).
    results = [m for m in messages if isinstance(m, TurnResult)]
    assert results[-1].usage == {"input_tokens": 70, "output_tokens": 59}
    assert sum(c.usage["output_tokens"] for c in carriers) == 59


def within_step_streaming_frames() -> list[dict[str, Any]]:
    """One step whose block streams upward (5 -> 20) under a single part id."""

    return [
        busy(),
        step_finish(10, 5, "prt_s1"),  # step 1 rises: total 0 -> 5, delta 5
        step_finish(10, 20, "prt_s1"),  # SAME id 5 -> 20: total 5 -> 20, delta 15
        idle(),
    ]


def test_synth_within_step_streaming_takes_max_not_sum() -> None:
    # A step's block can climb within one part id as it streams. That id
    # contributes its MAX (20), not the sum of its intermediate reports (25), so
    # the running total tracks 20 and the deltas [5, 15] sum to 20.
    messages, _ = _run_synth(within_step_streaming_frames())
    carriers = _usage_carriers(messages)
    assert [c.usage["output_tokens"] for c in carriers] == [5, 15]
    assert sum(c.usage["output_tokens"] for c in carriers) == 20
    results = [m for m in messages if isinstance(m, TurnResult)]
    assert results[-1].usage == {"input_tokens": 10, "output_tokens": 20}


def budget_runaway_frames() -> list[dict[str, Any]]:
    """A runaway turn: output far above the ceiling reported mid-turn, then more text.

    The post-report ``text_delta`` is the tripwire: if the halt fires at the
    ``step-finish`` (via a mid-turn usage carrier), the text after it never
    reaches the stream and the session is interrupted; if the halt only lands at
    the terminal ``idle`` (base behavior), the text leaks through first and no
    interrupt is issued.
    """

    return [
        busy(),
        text_part_bookend(""),
        text_delta("before "),
        step_finish(10, 500),
        text_delta("LEAKED-AFTER-HALT"),
        idle(),
    ]


def test_opencode_budget_halts_midturn_on_reported_usage() -> None:
    created: list[ScriptedOpenCodeSession] = []

    def factory() -> ScriptedOpenCodeSession:
        session = ScriptedOpenCodeSession(budget_runaway_frames)
        created.append(session)
        return session

    lines: list[str] = []

    async def _run() -> None:
        runner = SessionRunner(
            session_factory=factory,
            ceiling=10,
            tracer=RunTracer(None),
            classifier=SideEffectClassifier(CLAUDE_READONLY_TOOLS),
            trace_name="opencode-offline",
        )
        await runner.start()
        try:
            async for line in runner.run_turn(
                Event(type="message", text="x", user="U1", ts="1.0")
            ):
                lines.append(line)
        finally:
            await runner.close()

    anyio.run(_run)

    blob = "".join(lines)
    events = parse_ndjson(blob)
    final = events[-1]
    assert final.type == "final"
    assert final.status == SessionStatus.CLASSIFIED_FAILURE
    assert any(
        e.type == "error" and e.classification == BUDGET_CLASSIFICATION for e in events
    )
    # Halt landed at the step-finish, before the post-report text: the runaway
    # turn is actually cut off (interrupt called), not just relabelled at the end.
    assert "LEAKED-AFTER-HALT" not in blob
    assert created and created[0].interrupts >= 1


def multistep_undercount_frames() -> list[dict[str, Any]]:
    """Two steps EACH under the ceiling whose SUM exceeds it (the Codex case).

    Each step reports 300 output tokens (below a ceiling of 500), but the turn's
    true total is 600. The single-max fold this module used to do would top out
    at 300 and never trip the ceiling; summing per-step maxima trips it at the
    second step-finish. The post-report text is the tripwire: it must not reach
    the stream if the halt fires mid-turn.
    """

    return [
        busy(),
        text_part_bookend(""),
        text_delta("step one "),
        step_finish(10, 300, "prt_s1"),  # per-step 300 < 500, running total 300
        text_delta("step two "),
        step_finish(10, 300, "prt_s2"),  # running total 600 > 500 -> halt here
        text_delta("LEAKED-AFTER-HALT"),
        idle(),
    ]


def test_opencode_budget_halts_on_summed_multistep_usage() -> None:
    # The multi-step undercount regression: no single step crosses the ceiling,
    # but their SUM does. Proves the runner halts on the summed per-step total,
    # not on the max of any one step's report.
    created: list[ScriptedOpenCodeSession] = []

    def factory() -> ScriptedOpenCodeSession:
        session = ScriptedOpenCodeSession(multistep_undercount_frames)
        created.append(session)
        return session

    lines: list[str] = []

    async def _run() -> None:
        runner = SessionRunner(
            session_factory=factory,
            ceiling=500,
            tracer=RunTracer(None),
            classifier=SideEffectClassifier(CLAUDE_READONLY_TOOLS),
            trace_name="opencode-offline",
        )
        await runner.start()
        try:
            async for line in runner.run_turn(
                Event(type="message", text="x", user="U1", ts="1.0")
            ):
                lines.append(line)
        finally:
            await runner.close()

    anyio.run(_run)

    blob = "".join(lines)
    events = parse_ndjson(blob)
    final = events[-1]
    assert final.type == "final"
    assert final.status == SessionStatus.CLASSIFIED_FAILURE
    assert any(
        e.type == "error" and e.classification == BUDGET_CLASSIFICATION for e in events
    )
    # Halt fired at the second step-finish (summed total 600 > 500), before the
    # post-report text -- the runaway multi-step turn is actually cut off.
    assert "LEAKED-AFTER-HALT" not in blob
    assert created and created[0].interrupts >= 1
