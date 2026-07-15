"""A scripted ModelSession for tests and the conformance suite.

The fake is the mock at the adapter seam: it constructs real claude-agent-sdk
message dataclasses with canned content, so everything above it (translation,
budget, side-effect flagging, status, NDJSON, the HTTP layer) runs unmodified and
un-mocked while the model (the only external dependency) is replaced. It never
spawns the CLI or touches the network. ``aci-protocol`` is never mocked.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from typing import Any

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock


def _assistant(*blocks: Any, usage: dict[str, Any] | None = None) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model="fake-model", usage=usage)


def _result(
    *,
    text: str = "",
    is_error: bool = False,
    subtype: str = "success",
    usage: dict[str, Any] | None = None,
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="fake-session",
        result=text,
        usage=usage,
    )


def default_turn() -> list[Any]:
    """A representative successful turn: text, a side-effecting tool, then done."""

    return [
        _assistant(TextBlock(text="Looking into it")),
        _assistant(ToolUseBlock(id="t1", name="Bash", input={"command": "echo hi"})),
        _assistant(TextBlock(text="all done"), usage={"input_tokens": 20, "output_tokens": 8}),
        _result(text="all done", usage={"input_tokens": 20, "output_tokens": 8}),
    ]


# Explicit test-only marker: a query containing it makes the fake's default
# script raise an approval request (ADR-0010), so the awaiting-approval
# lifecycle round-trips fully offline (CI, `agentos skill up --fake-model`, the
# chart's sealed default pool). Everything after the marker on the same line is
# the approval summary; the routed form ``[fake:request-approval:managers]``
# additionally names an approval route (#247). Like all fake-model behavior:
# no model call, no network.
APPROVAL_MARKER = "[fake:request-approval]"
_APPROVAL_MARKER_RE = re.compile(r"\[fake:request-approval(?::([A-Za-z0-9_-]+))?\]")


def approval_turn(summary: str, route: str | None = None) -> list[Any]:
    """A turn that calls the platform approval-request tool, then ends."""

    text = "This needs sign-off; requesting approval."
    payload: dict[str, Any] = {"summary": summary}
    if route is not None:
        payload["route"] = route
    return [
        _assistant(TextBlock(text=text)),
        _assistant(
            ToolUseBlock(
                id="t1",
                name="mcp__agentos__request_approval",
                input=payload,
            )
        ),
        _result(text=text, usage={"input_tokens": 20, "output_tokens": 8}),
    ]


class FakeModelSession:
    """A ModelSession that replays a fixed script of SDK messages per turn.

    ``script_factory`` returns the messages for the next ``receive_turn``, so a
    test can vary the script across turns. ``interrupt`` truncates the current
    turn's replay at the next boundary (``truncate_on_interrupt=True``, the
    default), emulating an SDK interrupt that aborts the iterator before a result;
    set it False to model the other real shape, where the SDK still delivers a
    terminal error result after the interrupt.
    """

    def __init__(
        self,
        script_factory: Callable[[], list[Any]] | None = None,
        *,
        truncate_on_interrupt: bool = True,
    ) -> None:
        self._script_factory = script_factory or self._default_script
        self._truncate_on_interrupt = truncate_on_interrupt
        self.connected = False
        self.queries: list[str] = []
        self.interrupts = 0
        self._interrupted = False

    def _default_script(self) -> list[Any]:
        """The default per-turn script, branching on the approval marker.

        A custom ``script_factory`` bypasses this entirely, so existing tests
        keep their exact scripts; only the no-factory default (the container
        fake-model path) reacts to the marker.
        """

        last = self.queries[-1] if self.queries else ""
        match = _APPROVAL_MARKER_RE.search(last)
        if match:
            summary = last[match.end() :].strip() or "unspecified request"
            return approval_turn(summary, route=match.group(1))
        return default_turn()

    async def connect(self) -> None:
        self.connected = True

    async def query(self, text: str) -> None:
        self.queries.append(text)
        self._interrupted = False

    async def interrupt(self) -> None:
        self.interrupts += 1
        self._interrupted = True

    async def receive_turn(self) -> AsyncIterator[Any]:
        for message in self._script_factory():
            if self._interrupted and self._truncate_on_interrupt:
                return
            yield message

    async def close(self) -> None:
        self.connected = False
