"""Report rendering: assertions target substance (task titles, accuracy numbers,
the delta) rather than exact markdown formatting."""

from __future__ import annotations

from harness_eval.models import (
    Condition,
    ConditionRollup,
    DeltaReport,
    HarnessTask,
    TaskScore,
)
from harness_eval.report import render_markdown, render_summary

_TASKS = [
    HarnessTask(
        id="t1",
        title="Author a Claude skill",
        category="build-skill",
        prompt="p",
        landmine="l",
    ),
    HarnessTask(
        id="t2",
        title="Register an MCP server",
        category="add-mcp-server",
        prompt="p",
        landmine="l",
    ),
]


def _report() -> DeltaReport:
    baseline = ConditionRollup(
        condition=Condition.BASELINE,
        total=4,
        passed=1,  # 25%
        total_tokens=800,
        total_errors=8,
    )
    with_primer = ConditionRollup(
        condition=Condition.WITH_PRIMER,
        total=4,
        passed=3,  # 75%
        total_tokens=400,
        total_errors=2,
    )
    scores = [
        TaskScore(
            task_id="t1",
            condition=Condition.WITH_PRIMER,
            success=True,
            detail="ok",
            total_tokens=100,
            errors=0,
        ),
    ]
    return DeltaReport(baseline=baseline, with_primer=with_primer, scores=scores)


def test_render_markdown_includes_titles_and_numbers() -> None:
    body = render_markdown(_report(), _TASKS)
    for task in _TASKS:
        assert task.title in body
    # Both condition accuracies and the +50-point delta appear in some form
    # ("25%"/"0.25", "75%"/"0.75", "50%"/"0.50").
    assert "25" in body
    assert "75" in body
    assert "50" in body


def test_render_summary_includes_before_and_after_accuracy() -> None:
    summary = render_summary(_report())
    assert "25" in summary
    assert "75" in summary
