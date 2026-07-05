"""Budget accounting and the per-run output-token halt."""

import anyio
from aci_protocol import Event, SessionStatus, parse_ndjson
from agentos_runner import BudgetTracker, RunTracer, SideEffectClassifier
from agentos_runner.budget import BUDGET_CLASSIFICATION
from agentos_runner.fake import FakeModelSession
from agentos_runner.session import SessionRunner
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock


def test_tracker_uses_max_not_sum() -> None:
    tracker = BudgetTracker(ceiling=100)
    tracker.add({"output_tokens": 40})
    tracker.add({"output_tokens": 90})  # cumulative-for-turn, not additive
    assert tracker.used == 90
    assert not tracker.exceeded
    tracker.add({"output_tokens": 101})
    assert tracker.exceeded


def test_zero_ceiling_disables_enforcement() -> None:
    tracker = BudgetTracker(ceiling=0)
    tracker.add({"output_tokens": 10_000})
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
        AssistantMessage(
            content=[TextBlock(text="thinking hard")],
            model="fake",
            usage={"output_tokens": 500},
        ),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", result="done", usage={"output_tokens": 500},
        ),
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
        AssistantMessage(content=[TextBlock(text="quick")], model="fake"),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", result="done", usage={"output_tokens": 999},
        ),
    ]
    runner, _ = _runner(script, ceiling=10)
    events = _run(runner, _event())
    final = events[-1]
    assert final.type == "final"
    assert final.status == SessionStatus.CLASSIFIED_FAILURE


def test_under_budget_completes_done() -> None:
    script = [
        AssistantMessage(content=[TextBlock(text="hello")], model="fake"),
        ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s", result="hello", usage={"output_tokens": 5},
        ),
    ]
    runner, _ = _runner(script, ceiling=1000)
    events = _run(runner, _event())
    assert events[-1].type == "final"
    assert events[-1].status == SessionStatus.DONE
