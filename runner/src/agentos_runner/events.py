"""The runner-owned TurnEvent union: the vocabulary the runner core consumes.

``translate.py`` and ``session.py`` pattern-match on these frozen dataclasses
instead of the claude-agent-sdk message types, so the runner core never imports
``claude_agent_sdk``. Producers map onto this union at their own seam: the SDK
adapter (``adapter.map_sdk_message``), the fake session, and the OpenCode synth.

``usage`` stays a plain ``Mapping[str, int] | None`` -- the same dict shape the
SDK and OpenCode already produce -- so ``budget.py`` and ``otel.py`` consume it
unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class AssistantText:
    """A chunk of assistant answer text, plus optional turn metadata.

    ``model`` backfills the OTel generation model (first non-empty wins, idempotent);
    ``usage`` is the source message's output-token report, folded into the budget
    exactly once per source message (homed to one event by the adapter);
    ``error`` is a provider/assistant-level error code (e.g. ``authentication_failed``
    or a hard rate limit) surfaced WITHOUT terminating the turn.
    """

    text: str = ""
    model: str | None = None
    usage: Mapping[str, int] | None = None
    error: str | None = None


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation the assistant requested this turn."""

    name: str
    # Carried for a future history/resume consumer; no runtime reader yet.
    id: str = ""
    model: str | None = None
    usage: Mapping[str, int] | None = None


@dataclass(frozen=True)
class RateLimit:
    """A provider rate-limit signal; only a hard rejection is an ACI error."""

    rejected: bool = False


@dataclass(frozen=True)
class TurnResult:
    """The terminal result of a turn (authoritative text + usage)."""

    text: str = ""
    is_error: bool = False
    subtype: str = ""
    usage: Mapping[str, int] | None = None


TurnEvent = AssistantText | ToolCall | RateLimit | TurnResult
