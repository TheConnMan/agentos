"""K1 eval lane: run eval_case suites through a runner, grade, and record.

The eval runner executes an ``EvalSuite`` against a runner endpoint (the same ACI
HTTP channel the kernel uses), grades each case, and records per-case scores to
Langfuse keyed by version so the eval matrix (API) and PR-check (J1) can consume
them. Run a Job with ``python -m agentos_worker.eval``.
"""

from .models import (
    EvalCase,
    EvalCaseResult,
    EvalOutcome,
    EvalRunResult,
    EvalSuite,
    ExpectedStatus,
    Grader,
    GraderKind,
)
from .recorder import SCORE_NAME, IngestionError, LangfuseEvalRecorder
from .run import load_suite, run_eval_suite
from .runner import EvalRunner
from .scorer import (
    GraderScorer,
    Scorer,
    ScoreResult,
    TrajectoryMode,
    TrajectoryScorer,
    TrajectorySpec,
    match_trajectory,
)
from .stream import (
    EvalJob,
    EvalReport,
    EvalReporter,
    EvalStreamConsumer,
    load_suite_from_bundle,
)

__all__ = [
    "SCORE_NAME",
    "EvalCase",
    "EvalCaseResult",
    "EvalJob",
    "EvalOutcome",
    "EvalReport",
    "EvalReporter",
    "EvalRunResult",
    "EvalRunner",
    "EvalStreamConsumer",
    "EvalSuite",
    "ExpectedStatus",
    "Grader",
    "GraderKind",
    "GraderScorer",
    "IngestionError",
    "LangfuseEvalRecorder",
    "Scorer",
    "ScoreResult",
    "TrajectoryMode",
    "TrajectoryScorer",
    "TrajectorySpec",
    "load_suite",
    "load_suite_from_bundle",
    "match_trajectory",
    "run_eval_suite",
]
