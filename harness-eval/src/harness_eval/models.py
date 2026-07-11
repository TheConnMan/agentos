"""Domain models for the primer before-after harness.

Frozen configs plus derived rollups. A ``ConditionRollup`` accumulates one
condition's outcomes (baseline or with-primer); a ``DeltaReport`` pairs the two
and exposes the before-after deltas. All rate properties zero-guard an empty
rollup back to ``0.0`` so an unrun condition never divides by zero.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class Condition(StrEnum):
    """The two conditions each task runs under."""

    BASELINE = "baseline"
    WITH_PRIMER = "with_primer"


class HarnessTask(BaseModel):
    """One realistic AgentOS task the agent is asked to perform.

    ``landmine`` names the primer-taught gotcha the task keys on (the thing an
    unprimed agent tends to get wrong).
    """

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    category: str
    prompt: str
    landmine: str


class AgentRun(BaseModel):
    """The result of running one task under one condition.

    ``workspace`` is the directory the agent produced; the scorer grades it.
    """

    task_id: str
    condition: Condition
    workspace: Path
    transcript: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    errors: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class TaskScore(BaseModel):
    """The graded outcome of a single ``AgentRun``."""

    task_id: str
    condition: Condition
    success: bool
    detail: str
    total_tokens: int
    errors: int


class ConditionRollup(BaseModel):
    """Accumulated outcomes for one condition across every task."""

    condition: Condition
    total: int
    passed: int
    total_tokens: int
    total_errors: int

    @property
    def accuracy(self) -> float:
        if self.total == 0:
            return 0.0
        return self.passed / self.total

    @property
    def mean_tokens(self) -> float:
        if self.total == 0:
            return 0.0
        return self.total_tokens / self.total

    @property
    def error_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.total_errors / self.total


class DeltaReport(BaseModel):
    """The two condition rollups plus every per-run score, with the deltas.

    Each delta is ``with_primer - baseline`` so a positive accuracy delta and a
    negative token/error delta read as the primer helping.
    """

    baseline: ConditionRollup
    with_primer: ConditionRollup
    scores: list[TaskScore]

    @property
    def accuracy_delta(self) -> float:
        return self.with_primer.accuracy - self.baseline.accuracy

    @property
    def token_delta(self) -> float:
        return self.with_primer.mean_tokens - self.baseline.mean_tokens

    @property
    def error_rate_delta(self) -> float:
        return self.with_primer.error_rate - self.baseline.error_rate
