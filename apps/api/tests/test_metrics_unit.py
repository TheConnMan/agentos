"""Unit tests for the Langfuse metrics query builders (no I/O)."""

from agentos_api.metrics import (
    _error_rate,
    _filters,
    _scalar_query,
    resolve_window,
)


def test_resolve_window_uses_explicit_bounds() -> None:
    start, end = resolve_window("2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00", 24)
    assert start == "2026-01-01T00:00:00+00:00"
    assert end == "2026-01-02T00:00:00+00:00"


def test_resolve_window_defaults_to_window_hours() -> None:
    start, end = resolve_window(None, "2026-01-08T00:00:00+00:00", 168)
    assert start == "2026-01-01T00:00:00+00:00"


def test_filters_use_the_right_name_column_per_view() -> None:
    traces = _filters("traces", "prod", "billing")
    assert {"column": "environment", "operator": "=", "value": "prod", "type": "string"} in traces
    assert any(f["column"] == "name" and f["value"] == "billing" for f in traces)

    observations = _filters("observations", None, "billing")
    assert any(f["column"] == "traceName" for f in observations)
    assert all(f["column"] != "environment" for f in observations)


def test_scalar_query_maps_metric_to_view_and_measure() -> None:
    q = _scalar_query("cost_usd", "s", "e", None, None)
    assert q["view"] == "observations"
    assert q["metrics"] == [{"measure": "totalCost", "aggregation": "sum"}]
    assert "timeDimension" not in q

    series_q = _scalar_query("runs", "s", "e", None, None, granularity="day")
    assert series_q["view"] == "traces"
    assert series_q["timeDimension"] == {"granularity": "day"}


def test_latency_is_measured_per_run_on_the_traces_view() -> None:
    # p95 latency must be a per-run aggregate, not span-weighted (observations).
    q = _scalar_query("latency_p95_seconds", "s", "e", None, None)
    assert q["view"] == "traces"
    assert q["metrics"] == [{"measure": "latency", "aggregation": "p95"}]


def test_error_rate_from_level_rows() -> None:
    rows = [
        {"level": "DEFAULT", "count_count": "8"},
        {"level": "ERROR", "count_count": "2"},
    ]
    assert _error_rate(rows) == 0.2
    assert _error_rate([]) == 0.0
