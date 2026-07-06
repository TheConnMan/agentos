"""Eval endpoints (K1): the matrix grid and the PR-check report callback."""

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import require_api_key
from ..deps import GitHubReporterDep, LangfuseDep
from ..evals import build_matrix
from ..github_checks import GitHubReportError
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
    return build_matrix(traces, suite, versions)


@router.post("/report", response_model=EvalReportResult)
async def report_eval(
    report: EvalReport, reporter: GitHubReporterDep
) -> EvalReportResult:
    # The eval Job (or the fan-out consumer) posts its rollup here on completion;
    # the API turns it into a GitHub commit status. Posting twice is harmless
    # (GitHub statuses are last-write-wins per context), so no dedupe.
    try:
        state = await reporter.report_eval(
            report.repo_full_name,
            report.sha,
            report.passed_count,
            report.total,
            report.target_url,
        )
    except GitHubReportError as exc:
        # A GitHub rejection is a caller/input problem (unknown repo or commit,
        # bad token), not an API server fault: surface it as a 4xx with the
        # upstream detail instead of a bare 500. A GitHub 404 means the repo or
        # commit does not exist; other 4xx rejections map to 422; only a genuine
        # GitHub server fault (5xx) stays a 5xx (502 Bad Gateway).
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"repo or commit not found on GitHub: "
                f"{report.repo_full_name}@{report.sha}",
            ) from exc
        if 400 <= exc.status_code < 500:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"GitHub rejected the commit-status post "
                f"({exc.status_code}): {exc.detail}",
            ) from exc
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"GitHub commit-status API error ({exc.status_code})",
        ) from exc
    return EvalReportResult(state=state, sha=report.sha)
