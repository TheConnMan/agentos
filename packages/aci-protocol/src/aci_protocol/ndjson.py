"""NDJSON serialization for the outbound response stream.

The response channel is newline-delimited JSON: one outbound event per line.
These helpers are the single sanctioned encoder and decoder. The decoder
enforces the protocol version: an event whose ``version`` is missing or does not
match PROTOCOL_VERSION is rejected with ProtocolVersionError rather than being
silently accepted, so a runner speaking a different version fails loudly.
"""

import json
from collections.abc import Iterable, Iterator
from typing import Any

from pydantic import TypeAdapter

from .events import (
    ErrorEvent,
    Final,
    InboundMessage,
    OutboundEvent,
    SideEffectFlag,
    TextDelta,
    ToolNote,
)
from .version import PROTOCOL_VERSION

OutboundEventModel = TextDelta | ToolNote | Final | ErrorEvent | SideEffectFlag

_OUTBOUND_ADAPTER: TypeAdapter[OutboundEventModel] = TypeAdapter(OutboundEvent)
_INBOUND_ADAPTER: TypeAdapter[Any] = TypeAdapter(InboundMessage)


class ProtocolVersionError(ValueError):
    """Raised when a decoded event declares an incompatible protocol version."""


def to_ndjson_line(event: OutboundEventModel) -> str:
    """Encode one outbound event as a single NDJSON line (trailing newline)."""

    return event.model_dump_json() + "\n"


def dump_ndjson(events: Iterable[OutboundEventModel]) -> str:
    """Encode a sequence of outbound events as an NDJSON document."""

    return "".join(to_ndjson_line(e) for e in events)


def parse_ndjson_line(line: str) -> OutboundEventModel:
    """Decode one NDJSON line into a validated outbound event.

    Raises ProtocolVersionError if the line omits ``version`` or declares a
    version other than PROTOCOL_VERSION. Raises pydantic ValidationError for a
    structurally invalid event.
    """

    raw = json.loads(line)
    if not isinstance(raw, dict):
        raise ProtocolVersionError(f"expected a JSON object per line, got {type(raw).__name__}")
    version = raw.get("version")
    if version != PROTOCOL_VERSION:
        raise ProtocolVersionError(
            f"unsupported protocol version {version!r}; this build speaks {PROTOCOL_VERSION!r}"
        )
    return _OUTBOUND_ADAPTER.validate_python(raw)


def parse_ndjson(text: str) -> list[OutboundEventModel]:
    """Decode an NDJSON document, skipping blank lines."""

    return list(iter_ndjson(text))


def iter_ndjson(text: str) -> Iterator[OutboundEventModel]:
    """Lazily decode an NDJSON document, skipping blank lines."""

    for line in text.splitlines():
        if line.strip():
            yield parse_ndjson_line(line)


def parse_inbound(raw: str | dict[str, Any]) -> Any:
    """Decode an inbound channel message (event or interrupt) from JSON."""

    data = json.loads(raw) if isinstance(raw, str) else raw
    return _INBOUND_ADAPTER.validate_python(data)


def to_inbound_json(message: Any) -> str:
    """Encode an inbound channel message to a JSON string."""

    return _INBOUND_ADAPTER.dump_json(message).decode("utf-8")


__all__ = [
    "PROTOCOL_VERSION",
    "ProtocolVersionError",
    "to_ndjson_line",
    "dump_ndjson",
    "parse_ndjson_line",
    "parse_ndjson",
    "iter_ndjson",
    "parse_inbound",
    "to_inbound_json",
]
