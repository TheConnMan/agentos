"""Eval matrix (K1): assemble the case-by-version grid from Langfuse.

K1's recorder writes, per case, a trace tagged version:<sha> + suite:<name> whose
metadata carries case_id and the pass/fail (mirrored by an eval_pass score). The
matrix reads pass/fail straight off each suite trace's metadata -- scoping to the
traces just fetched, rather than joining a globally-paginated scores query -- and
pivots them into rows = cases, columns = the last N versions, cells =
pass/fail/missing. Pure builder so the pivot is unit-testable off canned data.
"""

from typing import Any, Literal

from .schemas import EvalCell, EvalMatrix, EvalMatrixRow, EvalModelSummary

Status = Literal["pass", "fail", "missing"]

_VERSION_TAG = "version:"
_MODEL_TAG = "model:"


def _version_of(trace: dict[str, Any]) -> str | None:
    metadata = trace.get("metadata") or {}
    version = metadata.get("version")
    if isinstance(version, str) and version:
        return version
    for tag in trace.get("tags") or []:
        if isinstance(tag, str) and tag.startswith(_VERSION_TAG):
            return tag[len(_VERSION_TAG) :]
    return None


def _model_of(trace: dict[str, Any]) -> str | None:
    """The model tag/metadata on an eval trace, or None when unlabelled.

    Mirrors ``_version_of``: metadata field first (what the recorder writes
    straight onto the trace), falling back to the ``model:`` tag. A run with no
    resolved model records neither, so the case is model-unlabelled (``None``).
    """
    metadata = trace.get("metadata") or {}
    model = metadata.get("model")
    if isinstance(model, str) and model:
        return model
    for tag in trace.get("tags") or []:
        if isinstance(tag, str) and tag.startswith(_MODEL_TAG):
            return tag[len(_MODEL_TAG) :]
    return None


def _cost_of(trace: dict[str, Any]) -> float | None:
    cost = (trace.get("metadata") or {}).get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return float(cost)
    return None


def build_matrix(
    traces: list[dict[str, Any]],
    suite: str,
    limit_versions: int,
) -> EvalMatrix:
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
        passed = (trace.get("metadata") or {}).get("passed")
        if passed is None:
            return "missing"
        return "pass" if passed else "fail"

    rows = [
        EvalMatrixRow(
            case_id=case,
            cells=[
                EvalCell(
                    version=version,
                    status=_status(version, case),
                    model=(
                        _model_of(trace)
                        if (trace := latest.get((version, case))) is not None
                        else None
                    ),
                )
                for version in versions
            ],
        )
        for case in cases
    ]
    summaries = _model_summaries(latest, version_set)
    return EvalMatrix(
        suite=suite,
        versions=versions,
        cases=cases,
        rows=rows,
        models=[s.model for s in summaries],
        model_summaries=summaries,
    )


def _model_summaries(
    latest: dict[tuple[str, str], dict[str, Any]],
    version_set: set[str],
) -> list[EvalModelSummary]:
    """Roll the shown grid up per model: pass-rate + summed cost.

    Aggregates over exactly the cells that back the displayed grid (latest trace
    per (version, case), scoped to the shown version columns), so the model
    dimension and the version grid never disagree about a result. Cost sums only
    the cases that reported one; a model with no cost anywhere stays ``None``.
    """
    passed: dict[str | None, int] = {}
    total: dict[str | None, int] = {}
    cost: dict[str | None, float | None] = {}
    for (version, _case), trace in latest.items():
        if version not in version_set:
            continue
        model = _model_of(trace)
        meta_passed = (trace.get("metadata") or {}).get("passed")
        if meta_passed is None:
            continue
        total[model] = total.get(model, 0) + 1
        passed[model] = passed.get(model, 0) + (1 if meta_passed else 0)
        case_cost = _cost_of(trace)
        if case_cost is not None:
            cost[model] = (cost.get(model) or 0.0) + case_cost

    # Deterministic order: named models sorted, unlabelled (None) last.
    keys = sorted(total, key=lambda m: (m is None, m or ""))
    return [
        EvalModelSummary(
            model=model,
            passed=passed.get(model, 0),
            total=total[model],
            cost_usd=cost.get(model),
        )
        for model in keys
    ]
