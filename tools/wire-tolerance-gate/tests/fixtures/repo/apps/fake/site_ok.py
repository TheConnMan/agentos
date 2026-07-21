"""A sanctioned decode: threads READER_CONTEXT explicitly, so the gate must
not flag it."""

from packages.aci_protocol.src.aci_protocol.events import READER_CONTEXT, Widget


def decode(raw: str) -> Widget:
    return Widget.model_validate_json(raw, context=READER_CONTEXT)
