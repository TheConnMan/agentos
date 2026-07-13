"""Run an eval suite against a runner and grade each case.

Each case is delivered as an ACI ``eval_case`` event (the frozen contract already
carries that event type) over the same HTTP channel the kernel uses, via the F1
``RunnerClient``. The case's answer is the runner's ``final`` text (or the
accumulated text deltas if no final arrives); the grader turns that into pass/fail.
A case whose turn errors is recorded as a failed case with the error, so one bad
case never aborts the suite.

Isolation note: cases are run sequentially against the given runner base_url, so
they share that runner session. Strong per-case isolation (a fresh sandbox per
case) is the Job-orchestration layer's choice (the API fans out eval Jobs per
version @ sha); this runner executes whatever suite it is handed against one
endpoint.
"""

from __future__ import annotations

import time

import aiohttp
from aci_protocol import ErrorEvent, Event, Final, SessionStatus, TextDelta

from ..runner_client import RunnerClient, RunnerError
from .models import EvalCase, EvalCaseResult, EvalRunResult, EvalSuite


class EvalRunner:
    """Executes eval suites against a runner endpoint and grades the answers."""

    def __init__(self, runner: RunnerClient) -> None:
        self._runner = runner

    async def run(
        self, suite: EvalSuite, *, base_url: str, version: str, token: str | None = None
    ) -> EvalRunResult:
        results = [await self._run_case(case, base_url, token) for case in suite.cases]
        return EvalRunResult(version=version, suite=suite.name, results=results)

    async def _run_case(
        self, case: EvalCase, base_url: str, token: str | None = None
    ) -> EvalCaseResult:
        start = time.monotonic()
        event = Event(type="eval_case", text=case.input, user="eval", ts="0")
        parts: list[str] = []
        final_text: str | None = None
        final_status: SessionStatus | None = None
        error_detail: str | None = None
        try:
            turn = await self._runner.start_turn(base_url, event, token=token)
            async with turn:
                async for frame in turn:
                    if isinstance(frame, TextDelta):
                        parts.append(frame.text)
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
        # Grade only a completed turn. A turn that ends idle-awaiting-input, or
        # never produced a Final at all, is not a passing answer even if its text
        # matches the grader -- this mirrors the CLI's ``turn_passes`` Done-gate
        # so the platform promotion gate keeps skill/local/cluster parity.
        if final_status is not SessionStatus.DONE:
            return EvalCaseResult(
                case_id=case.id,
                passed=False,
                output=output,
                latency_ms=_elapsed_ms(start),
                error=f"turn did not complete (status={final_status})",
            )
        return EvalCaseResult(
            case_id=case.id,
            passed=case.grader.grade(output),
            output=output,
            latency_ms=_elapsed_ms(start),
        )


def _elapsed_ms(start: float) -> float:
    return round((time.monotonic() - start) * 1000, 2)
