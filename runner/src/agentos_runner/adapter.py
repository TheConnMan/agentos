"""The SDK adapter seam: a ModelSession protocol and its claude-agent-sdk impl.

The runner owns exactly one long-lived model session per process (one session per
sandbox), which is the source of prompt-cache affinity across turns. The session
is driven in the SDK's **streaming-input mode**: ``query`` pushes a user message
(initial or a mid-run steer), ``receive_turn`` yields the SDK messages for the
current turn until its terminal result, and ``interrupt`` is the native hard stop.
Steering is therefore first-class, not emulated: a ``query`` issued while a turn's
``receive_turn`` iterator is live is incorporated at the next loop boundary.

The protocol is the fake seam: unit tests and the conformance suite supply a
scripted ModelSession, so the model (the only external dependency) is mocked at
this boundary and nothing above it is. ``aci-protocol`` is never mocked.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from typing import Protocol

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    RateLimitEvent,
    ResultMessage,
    SdkPluginConfig,
    TaskBudget,
    TextBlock,
    ToolUseBlock,
)

from .events import AssistantText, RateLimit, ToolCall, TurnEvent, TurnResult


class ModelSession(Protocol):
    """One long-lived model session the runner drives turn by turn."""

    async def connect(self) -> None:
        """Start the session (spawn/attach the harness), rehydrating if configured."""
        ...

    async def query(self, text: str) -> None:
        """Push a user message into the session (initial turn or mid-run steer)."""
        ...

    def receive_turn(self) -> AsyncIterator[TurnEvent]:
        """Yield TurnEvents for the current turn, ending at its terminal result."""
        ...

    async def interrupt(self) -> None:
        """Hard-stop the in-flight turn at the next safe boundary."""
        ...

    async def close(self) -> None:
        """Tear down the session."""
        ...


def map_sdk_message(message: object) -> list[TurnEvent]:
    """Map one claude-agent-sdk message to the runner TurnEvents it produces.

    This is the ONE place claude-agent-sdk dataclasses survive in the runner: the
    SDK boundary maps into the runner-owned ``TurnEvent`` union so everything above
    it (translate, session) never imports the SDK.
    """

    if isinstance(message, AssistantMessage):
        return _map_assistant(message)
    if isinstance(message, ResultMessage):
        return [
            TurnResult(
                text=message.result or "",
                is_error=message.is_error,
                subtype=message.subtype or "",
                usage=message.usage,
            )
        ]
    if isinstance(message, RateLimitEvent):
        return [RateLimit(rejected=message.rate_limit_info.status == "rejected")]
    # UserMessage, SystemMessage, and StreamEvent carry no outbound-visible
    # content in the v0.1 contract; they are intentionally dropped.
    return []


def _map_assistant(message: AssistantMessage) -> list[TurnEvent]:
    model = message.model or None
    events: list[TurnEvent] = []

    # A provider auth rejection arrives as an AssistantMessage.error with (often)
    # empty content. Prepend it as a leading AssistantText carrier so the error
    # precedes any text and survives content-less messages (the fast-fail case).
    if message.error:
        events.append(AssistantText(text="", model=model, error=message.error))

    for block in message.content:
        if isinstance(block, TextBlock):
            if block.text:
                events.append(AssistantText(text=block.text, model=model))
        elif isinstance(block, ToolUseBlock):
            events.append(ToolCall(name=block.name, id=block.id, model=model))
        # ThinkingBlock and any other block type are dropped: this is where a
        # reasoning model's thinking is discarded so it never reaches the ACI.

    # Home ``usage`` onto exactly one event so the budget increments once per
    # source message (session._drive_turn folds usage per event). Home it onto the
    # LAST mapped event, not the first: session._drive_turn folds usage per event
    # in order and halts the turn the instant the ceiling trips, so if a source
    # message's usage exceeds the budget the halt must fire only AFTER every event
    # that message produced. Homing to the first event would halt right after the
    # leading text and drop a later side-effecting ToolCall -- and with it the
    # required ``side_effect_flag`` -- letting a run whose mutating tool already
    # ran be auto-retried. Homing to the last event preserves the pre-refactor
    # guarantee that the whole message was translated as one unit before any
    # same-message budget halt. If the message has no content/error events, a
    # single carrier event delivers the usage.
    if message.usage:
        if events:
            last = events[-1]
            # Content events are only ever AssistantText/ToolCall (both carry a
            # ``usage`` field); the narrowing keeps ``dataclasses.replace`` typed.
            if isinstance(last, (AssistantText, ToolCall)):
                events[-1] = dataclasses.replace(last, usage=message.usage)
        else:
            events.append(AssistantText(text="", model=model, usage=message.usage))
    return events


def build_options(
    *,
    plugins: list[SdkPluginConfig],
    model: str | None,
    system_prompt: str | None,
    max_turns: int,
    max_budget_usd: float | None,
    resume: str | None,
    task_budget_hint: int | None = None,
    env: dict[str, str] | None = None,
) -> ClaudeAgentOptions:
    """Assemble ClaudeAgentOptions for the session.

    ``resume`` is the rehydrate path (ADR-0003, stateless-first): when a history
    ref is supplied it is passed as the SDK ``resume`` session id so a resumed
    thread reconstructs its history from the store rather than assuming a
    surviving in-RAM process.

    The three ACI budget fields map to distinct SDK controls: ``max_budget_usd``
    is the daily USD cap enforced natively; ``task_budget_hint`` becomes the SDK
    ``task_budget`` so the model self-paces (ACI section 6b, a soft hint, not a
    ceiling); and the hard per-run output-token ceiling is enforced by the runner
    itself (see budget.py).
    """

    task_budget = TaskBudget(total=task_budget_hint) if task_budget_hint else None
    return ClaudeAgentOptions(
        plugins=plugins,
        model=model,
        system_prompt=system_prompt,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        resume=resume,
        task_budget=task_budget,
        permission_mode="bypassPermissions",
        env=env or {},
    )


class ClaudeAgentSession:
    """ModelSession backed by a real claude-agent-sdk streaming-input session."""

    def __init__(self, options: ClaudeAgentOptions) -> None:
        self._options = options
        self._client = ClaudeSDKClient(options)

    async def connect(self) -> None:
        await self._client.connect()

    async def query(self, text: str) -> None:
        await self._client.query(text)

    async def receive_turn(self) -> AsyncIterator[TurnEvent]:
        async for message in self._client.receive_response():
            for event in map_sdk_message(message):
                yield event

    async def interrupt(self) -> None:
        await self._client.interrupt()

    async def close(self) -> None:
        await self._client.disconnect()
