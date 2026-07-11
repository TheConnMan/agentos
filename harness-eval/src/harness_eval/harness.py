"""The harness loop: run every task under every condition, score, roll up.

Drives the injected ``driver`` across the given conditions, scores each run
through the injected ``scorer``, accumulates per-condition rollups, and returns
the paired ``DeltaReport``. The primer is forwarded only on the WITH_PRIMER
condition; the baseline condition always gets ``None``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from .driver import AgentDriver
from .models import (
    AgentRun,
    Condition,
    ConditionRollup,
    DeltaReport,
    HarnessTask,
    TaskScore,
)
from .scoring import score_run


class _Accumulator:
    """Mutable running totals for one condition."""

    def __init__(self, condition: Condition) -> None:
        self.condition = condition
        self.total = 0
        self.passed = 0
        self.total_tokens = 0
        self.total_errors = 0

    def add(self, score: TaskScore) -> None:
        self.total += 1
        if score.success:
            self.passed += 1
        self.total_tokens += score.total_tokens
        self.total_errors += score.errors

    def rollup(self) -> ConditionRollup:
        return ConditionRollup(
            condition=self.condition,
            total=self.total,
            passed=self.passed,
            total_tokens=self.total_tokens,
            total_errors=self.total_errors,
        )


def run_harness(
    tasks: Sequence[HarnessTask],
    driver: AgentDriver,
    *,
    conditions: Sequence[Condition] = (Condition.BASELINE, Condition.WITH_PRIMER),
    primer: str | None = None,
    scorer: Callable[[HarnessTask, AgentRun], TaskScore] = score_run,
) -> DeltaReport:
    """Run the full task-by-condition matrix and return the delta report."""
    if not {Condition.BASELINE, Condition.WITH_PRIMER}.issubset(conditions):
        raise ValueError(
            "run_harness requires both the baseline and with-primer conditions "
            "to compute a delta report"
        )
    accumulators = {condition: _Accumulator(condition) for condition in conditions}
    scores: list[TaskScore] = []

    for task in tasks:
        for condition in conditions:
            forwarded = primer if condition is Condition.WITH_PRIMER else None
            run = driver.run(task, condition, forwarded)
            score = scorer(task, run)
            scores.append(score)
            accumulators[condition].add(score)

    baseline = accumulators[Condition.BASELINE].rollup()
    with_primer = accumulators[Condition.WITH_PRIMER].rollup()
    return DeltaReport(baseline=baseline, with_primer=with_primer, scores=scores)
