"""SDK-message to ACI-outbound-event translation."""

from aci_protocol import SessionStatus
from agentos_runner import SideEffectClassifier
from agentos_runner.translate import TurnState, translate_message
from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
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


def test_rate_limit_event_maps_to_error() -> None:
    info = RateLimitInfo(status="allowed_warning")
    events = _translate(RateLimitEvent(rate_limit_info=info, uuid="u", session_id="s"))
    assert [e.type for e in events] == ["error"]
    assert events[0].classification == "rate-limit"
