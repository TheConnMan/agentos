"""Eval endpoints (K1): the matrix grid and the PR-check report callback."""

from fastapi import APIRouter, Depends

from ..auth import require_api_key
from ..deps import GitHubReporterDep, LangfuseDep
from ..evals import build_matrix
from ..schemas import EvalMatrix, EvalReport, EvalReportResult

router = APIRouter(
    prefix="/evals", tags=["evals"], dependencies=[Depends(require_api_key)]
)


@router.get("/matrix", response_model=EvalMatrix)
async def eval_matrix(
    lf: LangfuseDep, suite: str, versions: int = 5
) -> EvalMatrix:
    # Filtered by SUITE, the real dimension on eval traces. There is
    # deliberately no agent filter: eval traces are tagged by suite + version,
    # not agent, so an agent param would be dead. Do not re-add one.
    traces = await lf.list_traces_by_tags(["eval", f"suite:{suite}"])
    scores = await lf.list_scores("eval_pass")
    return build_matrix(traces, scores, suite, versions)


@router.post("/report", response_model=EvalReportResult)
async def report_eval(
    report: EvalReport, reporter: GitHubReporterDep
) -> EvalReportResult:
    # The eval Job (or the fan-out consumer) posts its rollup here on completion;
    # the API turns it into a GitHub commit status. Posting twice is harmless
    # (GitHub statuses are last-write-wins per context), so no dedupe.
    state = await reporter.report_eval(
        report.repo_full_name,
        report.sha,
        report.passed_count,
        report.total,
        report.target_url,
    )
    return EvalReportResult(state=state, sha=report.sha)
