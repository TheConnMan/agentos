"""Cross-language bound gate (#490): the Python OutboundMessage model asserts
against the SAME committed corpus the Rust adapter (cli/src/channel.rs) checks, so
a field-bound change on one side without the other fails a corpus test.
"""

import json
from pathlib import Path

import pytest
from channel_protocol.models import OutboundMessage
from pydantic import ValidationError

_CORPUS = json.loads(
    (Path(__file__).parents[1] / "schema" / "channel-protocol.corpus.json").read_text(
        encoding="utf-8"
    )
)


def test_valid_messages_pass_the_model() -> None:
    for message in _CORPUS["valid"]:
        OutboundMessage.model_validate(message)


def test_out_of_bounds_messages_are_rejected() -> None:
    for entry in _CORPUS["invalid"]:
        with pytest.raises(ValidationError):
            OutboundMessage.model_validate(entry["message"])
