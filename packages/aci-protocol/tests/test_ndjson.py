import json

import pytest
from aci_protocol import (
    PROTOCOL_VERSION,
    Event,
    Final,
    Interrupt,
    ProtocolVersionError,
    SessionStatus,
    SideEffectFlag,
    TextDelta,
    ToolNote,
    dump_ndjson,
    parse_inbound,
    parse_ndjson,
    parse_ndjson_line,
    to_inbound_json,
    to_ndjson_line,
)


def test_ndjson_line_has_trailing_newline() -> None:
    line = to_ndjson_line(TextDelta(text="hi"))
    assert line.endswith("\n")
    assert line.count("\n") == 1


def test_ndjson_document_roundtrips_every_type() -> None:
    events = [
        TextDelta(text="a"),
        ToolNote(text="calling", tool="search"),
        SideEffectFlag(tool="send_email"),
        Final(text="done", status=SessionStatus.DONE),
    ]
    assert parse_ndjson(dump_ndjson(events)) == events


def test_parse_skips_blank_lines() -> None:
    doc = to_ndjson_line(TextDelta(text="a")) + "\n   \n" + to_ndjson_line(Final(text="b"))
    assert len(parse_ndjson(doc)) == 2


def test_unknown_version_is_rejected() -> None:
    bad = json.dumps({"type": "final", "version": "9.9.9", "text": "x", "status": "done"})
    with pytest.raises(ProtocolVersionError):
        parse_ndjson_line(bad)


def test_missing_version_is_rejected() -> None:
    bad = json.dumps({"type": "final", "text": "x", "status": "done"})
    with pytest.raises(ProtocolVersionError):
        parse_ndjson_line(bad)


def test_current_version_is_accepted() -> None:
    good = json.dumps(
        {"type": "final", "version": PROTOCOL_VERSION, "text": "x", "status": "done"}
    )
    assert parse_ndjson_line(good) == Final(text="x")


def test_inbound_roundtrip() -> None:
    for message in (
        Event(type="message", text="hi", user="U1", ts="1.0"),
        Interrupt(reason="stop"),
    ):
        assert parse_inbound(to_inbound_json(message)) == message
