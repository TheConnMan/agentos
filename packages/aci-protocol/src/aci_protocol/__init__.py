"""The ACI session protocol and NDJSON event types.

This package is the single source of truth for the Agent Container Interface
(ACI) contract (docs/reference/detailed-architecture.md section 0). Every lane
compiles against it across three languages (Pydantic here, generated TypeScript
and Rust from the committed JSON Schema). The wire is strict producers, tolerant
consumers: constructing an event with an unknown field is an error, but a
consumer decoding the wire ignores fields it does not model. The version is
semver; a consumer accepts a compatible wire version (same major.minor under 0.x)
via ``is_compatible``. A contract change bumps PROTOCOL_VERSION per the change
class and regenerates the committed schema and types.
"""

from .conformance import ConformanceReport, Producer, run_conformance
from .events import (
    OUTBOUND_EVENT_TYPES,
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
from .ndjson import (
    ProtocolVersionError,
    dump_ndjson,
    iter_ndjson,
    parse_inbound,
    parse_ndjson,
    parse_ndjson_line,
    parse_queued_turn,
    to_inbound_json,
    to_ndjson_line,
)
from .reference import reference_producer
from .session import Budget, OtelConfig, SessionConfig
from .turn import QueuedTurn, ReplyHandle
from .version import PROTOCOL_VERSION, is_compatible

__version__ = "0.0.0"

__all__ = [
    "PROTOCOL_VERSION",
    "is_compatible",
    "__version__",
    # session setup
    "SessionConfig",
    "Budget",
    "OtelConfig",
    # queue turn payload (the ingress job the worker consumes)
    "QueuedTurn",
    "ReplyHandle",
    # inbound
    "Event",
    "Interrupt",
    "InboundMessage",
    # outbound
    "TextDelta",
    "ToolNote",
    "Final",
    "ErrorEvent",
    "SideEffectFlag",
    "OutboundEvent",
    "OUTBOUND_EVENT_TYPES",
    "SessionStatus",
    # ndjson
    "to_ndjson_line",
    "dump_ndjson",
    "parse_ndjson_line",
    "parse_ndjson",
    "iter_ndjson",
    "parse_inbound",
    "to_inbound_json",
    "parse_queued_turn",
    "ProtocolVersionError",
    # conformance
    "run_conformance",
    "ConformanceReport",
    "Producer",
    "reference_producer",
]
