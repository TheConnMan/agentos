"""Translate claude-agent-sdk messages into ACI outbound events.

This is the pure mapping at the heart of the runner: it turns each SDK message
(assistant text, tool calls, terminal result, rate-limit signal) into zero or
more ACI outbound events (text_delta / tool_note / side_effect_flag / error /
final). It is stateful only through ``TurnState`` (side-effect dedup, carried
error classification) and side-effect free otherwise, so it is unit-testable
without a session, a network, or the HTTP layer.

Budget and interrupt outcomes are *not* decided here: this layer reports the
model's own terminal status (done vs classified-failure), and the session applies
budget/interrupt overrides on top. Keeping that split is what lets the same
translation serve both the live HTTP turn and the conformance producer.
"""

from __future__ import annotations

from dataclasses import dataclass

from aci_protocol import (
    ErrorEvent,
    Final,
    OutboundEvent,
    SessionStatus,
    SideEffectFlag,
    TextDelta,
    ToolNote,
)
from claude_agent_sdk import (
    AssistantMessage,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from .otel import _GenerationSpan
from .side_effects import SideEffectClassifier


@dataclass
class TurnState:
    """Mutable per-turn state threaded through translation."""

    side_effect_emitted: bool = False
    error_classification: str | None = None
    # Assistant text streamed during the turn, accumulated so a DONE result with
    # an empty ``result`` can still deliver the model's answer. Reasoning models
    # routed through OpenRouter (e.g. z-ai/glm-5.2) emit the answer as a TextBlock
    # but their empty-signature thinking block trips the SDK's result extraction,
    # leaving ``ResultMessage.result`` empty (issue #107).
    assistant_text: str = ""


def translate_message(
    message: object,
    state: TurnState,
    classifier: SideEffectClassifier,
    gen: _GenerationSpan | None,
) -> list[OutboundEvent]:
    """Map one SDK message to the ACI outbound events it produces."""

    if isinstance(message, AssistantMessage):
        return _translate_assistant(message, state, classifier, gen)
    if isinstance(message, ResultMessage):
        return _translate_result(message, state, gen)
    if isinstance(message, RateLimitEvent):
        # status is one of allowed / allowed_warning / rejected; only a hard
        # rejection is an ACI error. The warning states are advisory (the model
        # is still allowed to continue) and must not inject a failure event into
        # an otherwise-successful run.
        if message.rate_limit_info.status == "rejected":
            state.error_classification = "rate-limit"
            return [ErrorEvent(message="model rate limit reached", classification="rate-limit")]
        return []
    # UserMessage, SystemMessage, and StreamEvent carry no outbound-visible
    # content in the v0.1 contract; they are intentionally dropped.
    return []


def _translate_assistant(
    message: AssistantMessage,
    state: TurnState,
    classifier: SideEffectClassifier,
    gen: _GenerationSpan | None,
) -> list[OutboundEvent]:
    events: list[OutboundEvent] = []

    # Backfill the generation model from the SDK's own report when AGENTOS_MODEL
    # was unset at span open (record_model no-ops once a model is already stamped).
    if gen is not None:
        gen.record_model(getattr(message, "model", None))

    error = getattr(message, "error", None)
    if error:
        state.error_classification = error
        events.append(ErrorEvent(message=f"model error: {error}", classification=error))

    for block in message.content:
        if isinstance(block, TextBlock):
            if block.text:
                state.assistant_text += block.text
                events.append(TextDelta(text=block.text))
        elif isinstance(block, ToolUseBlock):
            events.append(ToolNote(text=f"running tool {block.name}", tool=block.name))
            if gen is not None:
                gen.tool_span(block.name)
            if classifier.is_side_effecting(block.name) and not state.side_effect_emitted:
                events.append(
                    SideEffectFlag(tool=block.name, detail="non-idempotent tool executed")
                )
                state.side_effect_emitted = True
    return events


def _translate_result(
    message: ResultMessage,
    state: TurnState,
    gen: _GenerationSpan | None,
) -> list[OutboundEvent]:
    if gen is not None:
        gen.record_usage(message.usage)

    subtype = message.subtype or ""
    if message.is_error or subtype.startswith("error"):
        text = message.result or "run failed"
        events: list[OutboundEvent] = []
        if state.error_classification is None:
            events.append(
                ErrorEvent(message=text, classification=subtype or "server-error")
            )
        events.append(Final(text=text, status=SessionStatus.CLASSIFIED_FAILURE))
        return events

    # The SDK's ``result`` is authoritative when present. When it is empty on an
    # otherwise-successful turn, fall back to the assistant text streamed this turn
    # so a reasoning model whose result-extraction returned empty (issue #107)
    # still delivers its answer. Provider-agnostic: it only fires when result is
    # empty, so non-reasoning models and the fake-model path are unaffected.
    return [Final(text=message.result or state.assistant_text, status=SessionStatus.DONE)]
