"""Eval domain models: cases, graders, and results.

An eval suite is a set of cases; each case is an input prompt plus a grader that
decides pass/fail from the agent's answer. Graders are deny-by-default: a case
must name one, so a suite can never "pass" by omission. The result types roll a
run up into the ``N/M passed`` summary the PR check reports and the per-case rows
the eval matrix records per version.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class GraderKind(StrEnum):
    """How a case's expected value is compared against the agent's output."""

    EXACT = "exact"
    CONTAINS = "contains"
    REGEX = "regex"


class Grader(BaseModel):
    """A single deterministic grader. MVP scope: string-shaped checks only.

    (An LLM-judge grader is a later addition; keeping these deterministic makes
    eval results reproducible, which is the whole point of eval-as-CI.)
    """

    model_config = ConfigDict(frozen=True)

    kind: GraderKind
    expected: str
    case_sensitive: bool = False

    def grade(self, output: str) -> bool:
        """True if ``output`` satisfies this grader."""
        if self.kind is GraderKind.REGEX:
            flags = 0 if self.case_sensitive else re.IGNORECASE
            return re.search(self.expected, output, flags) is not None

        actual, expected = output, self.expected
        if not self.case_sensitive:
            actual, expected = actual.lower(), expected.lower()
        if self.kind is GraderKind.EXACT:
            return actual.strip() == expected.strip()
        return expected in actual  # CONTAINS


class EvalCase(BaseModel):
    """One eval: an input prompt and the grader that judges the answer."""

    model_config = ConfigDict(frozen=True)

    id: str
    input: str
    grader: Grader


class EvalSuite(BaseModel):
    """A named set of eval cases run together against one plugin version."""

    model_config = ConfigDict(frozen=True)

    name: str
    cases: list[EvalCase]


class EvalCaseResult(BaseModel):
    """The outcome of running one case: pass/fail, the output, and any error."""

    case_id: str
    passed: bool
    output: str
    latency_ms: float
    error: str | None = None


class EvalRunResult(BaseModel):
    """A whole suite run against one version: the per-case rows plus rollups."""

    version: str
    suite: str
    results: list[EvalCaseResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    def all_passed(self) -> bool:
        return self.total > 0 and self.passed_count == self.total

    def summary(self) -> str:
        """The one-line ``34/36 passed`` string the PR check surfaces."""
        return f"{self.passed_count}/{self.total} passed"
