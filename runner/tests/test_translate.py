"""TurnEvent to ACI-outbound-event translation."""

from aci_protocol import SessionStatus
from agentos_runner import CLAUDE_READONLY_TOOLS, SideEffectClassifier
from agentos_runner.events import AssistantText, RateLimit, ToolCall, TurnResult
from agentos_runner.opencode import OPENCODE_READONLY_TOOLS
from agentos_runner.translate import TurnState, translate_event


def _translate(event: object, state: TurnState | None = None) -> list:
    return translate_event(
        event, state or TurnState(), SideEffectClassifier(CLAUDE_READONLY_TOOLS), None
    )


def test_text_block_becomes_text_delta() -> None:
    events = _translate(AssistantText(text="hi there"))
    assert [e.type for e in events] == ["text_delta"]
    assert events[0].text == "hi there"


def test_tool_use_emits_note_and_side_effect_once() -> None:
    # Two tool calls share a TurnState across events: the side-effect dedup lives
    # in the state, so a second side-effecting tool in the same run adds a note
    # but no second flag.
    state = TurnState()
    events = _translate(ToolCall(name="Bash", id="1"), state)
    events += _translate(ToolCall(name="Write", id="2"), state)
    types = [e.type for e in events]
    # Two tool notes, but the side-effect flag fires once per run (dedup).
    assert types.count("tool_note") == 2
    assert types.count("side_effect_flag") == 1
    assert state.side_effect_emitted


def test_read_only_tool_notes_without_flag() -> None:
    events = _translate(ToolCall(name="Read", id="1"))
    assert [e.type for e in events] == ["tool_note"]


def test_opencode_read_only_tool_notes_without_flag() -> None:
    # Regression (issue #308): an OpenCode ``read`` classified against the OpenCode
    # declaration is a plain tool note with NO side-effect flag.
    events = translate_event(
        ToolCall(name="read", id="t1", model=None),
        TurnState(),
        SideEffectClassifier(OPENCODE_READONLY_TOOLS),
        None,
    )
    assert [e.type for e in events] == ["tool_note"]


def test_opencode_unknown_tool_emits_side_effect_flag() -> None:
    events = translate_event(
        ToolCall(name="SomeBrandNewTool", id="t1", model=None),
        TurnState(),
        SideEffectClassifier(OPENCODE_READONLY_TOOLS),
        None,
    )
    assert "side_effect_flag" in [e.type for e in events]


def test_result_success_is_final_done() -> None:
    events = _translate(TurnResult(text="answer"))
    assert [e.type for e in events] == ["final"]
    assert events[0].status == SessionStatus.DONE
    assert events[0].text == "answer"


def test_reasoning_model_empty_result_falls_back_to_assistant_text() -> None:
    # A reasoning model routed through OpenRouter (e.g. z-ai/glm-5.2) streams the
    # answer as assistant text but the terminal TurnResult reports success with an
    # EMPTY result (the empty-signature thinking block trips result extraction).
    # The delivered Final must carry the accumulated assistant text, not "".
    state = TurnState()
    _translate(AssistantText(text="The sky is blue on a clear day."), state)  # accumulates
    events = _translate(TurnResult(text=""), state)
    assert [e.type for e in events] == ["final"]
    assert events[0].status == SessionStatus.DONE
    assert events[0].text == "The sky is blue on a clear day."


def test_result_with_own_text_ignores_accumulated_fallback() -> None:
    # When the TurnResult carries its own text, it wins over accumulated text
    # (non-reasoning models are unaffected by the empty-result fallback).
    state = TurnState()
    _translate(AssistantText(text="streamed"), state)
    events = _translate(TurnResult(text="authoritative"), state)
    assert [e.type for e in events] == ["final"]
    assert events[0].text == "authoritative"


def test_result_error_is_error_then_classified_final() -> None:
    events = _translate(
        TurnResult(text="boom", is_error=True, subtype="error_during_execution")
    )
    assert [e.type for e in events] == ["error", "final"]
    assert events[-1].status == SessionStatus.CLASSIFIED_FAILURE


def test_assistant_error_field_emits_error_event() -> None:
    events = _translate(AssistantText(text="", error="rate_limit"))
    assert [e.type for e in events] == ["error"]
    assert events[0].classification == "rate_limit"


def test_rate_limit_rejected_maps_to_error() -> None:
    events = _translate(RateLimit(rejected=True))
    assert [e.type for e in events] == ["error"]
    assert events[0].classification == "rate-limit"


def test_rate_limit_warning_is_dropped() -> None:
    # A non-rejecting rate-limit signal is advisory; the run is still allowed to
    # continue, so no failure event is injected.
    assert _translate(RateLimit(rejected=False)) == []
