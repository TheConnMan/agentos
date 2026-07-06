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

import contextlib
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
from claude_agent_sdk import ResultMessage

from .adapter import ModelSession
from .budget import BUDGET_CLASSIFICATION, BudgetTracker
from .otel import RunTracer, _GenerationSpan
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
        session_id: str | None = None,
        model: str | None = None,
    ) -> None:
        self._factory = session_factory
        self._ceiling = ceiling
        self._tracer = tracer
        self._classifier = classifier
        self._trace_name = trace_name
        self._session_id = session_id
        self._model = model

        self._session: ModelSession | None = None
        self._turn_lock = anyio.Lock()
        self._interrupt_requested = False
        self._status = SessionStatus.IDLE_AWAITING_INPUT
        self._started = False
        # True only while a turn can still accept a steer: from turn start until
        # the terminal final is produced. It is cleared the instant a turn
        # terminates -- before the lock releases -- so a steer landing in the
        # finish-race window (final produced, lock not yet freed) is rejected
        # instead of writing into an already-terminal stream.
        self._turn_open = False

    @property
    def status(self) -> SessionStatus:
        return self._status

    @property
    def ready(self) -> bool:
        return self._started

    @property
    def turn_active(self) -> bool:
        """True while a turn can still accept a steer (open, pre-terminal)."""

        return self._turn_open

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

        if self._session is None or not self._turn_open:
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
            self._turn_open = True
            state = TurnState()
            tracker = BudgetTracker(ceiling=self._ceiling)

            with self._tracer.run_span(
                self._trace_name, self._model, self._session_id, event.user
            ) as gen:
                try:
                    async for line in self._drive_turn(event, state, tracker, gen):
                        yield line
                except Exception as exc:  # noqa: BLE001 - the ACI stream must
                    # always terminate in a final; a raised SDK/transport error
                    # (CLI disconnect, auth expiry, model error) becomes a
                    # classified failure rather than a truncated, final-less
                    # stream. GeneratorExit (consumer disconnect) is a
                    # BaseException and is intentionally not caught here -- the
                    # finally handles that abandonment case.
                    self._turn_open = False
                    self._status = SessionStatus.CLASSIFIED_FAILURE
                    yield to_ndjson_line(
                        ErrorEvent(message=f"runner error: {exc}", classification="runner-error")
                    )
                    yield to_ndjson_line(
                        Final(text="run failed", status=SessionStatus.CLASSIFIED_FAILURE)
                    )
                finally:
                    # If the turn never reached a terminal final (_turn_open still
                    # set), the consumer abandoned the stream mid-run (client
                    # disconnect -> GeneratorExit, or cancellation). Stop the SDK
                    # so it does not keep executing tools past the released turn
                    # lock and bleed into the next turn. Best-effort.
                    if self._turn_open and self._session is not None:
                        with contextlib.suppress(Exception):
                            await self._session.interrupt()
                    self._turn_open = False

    async def _drive_turn(
        self,
        event: Event,
        state: TurnState,
        tracker: BudgetTracker,
        gen: _GenerationSpan,
    ) -> AsyncIterator[str]:
        """Drive one turn to a terminal final (budget/interrupt overrides applied)."""

        assert self._session is not None
        await self._session.query(event.text)
        async for message in self._session.receive_turn():
            usage = getattr(message, "usage", None)
            # The terminal result carries the authoritative turn total; streaming
            # assistant messages carry per-message output. Fold them differently
            # so the same tokens are not counted twice (see BudgetTracker).
            if isinstance(message, ResultMessage):
                tracker.set_total(usage)
            else:
                tracker.add_increment(usage)
            budget_hit = tracker.exceeded
            events = translate_message(message, state, self._classifier, gen)

            for outbound in events:
                if isinstance(outbound, Final):
                    if budget_hit:
                        for line in self._budget_halt_lines():
                            yield line
                        return
                    final = self._reclassify(outbound)
                    self._status = final.status
                    self._turn_open = False
                    yield to_ndjson_line(final)
                    return
                yield to_ndjson_line(outbound)

            if budget_hit:
                # Budget crossed on a non-terminal message: stop the live run,
                # then emit the same error+final pair.
                await self._session.interrupt()
                for line in self._budget_halt_lines():
                    yield line
                return

        # The turn iterator ended without a terminal result (e.g. an interrupt
        # aborted before the model produced one). Emit a final so the stream
        # always terminates in a final event.
        status = (
            SessionStatus.IDLE_AWAITING_INPUT
            if self._interrupt_requested
            else SessionStatus.DONE
        )
        self._status = status
        self._turn_open = False
        yield to_ndjson_line(Final(text="", status=status))

    def _budget_halt_lines(self) -> list[str]:
        """The error+final pair emitted whenever the output-token ceiling trips.

        The error carries the budget classification so downstream retry rules can
        tell a budget halt from any other classified failure.
        """

        self._turn_open = False
        self._status = SessionStatus.CLASSIFIED_FAILURE
        return [
            to_ndjson_line(
                ErrorEvent(
                    message="output token budget exceeded",
                    classification=BUDGET_CLASSIFICATION,
                )
            ),
            to_ndjson_line(
                Final(
                    text="run halted: output token budget exceeded",
                    status=SessionStatus.CLASSIFIED_FAILURE,
                )
            ),
        ]

    def _reclassify(self, final: Final) -> Final:
        """Apply the interrupt override to a model-produced terminal final.

        A requested interrupt is an intentional stop, so the run is idle-awaiting-
        input regardless of the SDK's terminal subtype (a real interrupt often
        surfaces as an error result). Without the override an intentional stop
        would look like a failure and could trip F1's escalation path.
        """

        if self._interrupt_requested:
            return Final(
                text=final.text or "run interrupted",
                status=SessionStatus.IDLE_AWAITING_INPUT,
            )
        return final
