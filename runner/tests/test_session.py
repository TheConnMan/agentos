"""SessionRunner: turn streaming, interrupt reclassification, rehydrate options."""

import anyio
from aci_protocol import Event, Interrupt, SessionStatus, parse_ndjson
from agentos_runner import RunTracer, SideEffectClassifier, build_options
from agentos_runner.fake import FakeModelSession, default_turn
from agentos_runner.session import SessionRunner


def _runner(
    script_factory=default_turn, ceiling: int = 0
) -> tuple[SessionRunner, FakeModelSession]:
    fake = FakeModelSession(script_factory)
    runner = SessionRunner(
        session_factory=lambda: fake,
        ceiling=ceiling,
        tracer=RunTracer(None),
        classifier=SideEffectClassifier(),
        trace_name="t",
    )
    return runner, fake


def _drain(runner: SessionRunner, frame) -> list:
    lines: list[str] = []

    async def go() -> None:
        await runner.start()
        async for line in runner.run_inbound(frame):
            lines.append(line)

    anyio.run(go)
    return parse_ndjson("".join(lines))


def test_happy_turn_stream_shape() -> None:
    runner, fake = _runner()
    events = _drain(runner, Event(type="message", text="go", user="U", ts="1"))
    types = [e.type for e in events]
    assert types[0] == "text_delta"
    assert "tool_note" in types
    assert "side_effect_flag" in types
    assert types[-1] == "final"
    assert events[-1].status == SessionStatus.DONE
    assert fake.queries == ["go"]  # the event text was pushed into the session
    assert runner.status == SessionStatus.DONE


def test_bare_interrupt_yields_idle_final() -> None:
    runner, _ = _runner()
    events = _drain(runner, Interrupt(reason="stop"))
    assert [e.type for e in events] == ["final"]
    assert events[0].status == SessionStatus.IDLE_AWAITING_INPUT


def test_midturn_interrupt_reclassifies_final_to_idle() -> None:
    # Interrupt delivered while the turn is live: the fake truncates its replay
    # (as the SDK's native interrupt would), the turn ends without a model result,
    # and the fallback final is idle-awaiting-input rather than done.
    runner, fake = _runner()  # default_turn: several messages before the result

    lines: list[str] = []

    async def go() -> None:
        await runner.start()
        gen = runner.run_turn(Event(type="message", text="go", user="U", ts="1"))
        lines.append(await gen.__anext__())  # consume the first outbound event
        assert runner.turn_active
        await runner.interrupt("user stop")  # side-channel interrupt mid-turn
        async for line in gen:
            lines.append(line)

    anyio.run(go)
    events = parse_ndjson("".join(lines))
    assert events[-1].type == "final"
    assert events[-1].status == SessionStatus.IDLE_AWAITING_INPUT
    assert fake.interrupts >= 1


def test_build_options_carries_resume_ref() -> None:
    # Rehydrate-from-history (ADR-0003): a history ref becomes the SDK resume id.
    options = build_options(
        plugins=[],
        model="claude-opus-4-8",
        system_prompt=None,
        max_turns=20,
        max_budget_usd=5.0,
        resume="s3://history/thread-42",
    )
    assert options.resume == "s3://history/thread-42"
    assert options.max_budget_usd == 5.0
    assert options.permission_mode == "bypassPermissions"


def test_build_options_no_history_ref_is_none() -> None:
    options = build_options(
        plugins=[], model=None, system_prompt=None, max_turns=20,
        max_budget_usd=1.0, resume=None,
    )
    assert options.resume is None
