"""SessionRunner: owns the model session and turns inbound frames into NDJSON.

One SessionRunner wraps one long-lived ``ModelSession`` (one session per sandbox).
It is the single owner of the SDK generator: a turn is driven by ``query`` +
``receive_turn``, and that iterator is consumed by exactly one ``run_turn`` at a
time (guarded by a turn lock). Steering and interrupt are side-channel injections
into the same live session that surface on the open turn's stream, mirroring the
proven PT-2 pattern rather than opening a second consumer of the generator.

Responsibilities layered on the translation:
- **Budget:** accumulate output tokens per turn; halt with a classified-failure
  final once ``max_output_tokens_per_run`` is crossed.
- **Interrupt:** a requested interrupt reclassifies an otherwise-done final as
  idle-awaiting-input.
- **OTel:** wrap each turn in the gen_ai span tree.
- **Status:** track the last final status (done / idle-awaiting-input /
  classified-failure) for the status endpoint.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable

import anyio
from aci_protocol import (
    ErrorEvent,
    Event,
    Final,
    Interrupt,
    SessionStatus,
    to_ndjson_line,
)

from .adapter import ModelSession
from .budget import BUDGET_CLASSIFICATION, BudgetTracker
from .otel import RunTracer
from .side_effects import SideEffectClassifier
from .translate import TurnState, translate_message

SessionFactory = Callable[[], ModelSession]


class SessionRunner:
    """Drives one model session, streaming ACI NDJSON for each inbound frame."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        ceiling: int,
        tracer: RunTracer,
        classifier: SideEffectClassifier,
        trace_name: str,
        model: str | None = None,
    ) -> None:
        self._factory = session_factory
        self._ceiling = ceiling
        self._tracer = tracer
        self._classifier = classifier
        self._trace_name = trace_name
        self._model = model

        self._session: ModelSession | None = None
        self._turn_lock = anyio.Lock()
        self._interrupt_requested = False
        self._status = SessionStatus.IDLE_AWAITING_INPUT
        self._started = False

    @property
    def status(self) -> SessionStatus:
        return self._status

    @property
    def ready(self) -> bool:
        return self._started

    @property
    def turn_active(self) -> bool:
        """True while a turn is consuming the SDK generator (a steer is deliverable)."""

        return self._turn_lock.locked()

    async def start(self) -> None:
        """Create and connect the model session (rehydrating if configured)."""

        self._session = self._factory()
        await self._session.connect()
        self._started = True

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
        self._tracer.shutdown()

    async def steer(self, text: str) -> bool:
        """Inject a follow-up message into the live turn without consuming output.

        Returns False when no turn is active (the finish-race boundary F1 owns:
        the caller falls back to opening a fresh turn). The steered output appears
        on the already-open turn's NDJSON stream.
        """

        if self._session is None or not self.turn_active:
            return False
        await self._session.query(text)
        return True

    async def interrupt(self, _reason: str = "") -> None:
        """Request a hard stop; the live turn's final is reclassified to idle."""

        self._interrupt_requested = True
        if self._session is not None:
            await self._session.interrupt()

    async def run_inbound(self, message: Event | Interrupt) -> AsyncIterator[str]:
        """Produce the NDJSON a compliant runner emits for one inbound frame.

        A bare ``Interrupt`` (no active turn) yields a single idle-awaiting-input
        final, matching the ACI reference behavior; an ``Event`` runs a turn. This
        is the shared entrypoint the conformance producer validates.
        """

        if isinstance(message, Interrupt):
            yield to_ndjson_line(
                Final(text="run interrupted", status=SessionStatus.IDLE_AWAITING_INPUT)
            )
            self._status = SessionStatus.IDLE_AWAITING_INPUT
            return
        async for line in self.run_turn(message):
            yield line

    async def run_turn(self, event: Event) -> AsyncIterator[str]:
        """Run one turn, streaming ACI NDJSON lines and enforcing the budget."""

        if self._session is None:
            raise RuntimeError("session not started")

        async with self._turn_lock:
            self._interrupt_requested = False
            state = TurnState()
            tracker = BudgetTracker(ceiling=self._ceiling)

            with self._tracer.run_span(self._trace_name, self._model) as gen:
                await self._session.query(event.text)
                async for message in self._session.receive_turn():
                    tracker.add(getattr(message, "usage", None))
                    budget_hit = tracker.exceeded
                    events = translate_message(message, state, self._classifier, gen)

                    emitted_final = False
                    for outbound in events:
                        if isinstance(outbound, Final):
                            final = self._finalize(outbound, budget_hit)
                            self._status = final.status
                            yield to_ndjson_line(final)
                            emitted_final = True
                            break
                        yield to_ndjson_line(outbound)
                    if emitted_final:
                        return

                    if budget_hit:
                        await self._session.interrupt()
                        yield to_ndjson_line(
                            ErrorEvent(
                                message="output token budget exceeded",
                                classification=BUDGET_CLASSIFICATION,
                            )
                        )
                        final = Final(
                            text="run halted: output token budget exceeded",
                            status=SessionStatus.CLASSIFIED_FAILURE,
                        )
                        self._status = final.status
                        yield to_ndjson_line(final)
                        return

                # The turn iterator ended without a terminal result (e.g. an
                # interrupt aborted before the model produced one). Emit a final
                # so the stream always terminates in a final event.
                status = (
                    SessionStatus.IDLE_AWAITING_INPUT
                    if self._interrupt_requested
                    else SessionStatus.DONE
                )
                self._status = status
                yield to_ndjson_line(Final(text="", status=status))

    def _finalize(self, final: Final, budget_hit: bool) -> Final:
        """Apply budget and interrupt overrides to a model-produced final."""

        if budget_hit:
            return Final(
                text="run halted: output token budget exceeded",
                status=SessionStatus.CLASSIFIED_FAILURE,
            )
        if self._interrupt_requested and final.status == SessionStatus.DONE:
            return Final(text=final.text, status=SessionStatus.IDLE_AWAITING_INPUT)
        return final
