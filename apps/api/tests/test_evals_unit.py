"""Unit tests for the eval matrix pivot (no I/O)."""

from typing import Any

from agentos_api.evals import build_matrix


def _trace(tid: str, version: str, case: str, ts: str) -> dict[str, Any]:
    return {
        "id": tid,
        "timestamp": ts,
        "tags": ["eval", f"version:{version}", "suite:s"],
        "metadata": {"version": version, "case_id": case},
    }


def test_build_matrix_pivots_cases_by_version() -> None:
    traces = [
        _trace("t1", "shaA", "c1", "2026-07-01T00:00:00Z"),
        _trace("t2", "shaA", "c2", "2026-07-01T00:00:00Z"),
        _trace("t3", "shaB", "c1", "2026-07-02T00:00:00Z"),
        # c2 was never run on shaB -> that cell is missing.
    ]
    scores = [
        {"traceId": "t1", "value": 1.0},
        {"traceId": "t2", "value": 0.0},
        {"traceId": "t3", "value": 1.0},
    ]
    matrix = build_matrix(traces, scores, "s", 5)

    assert matrix.versions == ["shaB", "shaA"]  # most recent first
    assert matrix.cases == ["c1", "c2"]

    cells = {
        (row.case_id, cell.version): cell.status
        for row in matrix.rows
        for cell in row.cells
    }
    assert cells[("c1", "shaA")] == "pass"
    assert cells[("c1", "shaB")] == "pass"
    assert cells[("c2", "shaA")] == "fail"
    assert cells[("c2", "shaB")] == "missing"


def test_latest_trace_per_cell_wins() -> None:
    traces = [
        _trace("old", "shaA", "c1", "2026-07-01T00:00:00Z"),
        _trace("new", "shaA", "c1", "2026-07-02T00:00:00Z"),
    ]
    scores = [{"traceId": "old", "value": 0.0}, {"traceId": "new", "value": 1.0}]
    matrix = build_matrix(traces, scores, "s", 5)
    assert matrix.rows[0].cells[0].status == "pass"  # the newer run


def test_version_limit_keeps_the_most_recent_columns() -> None:
    traces = [
        _trace("t1", "shaA", "c1", "2026-07-01T00:00:00Z"),
        _trace("t2", "shaB", "c1", "2026-07-02T00:00:00Z"),
        _trace("t3", "shaC", "c1", "2026-07-03T00:00:00Z"),
    ]
    scores = [{"traceId": t, "value": 1.0} for t in ("t1", "t2", "t3")]
    matrix = build_matrix(traces, scores, "s", 2)
    assert matrix.versions == ["shaC", "shaB"]
