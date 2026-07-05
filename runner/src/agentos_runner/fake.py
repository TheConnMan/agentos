"""A scripted ModelSession for tests and the conformance suite.

The fake is the mock at the adapter seam: it constructs real claude-agent-sdk
message dataclasses with canned content, so everything above it (translation,
budget, side-effect flagging, status, NDJSON, the HTTP layer) runs unmodified and
un-mocked while the model (the only external dependency) is replaced. It never
spawns the CLI or touches the network. ``aci-protocol`` is never mocked.
"""

from __future__ import annotations

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


class FakeModelSession:
    """A ModelSession that replays a fixed script of SDK messages per turn.

    ``script_factory`` returns the messages for the next ``receive_turn``, so a
    test can vary the script across turns. ``interrupt`` truncates the current
    turn's replay at the next boundary, emulating the SDK's native interrupt.
    """

    def __init__(self, script_factory: Callable[[], list[Any]] | None = None) -> None:
        self._script_factory = script_factory or default_turn
        self.connected = False
        self.queries: list[str] = []
        self.interrupts = 0
        self._interrupted = False

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
            if self._interrupted:
                return
            yield message

    async def close(self) -> None:
        self.connected = False
