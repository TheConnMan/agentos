"""Domain-model tests: enum values, pydantic validation, derived properties,
the ``total == 0`` zero-guards, and delta-math signs."""

from __future__ import annotations

from pathlib import Path

import pytest
from harness_eval.models import (
    AgentRun,
    Condition,
    ConditionRollup,
    DeltaReport,
    HarnessTask,
    TaskScore,
)
from pydantic import ValidationError


def test_condition_members() -> None:
    assert Condition.BASELINE.value == "baseline"
    assert Condition.WITH_PRIMER.value == "with_primer"
    # StrEnum: members compare equal to their string value.
    assert Condition.BASELINE == "baseline"


def test_harness_task_is_frozen() -> None:
    task = HarnessTask(
        id="t1",
        title="Build a skill",
        category="build-skill",
        prompt="Author a skill.",
        landmine="allowed-tools not tools",
    )
    with pytest.raises(ValidationError):
        task.title = "changed"  # type: ignore[misc]


def test_harness_task_requires_all_fields() -> None:
    with pytest.raises(ValidationError):
        HarnessTask(id="t1")  # type: ignore[call-arg]


def test_agent_run_total_tokens(tmp_path: Path) -> None:
    run = AgentRun(
        task_id="t1",
        condition=Condition.BASELINE,
        workspace=tmp_path,
        transcript="hello",
        input_tokens=120,
        output_tokens=30,
        errors=2,
    )
    assert run.total_tokens == 150


def test_task_score_carries_fields(tmp_path: Path) -> None:
    score = TaskScore(
        task_id="t1",
        condition=Condition.WITH_PRIMER,
        success=True,
        detail="skill built with allowed-tools",
        total_tokens=90,
        errors=0,
    )
    assert score.success is True
    assert score.total_tokens == 90


def test_condition_rollup_properties() -> None:
    rollup = ConditionRollup(
        condition=Condition.BASELINE,
        total=4,
        passed=1,
        total_tokens=800,
        total_errors=6,
    )
    assert rollup.accuracy == pytest.approx(0.25)
    assert rollup.mean_tokens == pytest.approx(200.0)
    assert rollup.error_rate == pytest.approx(1.5)


def test_condition_rollup_zero_guard() -> None:
    rollup = ConditionRollup(
        condition=Condition.WITH_PRIMER,
        total=0,
        passed=0,
        total_tokens=0,
        total_errors=0,
    )
    assert rollup.accuracy == 0.0
    assert rollup.mean_tokens == 0.0
    assert rollup.error_rate == 0.0


def test_delta_report_math_signs() -> None:
    baseline = ConditionRollup(
        condition=Condition.BASELINE,
        total=4,
        passed=1,
        total_tokens=800,
        total_errors=8,
    )
    with_primer = ConditionRollup(
        condition=Condition.WITH_PRIMER,
        total=4,
        passed=3,
        total_tokens=400,
        total_errors=2,
    )
    report = DeltaReport(baseline=baseline, with_primer=with_primer, scores=[])

    # accuracy: 0.75 - 0.25 => positive
    assert report.accuracy_delta == pytest.approx(0.5)
    # mean tokens: 100 - 200 => negative (primer cheaper)
    assert report.token_delta == pytest.approx(-100.0)
    # error rate: 0.5 - 2.0 => negative (primer fewer errors)
    assert report.error_rate_delta == pytest.approx(-1.5)
