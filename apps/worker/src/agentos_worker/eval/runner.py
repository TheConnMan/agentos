"""Run an eval suite against a runner and grade each case.

Each case is delivered as an ACI ``eval_case`` event (the frozen contract already
carries that event type) over the same HTTP channel the kernel uses, via the F1
``RunnerClient``. The case's answer is the runner's ``final`` text (or the
accumulated text deltas if no final arrives); the grader turns that into pass/fail.
A case whose turn errors is recorded as a failed case with the error, so one bad
case never aborts the suite.

Isolation note: cases run sequentially against one runner base_url, but each
case runs in a *fresh conversation* by default (#550). Before a case the runner
issues a ``reset`` (``POST /v1/reset``) that discards the runner's conversation,
so a case can never answer from an earlier case's history instead of actually
invoking its tools -- a false green for a side-effecting agent, and a silent
order-dependence in the suite. A case that sets ``shared_history=True`` opts out
of the reset and deliberately inherits the prior case's conversation (a
multi-turn scenario expressed as ordered cases). Strong *sandbox* isolation (a
fresh pod per case) remains the Job-orchestration layer's choice (the API fans
out eval Jobs per version @ sha); the fresh-conversation reset is the per-case
guarantee this in-process runner gives on one endpoint.
"""

from __future__ import annotations

import time

import aiohttp
from aci_protocol import ErrorEvent, Event, Final, SessionStatus, TextDelta, ToolNote

from ..runner_client import RunnerClient, RunnerError
from .models import EvalCase, EvalCaseResult, EvalRunResult, EvalSuite
from .scorer import GraderScorer, Scorer


class EvalRunner:
    """Executes eval suites against a runner endpoint and scores the answers.

    Scoring goes through the swappable :class:`~.scorer.Scorer` seam. The default
    is :class:`~.scorer.GraderScorer`, which applies the case's frozen grader to
    the answer text (behavior-preserving); pass a different scorer (e.g. a
    :class:`~.scorer.TrajectoryScorer` over the tool-call sequence) to score the
    same turns differently. The runner still owns the completion gate (a
    classified-failure or non-``done`` turn fails regardless of the scorer), so a
    scorer only ever judges a turn that actually completed.
    """

    def __init__(self, runner: RunnerClient, *, scorer: Scorer | None = None) -> None:
        self._runner = runner
        self._scorer: Scorer = scorer if scorer is not None else GraderScorer()

    async def run(
        self,
        suite: EvalSuite,
        *,
        base_url: str,
        version: str,
        token: str | None = None,
        model: str | None = None,
    ) -> EvalRunResult:
        results: list[EvalCaseResult] = []
        for case in suite.cases:
            # Fresh conversation by default (#550): reset the runner before a case
            # so it cannot inherit an earlier case's history. A shared_history
            # case skips the reset and deliberately chains onto what preceded it.
            if not case.shared_history:
                isolation_error = await self._isolate(base_url, token)
                if isolation_error is not None:
                    # Could not establish isolation: fail the case rather than run
                    # it against a possibly-leaked conversation (a false green is
                    # exactly what #550 removes). One bad reset never aborts the
                    # suite, matching the per-case error handling in _run_case.
                    results.append(
                        EvalCaseResult(
                            case_id=case.id,
                            passed=False,
                            output="",
                            latency_ms=0.0,
                            error=isolation_error,
                        )
                    )
                    continue
            results.append(await self._run_case(case, base_url, token))
        return EvalRunResult(
            version=version, suite=suite.name, results=results, model=model
        )

    async def _isolate(self, base_url: str, token: str | None) -> str | None:
        """Reset the runner's conversation; return an error string on failure.

        None on success. A failed reset means the fresh-conversation guarantee
        could not be established, so the caller fails the case instead of running
        it against a leaked history.
        """
        try:
            await self._runner.reset(base_url, token)
        except (RunnerError, aiohttp.ClientError, TimeoutError) as exc:
            return f"failed to reset conversation for isolation: {exc}"
        return None

    async def _run_case(
        self, case: EvalCase, base_url: str, token: str | None = None
    ) -> EvalCaseResult:
        start = time.monotonic()
        event = Event(type="eval_case", text=case.input, user="eval", ts="0")
        parts: list[str] = []
        trajectory: list[str] = []
        final_text: str | None = None
        final_status: SessionStatus | None = None
        error_detail: str | None = None
        try:
            turn = await self._runner.start_turn(base_url, event, token=token)
            async with turn:
                async for frame in turn:
                    if isinstance(frame, TextDelta):
                        parts.append(frame.text)
                    elif isinstance(frame, ToolNote):
                        # The tool-call trajectory the scorer seam can match on.
                        if frame.tool is not None:
                            trajectory.append(frame.tool)
                    elif isinstance(frame, Final):
                        final_text = frame.text
                        final_status = frame.status
                    elif isinstance(frame, ErrorEvent):
                        error_detail = frame.classification or frame.message
        except (RunnerError, aiohttp.ClientError, TimeoutError) as exc:
            return EvalCaseResult(
                case_id=case.id,
                passed=False,
                output="",
                latency_ms=_elapsed_ms(start),
                error=str(exc),
            )

        output = final_text if final_text is not None else "".join(parts)
        # A runner-reported failure (budget/model/runner error) fails the case
        # regardless of the grader, so a classified-failure final whose text
        # happens to contain the expected string can never turn a PR check green.
        if final_status is SessionStatus.CLASSIFIED_FAILURE:
            return EvalCaseResult(
                case_id=case.id,
                passed=False,
                output=output,
                latency_ms=_elapsed_ms(start),
                error=error_detail or "runner reported a classified failure",
            )
        # Gate on the case's expected terminal status. A case asserts its terminal
        # status (default ``done``, or ``awaiting-approval`` for a gate-blocked
        # case), so a turn that ends in any other status -- or never produced a
        # Final at all -- is not a passing answer even if its text matches the
        # grader. This mirrors the CLI's ``expect_status`` gate so the platform
        # promotion gate keeps skill/local/cluster parity. ``final_status`` may be
        # None and the two enums are distinct, so compare defensively by value.
        expected = case.expect_status
        if final_status is None or final_status.value != expected.value:
            return EvalCaseResult(
                case_id=case.id,
                passed=False,
                output=output,
                latency_ms=_elapsed_ms(start),
                error=f"turn ended {final_status}, expected status {expected.value!r}",
            )
        # The turn completed cleanly, so a negative verdict is the scorer saying
        # "no", not a runner/turn error -- keep `error` reserved for the latter
        # (exceptions, classified failures, incomplete turns handled above).
        verdict = self._scorer.score(case, output, trajectory)
        return EvalCaseResult(
            case_id=case.id,
            passed=verdict.passed,
            output=output,
            latency_ms=_elapsed_ms(start),
        )


def _elapsed_ms(start: float) -> float:
    return round((time.monotonic() - start) * 1000, 2)
