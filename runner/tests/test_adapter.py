"""map_sdk_message: the claude-agent-sdk -> TurnEvent adapter seam.

This is the ONE place claude-agent-sdk dataclasses survive in the runner. Every
assertion here pins the shape ``map_sdk_message`` produces so ``translate.py`` and
``session.py`` can consume the runner-owned ``TurnEvent`` union and never import
the SDK. Importing the SDK types in this file is therefore correct -- it is the
adapter's own contract under test.
"""

from agentos_runner.adapter import map_sdk_message
from agentos_runner.events import AssistantText, RateLimit, ToolCall, TurnResult
from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import RateLimitInfo


def _result(
    *,
    subtype: str = "success",
    is_error: bool = False,
    result: str = "",
    usage: dict | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="s",
        result=result,
        usage=usage,
    )


def test_text_block_maps_to_assistant_text() -> None:
    events = map_sdk_message(AssistantMessage(content=[TextBlock(text="hi")], model="m"))
    assert events == [AssistantText(text="hi", model="m")]


def test_tool_use_blocks_map_to_tool_calls_in_order() -> None:
    events = map_sdk_message(
        AssistantMessage(
            content=[
                ToolUseBlock(id="1", name="Bash", input={}),
                ToolUseBlock(id="2", name="Write", input={}),
            ],
            model="m",
        )
    )
    assert all(isinstance(e, ToolCall) for e in events)
    assert [(e.name, e.id, e.model) for e in events] == [
        ("Bash", "1", "m"),
        ("Write", "2", "m"),
    ]


def test_empty_content_error_survives_as_leading_carrier() -> None:
    # A provider auth rejection arrives as an AssistantMessage.error with EMPTY
    # content. The error must survive as a leading AssistantText carrier so the
    # session's fast-fail sees it (the auth-rejection case).
    events = map_sdk_message(
        AssistantMessage(content=[], model="m", error="authentication_failed")
    )
    assert events == [AssistantText(text="", error="authentication_failed", model="m")]


def test_error_prepended_before_content() -> None:
    # When a message carries BOTH an error and non-empty text, the error must be
    # emitted as a leading empty-text carrier BEFORE the text event, so the
    # downstream ACI stream surfaces the error before the text delta. Ordering is
    # load-bearing: a consumer fast-failing on the error must see it first.
    events = map_sdk_message(
        AssistantMessage(content=[TextBlock(text="hi")], model="m", error="rate_limit")
    )
    assert events == [
        AssistantText(text="", model="m", error="rate_limit"),
        AssistantText(text="hi", model="m"),
    ]


def test_usage_homed_to_exactly_one_event_no_double_count() -> None:
    # Load-bearing correctness property: a single source AssistantMessage carrying
    # usage must fold that usage into the budget exactly once, so the adapter homes
    # it onto exactly one event. If every mapped event carried the usage, the
    # budget would double-count (8 + 8 = 16) and could trip the ceiling early.
    events = map_sdk_message(
        AssistantMessage(
            content=[TextBlock(text="a"), ToolUseBlock(id="1", name="Read", input={})],
            model="m",
            usage={"output_tokens": 8},
        )
    )
    total = sum((e.usage or {}).get("output_tokens", 0) for e in events)
    assert total == 8


def test_usage_homed_to_last_event() -> None:
    # Usage homes onto the LAST mapped event, not the first. session._drive_turn
    # folds usage per event in order and halts the turn the instant the ceiling
    # trips; homing to the last event guarantees a same-message budget halt fires
    # only AFTER a later side-effecting ToolCall (and its required side_effect_flag)
    # has been emitted, preserving the no-retry-after-side-effects guarantee. Bash
    # is side-effecting, so the last event here is the ToolCall.
    events = map_sdk_message(
        AssistantMessage(
            content=[TextBlock(text="a"), ToolUseBlock(id="1", name="Bash", input={})],
            model="m",
            usage={"output_tokens": 8},
        )
    )
    assert events[-1].usage == {"output_tokens": 8}
    assert events[0].usage is None


def test_reasoning_block_dropped() -> None:
    # ThinkingBlock content is dropped at the adapter so a reasoning model's
    # thinking never reaches the ACI (mirrors how the answer-only channel works).
    events = map_sdk_message(
        AssistantMessage(
            content=[
                TextBlock(text="answer"),
                ThinkingBlock(thinking="secret plan", signature=""),
            ],
            model="m",
        )
    )
    assert events == [AssistantText(text="answer", model="m")]
    assert all("secret plan" not in getattr(e, "text", "") for e in events)


def test_result_success_maps_to_turn_result() -> None:
    events = map_sdk_message(_result(result="done", usage={"output_tokens": 5}))
    assert events == [
        TurnResult(text="done", is_error=False, subtype="success", usage={"output_tokens": 5})
    ]


def test_result_error_carries_subtype_and_is_error() -> None:
    events = map_sdk_message(
        _result(subtype="error_during_execution", is_error=True, result="boom")
    )
    assert len(events) == 1
    result = events[0]
    assert isinstance(result, TurnResult)
    assert result.is_error is True
    assert result.subtype == "error_during_execution"
    assert result.text == "boom"


def test_rate_limit_rejected_maps_to_rate_limit_event() -> None:
    events = map_sdk_message(
        RateLimitEvent(rate_limit_info=RateLimitInfo(status="rejected"), uuid="u", session_id="s")
    )
    assert events == [RateLimit(rejected=True)]


def test_rate_limit_allowed_maps_to_non_rejected() -> None:
    events = map_sdk_message(
        RateLimitEvent(rate_limit_info=RateLimitInfo(status="allowed"), uuid="u", session_id="s")
    )
    assert events == [RateLimit(rejected=False)]


def test_user_message_maps_to_nothing() -> None:
    assert map_sdk_message(UserMessage(content="hello")) == []
