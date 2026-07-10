"""Frozen ACI session protocol and NDJSON event types.

This package is the single source of truth for the Agent Container Interface
(ACI) contract v0.1 (docs/reference/detailed-architecture.md section 0). It is a
frozen interface: every lane compiles against it across three languages
(Pydantic here, generated TypeScript and Rust from the committed JSON Schema). A
change stops the current task and escalates to the orchestrator, and must bump
PROTOCOL_VERSION and regenerate the committed schema and types.
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
    to_inbound_json,
    to_ndjson_line,
)
from .queue import QueuedSlackEvent
from .reference import reference_producer
from .session import Budget, OtelConfig, SessionConfig
from .version import PROTOCOL_VERSION

__version__ = "0.0.0"

__all__ = [
    "PROTOCOL_VERSION",
    "__version__",
    # session setup
    "SessionConfig",
    "Budget",
    "OtelConfig",
    # queue
    "QueuedSlackEvent",
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
    "ProtocolVersionError",
    # conformance
    "run_conformance",
    "ConformanceReport",
    "Producer",
    "reference_producer",
]
