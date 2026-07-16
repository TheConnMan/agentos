"""Reader-context propagation into nested models on the queued-turn payload.

``QueuedTurn`` crosses the Valkey Stream boundary between the dispatcher and the
worker, carrying a nested ``ReplyHandle``. The tolerant-read policy must reach
that nested model, not just the top-level one -- pydantic propagates validation
context into nested models, but decision 1 says assert it, do not assume it.
"""

import json

import pytest
from aci_protocol import QueuedTurn, ReplyHandle, parse_queued_turn
from aci_protocol.events import _READER_CONTEXT_KEY
from pydantic import ValidationError


def _turn_payload_with_unknown_fields() -> dict[str, object]:
    return {
        "event_id": "e1",
        "conversation_id": "c1",
        "author": "u1",
        "text": "hi",
        "received_at": "2026-01-01T00:00:00Z",
        "future_top": 1,
        "reply_handle": {
            "channel": "C1",
            "placeholder": "1.0",
            "future_nested": 2,
        },
    }


def test_reader_context_reaches_nested_reply_handle() -> None:
    # With the reader context, an unknown field on the TOP-LEVEL turn and an
    # unknown field on the NESTED reply handle are both ignored -- proving the
    # context propagated into ReplyHandle.
    turn = QueuedTurn.model_validate(
        _turn_payload_with_unknown_fields(),
        context={_READER_CONTEXT_KEY: True},
    )
    assert turn.event_id == "e1"
    assert turn.reply_handle.channel == "C1"


def test_nested_reply_handle_is_strict_without_reader_context() -> None:
    # Producers stay strict: without the reader context, the unknown nested field
    # is rejected on construction/validation.
    with pytest.raises(ValidationError):
        QueuedTurn.model_validate(_turn_payload_with_unknown_fields())


def test_parse_queued_turn_tolerates_unknown_top_level_and_nested_fields() -> None:
    # The sanctioned consumer decode threads the reader context, so an unknown
    # field on the TOP-LEVEL turn and an unknown field on the NESTED reply handle
    # are both tolerated (the queue-boundary counterpart to the NDJSON decoders).
    turn = parse_queued_turn(json.dumps(_turn_payload_with_unknown_fields()))
    assert turn.event_id == "e1"
    assert turn.reply_handle.channel == "C1"


def test_constructing_queued_turn_with_unknown_field_is_strict() -> None:
    # Tolerance is read-only: producers construct the model directly, where an
    # unknown field is still rejected.
    with pytest.raises(ValidationError):
        QueuedTurn(
            event_id="e1",
            conversation_id="c1",
            author="u1",
            text="hi",
            reply_handle=ReplyHandle(channel="C1", placeholder="1.0"),
            received_at="2026-01-01T00:00:00Z",
            bogus=1,
        )
