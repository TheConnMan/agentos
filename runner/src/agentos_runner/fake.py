"""A scripted ModelSession for tests and the conformance suite.

The fake is the mock at the adapter seam: it emits runner ``TurnEvent``s with
canned content, so everything above it (translation, budget, side-effect
flagging, status, NDJSON, the HTTP layer) runs unmodified and un-mocked while the
model (the only external dependency) is replaced. It never spawns the CLI or
touches the network. ``aci-protocol`` is never mocked.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

from .events import AssistantText, ToolCall, TurnEvent, TurnResult


def default_turn() -> list[TurnEvent]:
    """A representative successful turn: text, a side-effecting tool, then done."""

    return [
        AssistantText(text="Looking into it", model="fake-model"),
        ToolCall(name="Bash", id="t1", model="fake-model"),
        AssistantText(
            text="all done", model="fake-model", usage={"input_tokens": 20, "output_tokens": 8}
        ),
        TurnResult(text="all done", usage={"input_tokens": 20, "output_tokens": 8}),
    ]


class FakeModelSession:
    """A ModelSession that replays a fixed script of TurnEvents per turn.

    ``script_factory`` returns the events for the next ``receive_turn``, so a
    test can vary the script across turns. ``interrupt`` truncates the current
    turn's replay at the next boundary (``truncate_on_interrupt=True``, the
    default), emulating an SDK interrupt that aborts the iterator before a result;
    set it False to model the other real shape, where the SDK still delivers a
    terminal error result after the interrupt.
    """

    def __init__(
        self,
        script_factory: Callable[[], list[TurnEvent]] | None = None,
        *,
        truncate_on_interrupt: bool = True,
    ) -> None:
        self._script_factory = script_factory or default_turn
        self._truncate_on_interrupt = truncate_on_interrupt
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

    async def receive_turn(self) -> AsyncIterator[TurnEvent]:
        for event in self._script_factory():
            if self._interrupted and self._truncate_on_interrupt:
                return
            yield event

    async def close(self) -> None:
        self.connected = False
