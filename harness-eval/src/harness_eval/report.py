"""Render a ``DeltaReport`` as markdown (full doc) or a compact summary block."""

from __future__ import annotations

from collections.abc import Sequence

from .models import Condition, DeltaReport, HarnessTask


def _pct(fraction: float) -> str:
    return f"{fraction * 100:.1f}%"


def _points(fraction: float) -> str:
    sign = "+" if fraction >= 0 else ""
    return f"{sign}{fraction * 100:.1f}"


def render_markdown(report: DeltaReport, tasks: Sequence[HarnessTask]) -> str:
    """Full repo-doc body: headline deltas plus a per-task pass/fail table."""
    outcomes: dict[tuple[str, Condition], bool] = {
        (score.task_id, score.condition): score.success for score in report.scores
    }

    def cell(task_id: str, condition: Condition) -> str:
        result = outcomes.get((task_id, condition))
        if result is None:
            return "n/a"
        return "pass" if result else "fail"

    lines = [
        "# Primer before-after harness",
        "",
        "## Headline",
        "",
        f"- Baseline accuracy: {_pct(report.baseline.accuracy)}",
        f"- With-primer accuracy: {_pct(report.with_primer.accuracy)}",
        f"- Accuracy delta: {_points(report.accuracy_delta)} points",
        f"- Mean-token delta: {report.token_delta:+.1f}",
        f"- Error-rate delta: {report.error_rate_delta:+.2f}",
        "",
        "## Per-task results",
        "",
        "| Task | Category | Baseline | With primer |",
        "| --- | --- | --- | --- |",
    ]
    for task in tasks:
        lines.append(
            f"| {task.title} | {task.category} "
            f"| {cell(task.id, Condition.BASELINE)} "
            f"| {cell(task.id, Condition.WITH_PRIMER)} |"
        )
    lines.append("")
    return "\n".join(lines)


def render_summary(report: DeltaReport) -> str:
    """Compact publishable block with the before/after headline accuracy."""
    return (
        f"Primer before-after: accuracy {_pct(report.baseline.accuracy)} -> "
        f"{_pct(report.with_primer.accuracy)} "
        f"({_points(report.accuracy_delta)} points), "
        f"mean tokens {report.token_delta:+.1f}, "
        f"error rate {report.error_rate_delta:+.2f}."
    )
