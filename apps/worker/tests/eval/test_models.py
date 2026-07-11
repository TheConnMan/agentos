"""Grader and result-rollup unit tests (pure, no server)."""

from __future__ import annotations

from pathlib import Path

import pytest
from agentos_worker.eval import (
    EvalCaseResult,
    EvalRunResult,
    EvalSuite,
    Grader,
    GraderKind,
)
from pydantic import ValidationError


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


# --- Frozen eval-case contract (issue #8) -------------------------------------
# These lock Section 3 of the plan: a suite must carry at least one case, a regex
# grader compiles eagerly, the committed cross-language fixture is
# platform-loadable, and the retired array form no longer loads. They fail
# against current main because models.py lacks the min_length and the regex
# validator.


def test_empty_suite_is_invalid() -> None:
    """A suite with zero cases can never pass, so the frozen schema rejects it."""
    with pytest.raises(ValidationError):
        EvalSuite(name="s", cases=[])


def test_invalid_regex_grader_rejected_at_parse() -> None:
    """An uncompilable regex pattern is rejected eagerly when the grader is built."""
    with pytest.raises(ValidationError):
        Grader(kind=GraderKind.REGEX, expected="(unclosed")


def test_valid_regex_grader_constructs_and_grades() -> None:
    """A valid regex grader builds and searches the output."""
    grader = Grader(kind=GraderKind.REGEX, expected="wea.her")
    assert grader.grade("weather") is True


def test_committed_fixture_parses_and_grades(eval_cases_example_path: Path) -> None:
    """The committed cross-language fixture loads as an EvalSuite and its single
    smoke grader (contains "") grades any completed turn's text True, proving the
    scaffold output is platform-loadable (the latent bug in issue #8)."""
    suite = EvalSuite.model_validate_json(
        eval_cases_example_path.read_text(encoding="utf-8")
    )
    assert len(suite.cases) == 1
    grader = suite.cases[0].grader
    assert grader.kind == GraderKind.CONTAINS
    assert grader.expected == ""
    assert grader.grade("anything at all") is True
    assert grader.grade("") is True


def test_old_array_form_is_rejected() -> None:
    """The retired CLI array form does not load as an EvalSuite, so exactly one
    schema is loadable anywhere."""
    old = '[{"name": "a", "input": "b", "expect_contains": ["c"]}]'
    with pytest.raises(ValidationError):
        EvalSuite.model_validate_json(old)
