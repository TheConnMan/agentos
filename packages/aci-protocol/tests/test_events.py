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


def test_outbound_version_field_rejects_wrong_value() -> None:
    # The version literal is enforced at the model level, not only by the NDJSON
    # decoder, so a producer cannot construct an off-version event.
    with pytest.raises(ValidationError):
        Final(text="ok", version="9.9.9")  # type: ignore[arg-type]


def test_protocol_version_literal_matches_constant() -> None:
    # Guards the one duplicated literal in version.py against PROTOCOL_VERSION.
    assert Final(text="ok").version == PROTOCOL_VERSION


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
