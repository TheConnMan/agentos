"""ACI channel events: inbound messages and outbound NDJSON response events.

Mirrors the ACI contract v0.1 (docs/reference/detailed-architecture.md section 0):

    CHANNEL (while claimed):
      -> event      {type: message|job|eval_case, text, user, ts}
      -> interrupt  {reason}
      <- response   NDJSON: {type: text_delta|tool_note|final|error|side_effect_flag, ...}

Inbound messages are modelled as a discriminated union on a ``kind`` tag so a
single control channel can carry both event and interrupt frames self
describingly. Outbound events are a discriminated union on ``type``; every
outbound event carries a ``version`` equal to PROTOCOL_VERSION.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .version import PROTOCOL_VERSION, ProtocolVersionLiteral

# Wire contract: unknown keys are a hard error. Every lane compiles against these
# exact shapes, so a stray field means a producer and consumer disagree.
_STRICT = ConfigDict(extra="forbid")


class SessionStatus(StrEnum):
    """Terminal or awaiting status of a session, from the output contract.

    Wire tokens follow the section 0 spelling; ``classified failure`` in prose
    becomes the token ``classified-failure`` on the wire.
    """

    DONE = "done"
    IDLE_AWAITING_INPUT = "idle-awaiting-input"
    CLASSIFIED_FAILURE = "classified-failure"
    # The turn ended pending a human decision (ADR-0010, epic #22): the platform
    # suspends the session on this status and resumes it when the durable
    # approval record is resolved. Additive value; consumers that only handle
    # the original three still parse every pre-existing payload.
    AWAITING_APPROVAL = "awaiting-approval"


# --- Inbound channel messages -------------------------------------------------


class Event(BaseModel):
    """An inbound event delivered into a live session (initial or follow-up)."""

    model_config = _STRICT

    kind: Literal["event"] = "event"
    type: Literal["message", "job", "eval_case"]
    text: str
    user: str
    ts: str


class Interrupt(BaseModel):
    """A hard stop delivered on the control channel, distinct from a steer."""

    model_config = _STRICT

    kind: Literal["interrupt"] = "interrupt"
    reason: str


InboundMessage = Annotated[Event | Interrupt, Field(discriminator="kind")]


# --- Outbound NDJSON response events ------------------------------------------


class _OutboundBase(BaseModel):
    model_config = _STRICT

    version: ProtocolVersionLiteral = PROTOCOL_VERSION


class TextDelta(_OutboundBase):
    """A streamed chunk of assistant text."""

    type: Literal["text_delta"] = "text_delta"
    text: str


class ToolNote(_OutboundBase):
    """A human readable note about a tool call the harness is making."""

    type: Literal["tool_note"] = "tool_note"
    text: str
    tool: str | None = None


class Final(_OutboundBase):
    """The terminal response event, carrying the session status.

    ``approval_summary`` accompanies an ``awaiting-approval`` status (ADR-0010):
    the human-readable statement of what needs approval, captured from the
    run's approval request so the platform can persist it on the durable
    ``Approval`` record and show it to the approver. ``None`` on every other
    status.
    """

    type: Literal["final"] = "final"
    text: str
    status: SessionStatus = SessionStatus.DONE
    approval_summary: str | None = None


class ErrorEvent(_OutboundBase):
    """A classified failure surfaced to the platform."""

    type: Literal["error"] = "error"
    message: str
    classification: str | None = None


class SideEffectFlag(_OutboundBase):
    """Marks that a non-idempotent tool call executed during the run.

    Its presence gates the no-retry-after-side-effects rule (section 2b): a
    failed run carrying this flag escalates to a human instead of retrying.
    """

    type: Literal["side_effect_flag"] = "side_effect_flag"
    tool: str | None = None
    detail: str | None = None


OutboundEvent = Annotated[
    TextDelta | ToolNote | Final | ErrorEvent | SideEffectFlag,
    Field(discriminator="type"),
]

# The concrete outbound model classes, in a fixed order used by schema and code
# generation so the committed artifacts are deterministic.
OUTBOUND_EVENT_TYPES: tuple[type[_OutboundBase], ...] = (
    TextDelta,
    ToolNote,
    Final,
    ErrorEvent,
    SideEffectFlag,
)
