import pytest
from aci_protocol import (
    PROTOCOL_VERSION,
    ErrorEvent,
    Event,
    Final,
    InboundMessage,
    Interrupt,
    OutboundEvent,
    SessionStatus,
    SideEffectFlag,
    TextDelta,
    ToolNote,
)
from pydantic import TypeAdapter, ValidationError

_OUTBOUND = TypeAdapter(OutboundEvent)
_INBOUND = TypeAdapter(InboundMessage)


def test_outbound_events_default_version_to_protocol_version() -> None:
    for event in (
        TextDelta(text="hi"),
        ToolNote(text="note"),
        Final(text="done"),
        ErrorEvent(message="boom"),
        SideEffectFlag(),
    ):
        assert event.version == PROTOCOL_VERSION


def test_final_defaults_to_done_status() -> None:
    assert Final(text="ok").status is SessionStatus.DONE


def test_unknown_session_status_is_rejected() -> None:
    # Decision 3: an unknown SessionStatus is a hard decode error, never
    # degraded to a fallback. Status is control-bearing (awaiting-approval drives
    # suspend-and-wait); silently defaulting a future value to "done" would
    # finalize a turn that is actually pending a human decision.
    with pytest.raises(ValidationError):
        _OUTBOUND.validate_python(
            {
                "type": "final",
                "version": PROTOCOL_VERSION,
                "text": "x",
                "status": "invented-future-status",
            }
        )


def test_outbound_union_discriminates_on_type() -> None:
    decoded = _OUTBOUND.validate_python(
        {"type": "tool_note", "version": PROTOCOL_VERSION, "text": "n", "tool": "search"}
    )
    assert isinstance(decoded, ToolNote)
    assert decoded.tool == "search"


def test_inbound_union_discriminates_on_kind() -> None:
    event = _INBOUND.validate_python(
        {"kind": "event", "type": "message", "text": "hi", "user": "U1", "ts": "1.0"}
    )
    interrupt = _INBOUND.validate_python({"kind": "interrupt", "reason": "stop"})
    assert isinstance(event, Event)
    assert isinstance(interrupt, Interrupt)


def test_event_type_is_constrained() -> None:
    with pytest.raises(ValidationError):
        Event(type="not_a_type", text="x", user="u", ts="1.0")  # type: ignore[arg-type]


def test_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Final(text="ok", nonsense=1)  # type: ignore[call-arg]


def test_session_status_wire_values() -> None:
    assert SessionStatus.IDLE_AWAITING_INPUT.value == "idle-awaiting-input"
    assert SessionStatus.CLASSIFIED_FAILURE.value == "classified-failure"


def test_awaiting_approval_wire_value_and_final_round_trip() -> None:
    # ADR-0010 (#244): the fourth status and the optional summary field on
    # final, round-tripped through the strict wire models.
    assert SessionStatus.AWAITING_APPROVAL.value == "awaiting-approval"

    final = Final(
        text="Requesting sign-off",
        status=SessionStatus.AWAITING_APPROVAL,
        approval_summary="Give ACME a 20% discount",
    )
    wire = final.model_dump(mode="json")
    assert wire["status"] == "awaiting-approval"
    assert wire["approval_summary"] == "Give ACME a 20% discount"
    assert Final.model_validate(wire) == final

    # Pre-existing payloads (no approval fields) still parse, defaulting the
    # summary to None -- the additive-change guarantee.
    legacy = Final.model_validate({"type": "final", "text": "ok", "status": "done"})
    assert legacy.approval_summary is None
