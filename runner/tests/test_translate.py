"""SDK-message to ACI-outbound-event translation."""

from aci_protocol import SessionStatus
from agentos_runner import SideEffectClassifier
from agentos_runner.translate import TurnState, translate_message
from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)
from claude_agent_sdk.types import RateLimitInfo


def _translate(message: object, state: TurnState | None = None) -> list:
    return translate_message(message, state or TurnState(), SideEffectClassifier(), None)


def test_text_block_becomes_text_delta() -> None:
    msg = AssistantMessage(content=[TextBlock(text="hi there")], model="m")
    events = _translate(msg)
    assert [e.type for e in events] == ["text_delta"]
    assert events[0].text == "hi there"


def test_tool_use_emits_note_and_side_effect_once() -> None:
    state = TurnState()
    msg = AssistantMessage(
        content=[
            ToolUseBlock(id="1", name="Bash", input={}),
            ToolUseBlock(id="2", name="Write", input={}),
        ],
        model="m",
    )
    events = _translate(msg, state)
    types = [e.type for e in events]
    # Two tool notes, but the side-effect flag fires once per run (dedup).
    assert types.count("tool_note") == 2
    assert types.count("side_effect_flag") == 1
    assert state.side_effect_emitted


def test_read_only_tool_notes_without_flag() -> None:
    msg = AssistantMessage(content=[ToolUseBlock(id="1", name="Read", input={})], model="m")
    events = _translate(msg)
    assert [e.type for e in events] == ["tool_note"]


def test_result_success_is_final_done() -> None:
    msg = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="s", result="answer",
    )
    events = _translate(msg)
    assert [e.type for e in events] == ["final"]
    assert events[0].status == SessionStatus.DONE
    assert events[0].text == "answer"
    # The SDK session id is surfaced on the final so the worker can rehydrate the
    # exact session on resume (ADR-0003) rather than guessing a history ref.
    assert events[0].session_id == "s"


def test_result_error_final_carries_session_id() -> None:
    msg = ResultMessage(
        subtype="error_during_execution", duration_ms=1, duration_api_ms=1, is_error=True,
        num_turns=1, session_id="s", result="boom",
    )
    events = _translate(msg)
    assert events[-1].type == "final"
    assert events[-1].session_id == "s"


def test_reasoning_model_empty_result_falls_back_to_assistant_text() -> None:
    # A reasoning model routed through OpenRouter (e.g. z-ai/glm-5.2) streams the
    # answer as a TextBlock but the terminal ResultMessage reports success with an
    # EMPTY result (the empty-signature thinking block trips result extraction).
    # The delivered Final must carry the assistant text, not "".
    state = TurnState()
    assistant = AssistantMessage(
        content=[
            TextBlock(text="The sky is blue on a clear day."),
            ThinkingBlock(thinking="the user asked...", signature=""),
        ],
        model="z-ai/glm-5.2",
    )
    _translate(assistant, state)  # accumulates text into state
    result = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="s", result="",
    )
    events = _translate(result, state)
    assert [e.type for e in events] == ["final"]
    assert events[0].status == SessionStatus.DONE
    assert events[0].text == "The sky is blue on a clear day."


def test_result_with_own_text_ignores_accumulated_fallback() -> None:
    # When the ResultMessage carries its own result, it wins over accumulated text
    # (non-reasoning models are unaffected by the empty-result fallback).
    state = TurnState()
    _translate(AssistantMessage(content=[TextBlock(text="streamed")], model="m"), state)
    result = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="s", result="authoritative",
    )
    events = _translate(result, state)
    assert [e.type for e in events] == ["final"]
    assert events[0].text == "authoritative"


def test_result_error_is_error_then_classified_final() -> None:
    msg = ResultMessage(
        subtype="error_during_execution", duration_ms=1, duration_api_ms=1, is_error=True,
        num_turns=1, session_id="s", result="boom",
    )
    events = _translate(msg)
    assert [e.type for e in events] == ["error", "final"]
    assert events[-1].status == SessionStatus.CLASSIFIED_FAILURE


def test_assistant_error_field_emits_error_event() -> None:
    msg = AssistantMessage(content=[], model="m", error="rate_limit")
    events = _translate(msg)
    assert [e.type for e in events] == ["error"]
    assert events[0].classification == "rate_limit"


def test_rate_limit_rejected_maps_to_error() -> None:
    info = RateLimitInfo(status="rejected")
    events = _translate(RateLimitEvent(rate_limit_info=info, uuid="u", session_id="s"))
    assert [e.type for e in events] == ["error"]
    assert events[0].classification == "rate-limit"


def test_rate_limit_warning_is_dropped() -> None:
    # allowed / allowed_warning are advisory; the run is still allowed to
    # continue, so no failure event is injected.
    for status in ("allowed", "allowed_warning"):
        info = RateLimitInfo(status=status)
        events = _translate(RateLimitEvent(rate_limit_info=info, uuid="u", session_id="s"))
        assert events == []
