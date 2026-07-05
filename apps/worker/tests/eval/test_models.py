"""Grader and result-rollup unit tests (pure, no server)."""

from __future__ import annotations

from agentos_worker.eval import EvalCaseResult, EvalRunResult, Grader, GraderKind


def test_exact_grader_trims_and_ignores_case_by_default() -> None:
    g = Grader(kind=GraderKind.EXACT, expected="Paris")
    assert g.grade("  paris ") is True
    assert g.grade("Paris, France") is False


def test_contains_grader() -> None:
    g = Grader(kind=GraderKind.CONTAINS, expected="4")
    assert g.grade("the answer is 4") is True
    assert g.grade("five") is False


def test_regex_grader() -> None:
    g = Grader(kind=GraderKind.REGEX, expected=r"\b4\b")
    assert g.grade("result: 4 units") is True
    assert g.grade("result: 42") is False


def test_case_sensitivity_is_honored() -> None:
    insensitive = Grader(kind=GraderKind.CONTAINS, expected="PARIS")
    assert insensitive.grade("paris") is True
    sensitive = Grader(kind=GraderKind.CONTAINS, expected="PARIS", case_sensitive=True)
    assert sensitive.grade("paris") is False


def test_run_result_rollups() -> None:
    result = EvalRunResult(
        version="v1",
        suite="basics",
        results=[
            EvalCaseResult(case_id="a", passed=True, output="4", latency_ms=1.0),
            EvalCaseResult(case_id="b", passed=False, output="x", latency_ms=1.0),
            EvalCaseResult(case_id="c", passed=True, output="ok", latency_ms=1.0),
        ],
    )
    assert result.total == 3
    assert result.passed_count == 2
    assert result.summary() == "2/3 passed"
    assert result.all_passed() is False


def test_all_passed_is_false_for_empty_suite() -> None:
    assert EvalRunResult(version="v", suite="s", results=[]).all_passed() is False
