"""Budget accounting and the per-run output-token halt."""

import anyio
from aci_protocol import Event, SessionStatus, parse_ndjson
from agentos_runner import BudgetTracker, RunTracer, SideEffectClassifier
from agentos_runner.budget import BUDGET_CLASSIFICATION
from agentos_runner.events import AssistantText, ToolCall, TurnResult
from agentos_runner.fake import FakeModelSession
from agentos_runner.session import SessionRunner


def test_tracker_sums_per_message_output() -> None:
    tracker = BudgetTracker(ceiling=100)
    tracker.add_increment({"output_tokens": 40})
    assert not tracker.exceeded
    tracker.add_increment({"output_tokens": 90})  # per-message output accumulates
    assert tracker.used == 130
    assert tracker.exceeded


def test_tracker_does_not_double_count_terminal_total() -> None:
    # An 80-token reply reported on both the assistant message and the terminal
    # result must count once, not 160, so it stays under a 100 ceiling.
    tracker = BudgetTracker(ceiling=100)
    tracker.add_increment({"output_tokens": 80})
    tracker.set_total({"output_tokens": 80})
    assert tracker.used == 80
    assert not tracker.exceeded


def test_tracker_uses_terminal_total_when_no_increments() -> None:
    tracker = BudgetTracker(ceiling=100)
    tracker.set_total({"output_tokens": 150})  # usage only on the result
    assert tracker.used == 150
    assert tracker.exceeded


def test_tracker_ignores_missing_usage() -> None:
    tracker = BudgetTracker(ceiling=100)
    tracker.add_increment(None)
    tracker.add_increment({"input_tokens": 5})  # no output_tokens field
    assert tracker.used == 0


def test_zero_ceiling_disables_enforcement() -> None:
    tracker = BudgetTracker(ceiling=0)
    tracker.add_increment({"output_tokens": 10_000})
    assert not tracker.exceeded


def _event() -> Event:
    return Event(type="message", text="hi", user="U1", ts="1.0")


def _run(runner: SessionRunner, event: Event) -> list:
    lines: list[str] = []

    async def go() -> None:
        await runner.start()
        async for line in runner.run_turn(event):
            lines.append(line)

    anyio.run(go)
    return parse_ndjson("".join(lines))


def _runner(script, ceiling: int) -> tuple[SessionRunner, FakeModelSession]:
    fake = FakeModelSession(lambda: script)
    runner = SessionRunner(
        session_factory=lambda: fake,
        ceiling=ceiling,
        tracer=RunTracer(None),
        classifier=SideEffectClassifier(),
        trace_name="t",
    )
    return runner, fake


def test_budget_halt_on_assistant_usage() -> None:
    script = [
        AssistantText(text="thinking hard", model="fake", usage={"output_tokens": 500}),
        TurnResult(text="done", usage={"output_tokens": 500}),
    ]
    runner, fake = _runner(script, ceiling=10)
    events = _run(runner, _event())

    final = events[-1]
    assert final.type == "final"
    assert final.status == SessionStatus.CLASSIFIED_FAILURE
    assert any(e.type == "error" and e.classification == BUDGET_CLASSIFICATION for e in events)
    # The run was actually halted, not just relabelled.
    assert fake.interrupts >= 1
    assert runner.status == SessionStatus.CLASSIFIED_FAILURE


def test_budget_halt_when_usage_only_on_result() -> None:
    script = [
        AssistantText(text="quick", model="fake"),
        TurnResult(text="done", usage={"output_tokens": 999}),
    ]
    runner, _ = _runner(script, ceiling=10)
    events = _run(runner, _event())
    final = events[-1]
    assert final.type == "final"
    assert final.status == SessionStatus.CLASSIFIED_FAILURE
    # The budget error must be present even when the ceiling is only crossed at
    # the terminal result, so consumers can distinguish it from a model failure.
    assert any(e.type == "error" and e.classification == BUDGET_CLASSIFICATION for e in events)


def test_side_effect_flag_survives_budget_halt() -> None:
    # A side-effecting ToolCall whose usage trips the ceiling must still emit its
    # side_effect_flag BEFORE the budget halt, so the worker's
    # no-retry-after-side-effects rule sees that a mutating tool already ran. This
    # is the exact TurnEvent sequence the fixed adapter homes usage-to-last onto:
    # the text event carries no usage, the ToolCall carries the budget-tripping
    # usage, so the halt fires only after the ToolCall has been translated.
    script = [
        AssistantText(text="a", model="m"),
        ToolCall(name="Bash", id="1", model="m", usage={"output_tokens": 500}),
        TurnResult(text="done", usage={"output_tokens": 500}),
    ]
    runner, _ = _runner(script, ceiling=10)
    events = _run(runner, _event())

    types = [e.type for e in events]
    assert "side_effect_flag" in types
    final = events[-1]
    assert final.type == "final"
    assert final.status == SessionStatus.CLASSIFIED_FAILURE
    # The side_effect_flag is emitted before the terminal final -- the tool's
    # mutation is visible to the retry rules ahead of the halt.
    assert types.index("side_effect_flag") < types.index("final")
    assert any(
        e.type == "error" and e.classification == BUDGET_CLASSIFICATION for e in events
    )


def test_no_false_halt_from_terminal_double_report() -> None:
    # Same 80 tokens reported on the assistant message and the terminal result
    # must not sum to 160 and trip a 100 ceiling.
    script = [
        AssistantText(text="hi", model="fake", usage={"output_tokens": 80}),
        TurnResult(text="hi", usage={"output_tokens": 80}),
    ]
    runner, _ = _runner(script, ceiling=100)
    events = _run(runner, _event())
    assert events[-1].type == "final"
    assert events[-1].status == SessionStatus.DONE


def test_under_budget_completes_done() -> None:
    script = [
        AssistantText(text="hello", model="fake"),
        TurnResult(text="hello", usage={"output_tokens": 5}),
    ]
    runner, _ = _runner(script, ceiling=1000)
    events = _run(runner, _event())
    assert events[-1].type == "final"
    assert events[-1].status == SessionStatus.DONE
