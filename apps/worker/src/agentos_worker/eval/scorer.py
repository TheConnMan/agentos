"""The scorer seam: how a completed turn becomes pass/fail, above the eval port.

Today scoring is a single deterministic string-grader family (:class:`Grader`
in ``models.py``): a case names a grader and the runner applies it to the final
answer text. This module lifts that decision into a swappable seam so a scorer
can look at more than the answer text -- specifically the **tool-call trajectory**
the turn produced -- without touching the frozen eval-case schema or the recorder.

The pipeline is unchanged end to end: frozen case -> scorer (swappable) -> the
existing :class:`~.recorder.LangfuseEvalRecorder`. The scorer only replaces the
inline ``case.grader.grade(output)`` call inside :class:`~.runner.EvalRunner`; it
returns a plain pass/fail (plus a human detail string), which the runner folds
into the same :class:`~.models.EvalCaseResult` the recorder already writes. So a
non-``contains`` scorer records its verdict through the identical path.

Two scorers ship here:

* :class:`GraderScorer` -- wraps the frozen :class:`Grader`, scoring the answer
  text exactly as before. It is the default, so existing suites are unaffected.
* :class:`TrajectoryScorer` -- the tier-1 **deterministic trajectory matcher**
  over the observed tool-call sequence (exact / in-order / any-order / precision
  / recall). It is configured *above the port* (a ``case_id -> spec`` mapping the
  run layer supplies), not from a new field on the frozen case -- adding a
  per-case trajectory expectation to ``EvalCase`` would change the frozen
  eval-case schema, which is deliberately out of scope here.

LLM-as-judge and hosted-eval-API scorers are the later, costlier tiers named in
the INTERFACE; they conform to the same :class:`Scorer` protocol but are not
built here.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .models import EvalCase


@dataclass(frozen=True)
class ScoreResult:
    """A scorer's verdict for one case: pass/fail plus a human-readable reason."""

    passed: bool
    detail: str | None = None


@runtime_checkable
class Scorer(Protocol):
    """The seam: turn a completed turn's observations into a pass/fail verdict.

    ``output`` is the graded answer text (the runner's ``final`` text, or the
    accumulated deltas). ``trajectory`` is the ordered tool-call sequence the turn
    produced (the ``tool`` field of each ``tool_note`` frame, in emission order).
    A scorer uses whichever it needs; the string graders ignore the trajectory,
    the trajectory matcher ignores the text.
    """

    def score(self, case: EvalCase, output: str, trajectory: Sequence[str]) -> ScoreResult: ...


class GraderScorer:
    """Default scorer: apply the case's frozen :class:`Grader` to the turn.

    Passes both the answer text and the observed tool-call trajectory to the
    grader; the grader uses whichever its ``kind`` needs (the text matchers judge
    the text, ``tool_called`` judges the trajectory). This is the same
    ``case.grader.grade(...)`` the runner used inline, now expressed through the
    seam so it is one swappable implementation among several.
    """

    def score(self, case: EvalCase, output: str, trajectory: Sequence[str]) -> ScoreResult:
        passed = case.grader.grade(output, trajectory)
        return ScoreResult(passed=passed, detail=None if passed else "grader did not match")


class TrajectoryMode(StrEnum):
    """How an observed tool-call sequence is compared against the expected one."""

    #: The observed sequence equals the expected one (order and multiplicity).
    EXACT = "exact"
    #: The expected sequence is a subsequence of the observed one (order kept,
    #: extra calls between allowed).
    IN_ORDER = "in_order"
    #: Every expected call appears in the observed sequence with at least its
    #: expected multiplicity; order is ignored.
    ANY_ORDER = "any_order"
    #: Fraction of observed calls that were expected is >= ``threshold`` (guards
    #: against extra/hallucinated tool calls).
    PRECISION = "precision"
    #: Fraction of expected tools that were observed is >= ``threshold`` (guards
    #: against missing tool calls).
    RECALL = "recall"


@dataclass(frozen=True)
class TrajectorySpec:
    """The expected tool trajectory for one case and how to compare it."""

    expected: tuple[str, ...]
    mode: TrajectoryMode = TrajectoryMode.IN_ORDER
    #: Pass threshold for the ratio modes (PRECISION / RECALL); ignored otherwise.
    threshold: float = 1.0


def _is_subsequence(expected: Sequence[str], observed: Sequence[str]) -> bool:
    """True if ``expected`` appears in ``observed`` in order (gaps allowed)."""
    it = iter(observed)
    return all(any(o == e for o in it) for e in expected)


def match_trajectory(spec: TrajectorySpec, trajectory: Sequence[str]) -> ScoreResult:
    """Grade one observed tool-call sequence against a spec (pure, testable)."""
    observed = list(trajectory)
    expected = list(spec.expected)
    detail = f"mode={spec.mode} expected={expected} observed={observed}"

    if spec.mode is TrajectoryMode.EXACT:
        passed = observed == expected
    elif spec.mode is TrajectoryMode.IN_ORDER:
        passed = _is_subsequence(expected, observed)
    elif spec.mode is TrajectoryMode.ANY_ORDER:
        passed = not (Counter(expected) - Counter(observed))
    elif spec.mode is TrajectoryMode.PRECISION:
        # Of the calls the agent made, how many were expected. No calls -> nothing
        # unexpected, so precision is vacuously 1.0.
        expected_set = set(expected)
        ratio = 1.0 if not observed else sum(o in expected_set for o in observed) / len(observed)
        passed = ratio >= spec.threshold
        detail = f"{detail} precision={ratio:.3f} threshold={spec.threshold}"
    else:  # RECALL
        # Of the expected tools, how many the agent actually used. No expectations
        # -> nothing to miss, so recall is vacuously 1.0.
        observed_set = set(observed)
        expected_set = set(expected)
        ratio = 1.0 if not expected_set else sum(e in observed_set for e in expected_set) / len(
            expected_set
        )
        passed = ratio >= spec.threshold
        detail = f"{detail} recall={ratio:.3f} threshold={spec.threshold}"

    return ScoreResult(passed=passed, detail=None if passed else detail)


@dataclass
class TrajectoryScorer:
    """Deterministic tier-1 scorer: match the observed tool calls against a spec.

    Specs are keyed by ``case.id`` and supplied by the run layer (above the eval
    port), not read from the frozen case. A case with no spec and no ``default``
    fails closed with an explanatory detail, so a misconfigured run never scores
    green by omission -- the same deny-by-default posture the grader family has.
    """

    specs: Mapping[str, TrajectorySpec] = field(default_factory=dict)
    default: TrajectorySpec | None = None

    def score(self, case: EvalCase, output: str, trajectory: Sequence[str]) -> ScoreResult:
        del output  # trajectory matcher: the answer text is irrelevant
        spec = self.specs.get(case.id, self.default)
        if spec is None:
            return ScoreResult(
                passed=False,
                detail=f"no trajectory spec for case {case.id!r}",
            )
        return match_trajectory(spec, trajectory)
