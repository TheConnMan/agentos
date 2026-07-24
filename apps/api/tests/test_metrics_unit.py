"""Unit tests for the Langfuse metrics query builders (no I/O)."""

import uuid

from curie_api.metrics import (
    _cost_known,
    _error_rate,
    _filters,
    _scalar_query,
    agent_trace_filter,
    resolve_window,
)


def test_filters_exclude_eval_traces_from_the_aggregate() -> None:
    # #547: eval traces (`eval:<suite>:<case>`) are billed but not product
    # traffic; the summary must exclude them so runs/tokens/cost aren't inflated.
    for view, name_col in (("traces", "name"), ("observations", "traceName")):
        filters = _filters(view, None, None)
        assert {
            "column": name_col,
            "operator": "does not contain",
            "value": "eval:",
            "type": "string",
        } in filters, view


def test_cost_known_flags_priced_to_zero_as_unknown() -> None:
    # #547: tokens spent but cost summed to exactly 0 => a missing Langfuse price
    # row, not a free run. A genuinely zero-work window stays cost-known.
    assert _cost_known(tokens=2576, cost_usd=0.0) is False
    assert _cost_known(tokens=2576, cost_usd=0.0506) is True
    assert _cost_known(tokens=0, cost_usd=0.0) is True


def test_agent_trace_filter_is_the_agent_id_token() -> None:
    # The runner names traces curie-run:agent-<id>-thread-<ts>, so the per-agent
    # filter must be the `agent-<id>` substring, not the agent's display name.
    agent_id = uuid.UUID("00000000-0000-0000-0000-000000000042")
    token = agent_trace_filter(agent_id)
    assert token == "agent-00000000-0000-0000-0000-000000000042"
    # The token is a substring of a real runner trace name.
    assert token in f"curie-run:{token}-thread-1720200000"


def test_agent_filter_matches_the_runner_trace_name() -> None:
    # A filter built from the id must select against the trace name via `contains`.
    agent_id = uuid.uuid4()
    token = agent_trace_filter(agent_id)
    filters = _filters("observations", None, token)
    assert {
        "column": "traceName",
        "operator": "contains",
        "value": token,
        "type": "string",
    } in filters


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
    q = _scalar_query("latency_p95_ms", "s", "e", None, None)
    assert q["view"] == "traces"
    assert q["metrics"] == [{"measure": "latency", "aggregation": "p95"}]


def test_error_rate_from_level_rows() -> None:
    rows = [
        {"level": "DEFAULT", "count_count": "8"},
        {"level": "ERROR", "count_count": "2"},
    ]
    assert _error_rate(rows) == 0.2
    assert _error_rate([]) == 0.0
