"""K1 eval lane: run eval_case suites through a runner, grade, and record.

The eval runner executes an ``EvalSuite`` against a runner endpoint (the same ACI
HTTP channel the kernel uses), grades each case, and records per-case scores to
Langfuse keyed by version so the eval matrix (API) and PR-check (J1) can consume
them. Run a Job with ``python -m agentos_worker.eval``.
"""

from .models import (
    EvalCase,
    EvalCaseResult,
    EvalRunResult,
    EvalSuite,
    Grader,
    GraderKind,
)
from .recorder import SCORE_NAME, LangfuseEvalRecorder
from .run import load_suite, run_eval_suite
from .runner import EvalRunner

__all__ = [
    "SCORE_NAME",
    "EvalCase",
    "EvalCaseResult",
    "EvalRunResult",
    "EvalRunner",
    "EvalSuite",
    "Grader",
    "GraderKind",
    "LangfuseEvalRecorder",
    "load_suite",
    "run_eval_suite",
]
