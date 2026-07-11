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
from aci_protocol import Event, Interrupt, parse_ndjson, run_conformance
from agentos_runner import CLAUDE_READONLY_TOOLS
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


def step_finish(input_tokens: int, output_tokens: int) -> dict[str, Any]:
    return {
        "type": "message.part.updated",
        "properties": {
            "sessionID": SID,
            "part": {
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

    texts = [m.text for m in messages if isinstance(m, AssistantText)]
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
    texts = [m.text for m in messages if isinstance(m, AssistantText)]
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
