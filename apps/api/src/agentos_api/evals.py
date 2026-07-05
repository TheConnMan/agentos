"""Eval matrix (K1): assemble the case-by-version grid from Langfuse.

K1's recorder writes, per case, a trace tagged version:<sha> + suite:<name> with
metadata.case_id, plus an eval_pass score (1.0/0.0). The matrix reads those back
and pivots them into rows = cases, columns = the last N versions, cells =
pass/fail/missing. Pure builder so the pivot is unit-testable off canned data.
"""

from typing import Any, Literal

from .schemas import EvalCell, EvalMatrix, EvalMatrixRow

Status = Literal["pass", "fail", "missing"]

_VERSION_TAG = "version:"


def _version_of(trace: dict[str, Any]) -> str | None:
    metadata = trace.get("metadata") or {}
    version = metadata.get("version")
    if isinstance(version, str) and version:
        return version
    for tag in trace.get("tags") or []:
        if isinstance(tag, str) and tag.startswith(_VERSION_TAG):
            return tag[len(_VERSION_TAG) :]
    return None


def build_matrix(
    traces: list[dict[str, Any]],
    scores: list[dict[str, Any]],
    suite: str,
    limit_versions: int,
) -> EvalMatrix:
    score_by_trace: dict[str, float] = {
        s["traceId"]: float(s["value"])
        for s in scores
        if s.get("traceId") is not None and s.get("value") is not None
    }

    # Latest trace per (version, case) so a re-run supersedes an older result.
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    version_last_seen: dict[str, str] = {}
    for trace in traces:
        version = _version_of(trace)
        case_id = (trace.get("metadata") or {}).get("case_id")
        if not version or not isinstance(case_id, str):
            continue
        ts = str(trace.get("timestamp") or "")
        key = (version, case_id)
        if key not in latest or ts >= str(latest[key].get("timestamp") or ""):
            latest[key] = trace
        if ts >= version_last_seen.get(version, ""):
            version_last_seen[version] = ts

    # Columns: the most recently exercised versions, newest first, capped at N.
    versions = sorted(
        version_last_seen, key=lambda v: version_last_seen[v], reverse=True
    )[:limit_versions]
    version_set = set(versions)
    cases = sorted(
        {case for (version, case) in latest if version in version_set}
    )

    def _status(version: str, case: str) -> Status:
        trace = latest.get((version, case))
        if trace is None:
            return "missing"
        value = score_by_trace.get(trace["id"])
        if value is None:
            return "missing"
        return "pass" if value >= 0.5 else "fail"

    rows = [
        EvalMatrixRow(
            case_id=case,
            cells=[
                EvalCell(version=version, status=_status(version, case))
                for version in versions
            ],
        )
        for case in cases
    ]
    return EvalMatrix(suite=suite, versions=versions, cases=cases, rows=rows)
