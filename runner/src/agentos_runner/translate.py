"""Translate runner TurnEvents into ACI outbound events.

This is the pure mapping at the heart of the runner: it turns each runner
``TurnEvent`` (assistant text, tool calls, terminal result, rate-limit signal)
into zero or more ACI outbound events (text_delta / tool_note / side_effect_flag
/ error / final). It is stateful only through ``TurnState`` (side-effect dedup,
carried error classification) and side-effect free otherwise, so it is
unit-testable without a session, a network, or the HTTP layer.

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

from .events import AssistantText, RateLimit, ToolCall, TurnResult
from .otel import _GenerationSpan
from .side_effects import SideEffectClassifier


@dataclass
class TurnState:
    """Mutable per-turn state threaded through translation."""

    side_effect_emitted: bool = False
    error_classification: str | None = None
    # Assistant text streamed during the turn, accumulated so a DONE result with
    # an empty ``text`` can still deliver the model's answer. Reasoning models
    # routed through OpenRouter (e.g. z-ai/glm-5.2) emit the answer as an
    # AssistantText but their empty-signature thinking block trips the SDK's
    # result extraction, leaving the terminal TurnResult text empty (issue #107).
    assistant_text: str = ""


def translate_event(
    event: object,
    state: TurnState,
    classifier: SideEffectClassifier,
    gen: _GenerationSpan | None,
) -> list[OutboundEvent]:
    """Map one runner TurnEvent to the ACI outbound events it produces."""

    if isinstance(event, AssistantText):
        return _translate_assistant_text(event, state, gen)
    if isinstance(event, ToolCall):
        return _translate_tool_call(event, state, classifier, gen)
    if isinstance(event, RateLimit):
        # Only a hard rejection is an ACI error. A non-rejecting rate-limit signal
        # is advisory (the model is still allowed to continue) and must not inject
        # a failure event into an otherwise-successful run.
        if event.rejected:
            state.error_classification = "rate-limit"
            return [ErrorEvent(message="model rate limit reached", classification="rate-limit")]
        return []
    if isinstance(event, TurnResult):
        return _translate_result(event, state, gen)
    return []


def _translate_assistant_text(
    event: AssistantText,
    state: TurnState,
    gen: _GenerationSpan | None,
) -> list[OutboundEvent]:
    events: list[OutboundEvent] = []

    # Backfill the generation model from the event's own report when AGENTOS_MODEL
    # was unset at span open (record_model no-ops once a model is already stamped).
    if gen is not None:
        gen.record_model(event.model)

    if event.error:
        state.error_classification = event.error
        events.append(
            ErrorEvent(message=f"model error: {event.error}", classification=event.error)
        )

    if event.text:
        state.assistant_text += event.text
        events.append(TextDelta(text=event.text))
    return events


def _translate_tool_call(
    event: ToolCall,
    state: TurnState,
    classifier: SideEffectClassifier,
    gen: _GenerationSpan | None,
) -> list[OutboundEvent]:
    events: list[OutboundEvent] = []

    if gen is not None:
        gen.record_model(event.model)

    events.append(ToolNote(text=f"running tool {event.name}", tool=event.name))
    if gen is not None:
        gen.tool_span(event.name)
    if classifier.is_side_effecting(event.name) and not state.side_effect_emitted:
        events.append(SideEffectFlag(tool=event.name, detail="non-idempotent tool executed"))
        state.side_effect_emitted = True
    return events


def _translate_result(
    event: TurnResult,
    state: TurnState,
    gen: _GenerationSpan | None,
) -> list[OutboundEvent]:
    if gen is not None:
        gen.record_usage(event.usage)

    if event.is_error or event.subtype.startswith("error"):
        text = event.text or "run failed"
        events: list[OutboundEvent] = []
        if state.error_classification is None:
            events.append(
                ErrorEvent(message=text, classification=event.subtype or "server-error")
            )
        events.append(Final(text=text, status=SessionStatus.CLASSIFIED_FAILURE))
        return events

    # The result's ``text`` is authoritative when present. When it is empty on an
    # otherwise-successful turn, fall back to the assistant text streamed this turn
    # so a reasoning model whose result-extraction returned empty (issue #107)
    # still delivers its answer. Provider-agnostic: it only fires when text is
    # empty, so non-reasoning models and the fake-model path are unaffected.
    return [Final(text=event.text or state.assistant_text, status=SessionStatus.DONE)]
