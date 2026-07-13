import pytest
from aci_protocol import (
    PROTOCOL_VERSION,
    ApprovalRequest,
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
    assert SessionStatus.AWAITING_APPROVAL.value == "awaiting-approval"


def test_final_defaults_optional_approval_fields_to_none() -> None:
    final = Final(text="ok")
    assert final.session_id is None
    assert final.approval_request is None


def test_awaiting_approval_final_round_trips_through_the_union() -> None:
    # A paused final carries the gated tool call so the platform builds the
    # durable record without reconstructing it from a prior tool_note.
    final = Final(
        text="paused",
        status=SessionStatus.AWAITING_APPROVAL,
        session_id="sess-1",
        approval_request=ApprovalRequest(
            tool="apply_discount",
            tool_use_id="toolu_01",
            input_digest="sha256:beef",
            prompt="Approve 30% discount?",
        ),
    )
    decoded = _OUTBOUND.validate_python(final.model_dump(mode="json"))
    assert isinstance(decoded, Final)
    assert decoded.status is SessionStatus.AWAITING_APPROVAL
    assert decoded.session_id == "sess-1"
    assert decoded.approval_request is not None
    assert decoded.approval_request.tool == "apply_discount"
    assert decoded.approval_request.tool_use_id == "toolu_01"


def test_approval_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ApprovalRequest(
            tool="t", tool_use_id="u", input_digest="d", prompt="p", extra=1  # type: ignore[call-arg]
        )
