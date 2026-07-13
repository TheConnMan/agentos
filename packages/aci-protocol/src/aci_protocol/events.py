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

    ``awaiting-approval`` is a non-terminal pause: the session has stopped on a
    human-in-the-loop approval gate (ADR-0010) and the platform suspends it
    (ADR-0003) until the approval is resolved, then resumes. It is additive to
    the three original values, so an old consumer that does not know the token
    simply cannot decode a paused frame -- a paused session never reaches a
    consumer that predates the gate.
    """

    DONE = "done"
    IDLE_AWAITING_INPUT = "idle-awaiting-input"
    CLASSIFIED_FAILURE = "classified-failure"
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


class ApprovalRequest(BaseModel):
    """The tool call that tripped an approval gate, carried on a paused final.

    Emitted by the runner on a ``Final`` whose status is ``awaiting-approval`` so
    the platform can build the durable approval record (and the human-facing
    card) without reconstructing the tool from a preceding ``tool_note``.
    ``tool_use_id`` is the SDK's per-call id and the correlation key that ties
    the runner's permission callback, the durable record, and the resume
    decision together. ``input_digest`` is a stable digest of the tool input for
    display and tamper-evidence; ``prompt`` is the human-readable ask.
    """

    model_config = _STRICT

    tool: str
    tool_use_id: str
    input_digest: str
    prompt: str


class Final(_OutboundBase):
    """The terminal (or awaiting-approval) response event, carrying the status."""

    type: Literal["final"] = "final"
    text: str
    status: SessionStatus = SessionStatus.DONE
    # The harness session id for this turn. Optional and additive: it lets the
    # worker rehydrate the exact session on resume (ADR-0003) instead of guessing
    # a history ref. Absent (None) on producers that do not surface one.
    session_id: str | None = None
    # Present only when ``status`` is ``awaiting-approval``: the gated tool call.
    approval_request: ApprovalRequest | None = None


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
