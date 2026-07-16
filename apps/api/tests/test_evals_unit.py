"""Unit tests for the eval matrix pivot (no I/O)."""

from typing import Any

from agentos_api.evals import build_matrix


def _trace(tid: str, version: str, case: str, ts: str, passed: bool) -> dict[str, Any]:
    return {
        "id": tid,
        "timestamp": ts,
        "tags": ["eval", f"version:{version}", "suite:s"],
        "metadata": {"version": version, "case_id": case, "passed": passed},
    }


def test_build_matrix_pivots_cases_by_version() -> None:
    traces = [
        _trace("t1", "shaA", "c1", "2026-07-01T00:00:00Z", passed=True),
        _trace("t2", "shaA", "c2", "2026-07-01T00:00:00Z", passed=False),
        _trace("t3", "shaB", "c1", "2026-07-02T00:00:00Z", passed=True),
        # c2 was never run on shaB -> that cell is missing.
    ]
    matrix = build_matrix(traces, "s", 5)

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
        _trace("old", "shaA", "c1", "2026-07-01T00:00:00Z", passed=False),
        _trace("new", "shaA", "c1", "2026-07-02T00:00:00Z", passed=True),
    ]
    matrix = build_matrix(traces, "s", 5)
    assert matrix.rows[0].cells[0].status == "pass"  # the newer run


def test_version_limit_keeps_the_most_recent_columns() -> None:
    traces = [
        _trace("t1", "shaA", "c1", "2026-07-01T00:00:00Z", passed=True),
        _trace("t2", "shaB", "c1", "2026-07-02T00:00:00Z", passed=True),
        _trace("t3", "shaC", "c1", "2026-07-03T00:00:00Z", passed=True),
    ]
    matrix = build_matrix(traces, "s", 2)
    assert matrix.versions == ["shaC", "shaB"]


def _mtrace(
    tid: str,
    version: str,
    case: str,
    ts: str,
    passed: bool,
    model: str | None,
    cost: float | None = None,
) -> dict[str, Any]:
    tags = ["eval", f"version:{version}", "suite:s"]
    if model:
        tags.append(f"model:{model}")
    meta: dict[str, Any] = {
        "version": version,
        "case_id": case,
        "passed": passed,
        "model": model,
    }
    if cost is not None:
        meta["cost_usd"] = cost
    return {"id": tid, "timestamp": ts, "tags": tags, "metadata": meta}


def test_model_dimension_rolls_up_pass_rate_and_cost_per_model() -> None:
    # Same suite run across two models: the matrix slices pass-rate and summed
    # cost per model (issue #255). opus: 2/2 passed, $0.03; sonnet: 1/2, $0.01.
    traces = [
        _mtrace("o1", "shaA", "c1", "2026-07-01T00:00:00Z", True, "opus", 0.02),
        _mtrace("o2", "shaA", "c2", "2026-07-01T00:00:00Z", True, "opus", 0.01),
        _mtrace("s1", "shaB", "c1", "2026-07-02T00:00:00Z", True, "sonnet", 0.006),
        _mtrace("s2", "shaB", "c2", "2026-07-02T00:00:00Z", False, "sonnet", 0.004),
    ]
    matrix = build_matrix(traces, "s", 5)

    assert matrix.models == ["opus", "sonnet"]
    summaries = {m.model: m for m in matrix.model_summaries}
    assert summaries["opus"].passed == 2
    assert summaries["opus"].total == 2
    assert summaries["opus"].pass_rate == 1.0
    assert abs(summaries["opus"].cost_usd - 0.03) < 1e-9
    assert summaries["sonnet"].passed == 1
    assert summaries["sonnet"].total == 2
    assert summaries["sonnet"].pass_rate == 0.5
    assert abs(summaries["sonnet"].cost_usd - 0.01) < 1e-9


def test_same_version_sweep_keeps_every_model() -> None:
    """A sweep (#526) runs the SAME suite + version across N models, so those
    traces collide on (version, case) and would collapse to one in the grid dedup.
    The per-model rollup keys on the model too, so all N models survive with their
    own pass-rate -- the read surface a `local/cluster eval --model` sweep polls."""
    traces = [
        _mtrace("o1", "shaA", "c1", "2026-07-01T00:00:00Z", True, "opus", 0.02),
        _mtrace("o2", "shaA", "c2", "2026-07-01T00:00:01Z", True, "opus", 0.01),
        # sonnet ran the same version+cases slightly later (higher ts).
        _mtrace("s1", "shaA", "c1", "2026-07-01T00:01:00Z", True, "sonnet", 0.006),
        _mtrace("s2", "shaA", "c2", "2026-07-01T00:01:01Z", False, "sonnet", 0.004),
    ]
    matrix = build_matrix(traces, "s", 5)

    # One version column, but BOTH models present in the rollup (not collapsed).
    assert matrix.versions == ["shaA"]
    summaries = {m.model: m for m in matrix.model_summaries}
    assert set(summaries) == {"opus", "sonnet"}
    assert summaries["opus"].passed == 2 and summaries["opus"].total == 2
    assert summaries["sonnet"].passed == 1 and summaries["sonnet"].total == 2
    # The displayed grid still shows one cell per (version, case): newest wins.
    cell_models = {cell.model for row in matrix.rows for cell in row.cells}
    assert cell_models == {"sonnet"}  # sonnet recorded last


def test_cells_carry_model_and_unlabelled_runs_sort_last() -> None:
    traces = [
        _mtrace("m1", "shaA", "c1", "2026-07-01T00:00:00Z", True, "opus"),
        _mtrace("u1", "shaB", "c1", "2026-07-02T00:00:00Z", True, None),
    ]
    matrix = build_matrix(traces, "s", 5)

    cell_models = {
        cell.version: cell.model for row in matrix.rows for cell in row.cells
    }
    assert cell_models["shaA"] == "opus"
    assert cell_models["shaB"] is None
    # Unlabelled (None) model rolls up too and sorts after named models.
    assert matrix.models == ["opus", None]
    # A model with no reported cost anywhere stays None, not a misleading 0.
    assert {m.model: m.cost_usd for m in matrix.model_summaries} == {
        "opus": None,
        None: None,
    }
