"""Langfuse-backed metrics for the Metrics tab (OB1).

Builds Langfuse Metrics API queries for the five series the design shows (runs,
latency, tokens, cost, error rate) and assembles them into a summary or a time
series, filterable by environment and by agent (a trace-name match; see the note
below). Every number is a faithful proxy of a Langfuse aggregate.

Agent filtering matches the Langfuse trace name (`name` on traces, `traceName`
on observations) with a `contains` operator. The runner names traces
`agentos-run:agent-<agent_id>-thread-<ts>`, so an agent's runs are exactly the
traces whose name contains `agent-<agent_id>`. `agent_trace_filter` builds that
token; callers pass it as the `agent` argument below.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from .langfuse import LangfuseClient
from .schemas import MetricPoint, MetricSeries, MetricsSummary


def agent_trace_filter(agent_id: uuid.UUID | str) -> str:
    """The trace-name substring that selects one agent's runs.

    The runner names every trace `agentos-run:agent-<agent_id>-thread-<ts>`, so
    the agent's traces are those whose name contains `agent-<agent_id>`. This is
    the value passed as the `agent` filter (a `contains` match on trace name);
    matching on `agent.name` never matched a real trace name, which is why
    per-agent cost/traces always read zero.
    """

    return f"agent-{agent_id}"

SCALAR_METRICS = ("runs", "latency_p95_seconds", "tokens", "cost_usd")
ALL_METRICS = (*SCALAR_METRICS, "error_rate")

# metric -> (view, measure, aggregation, result-key, is_integer)
# Latency is queried on the traces view so the p95 is per run, not per span
# (a run with many tool/generation spans would otherwise skew a span-weighted p95).
_SPEC: dict[str, tuple[str, str, str, str, bool]] = {
    "runs": ("traces", "count", "count", "count_count", True),
    "latency_p95_seconds": ("traces", "latency", "p95", "p95_latency", False),
    "tokens": ("observations", "totalTokens", "sum", "sum_totalTokens", True),
    "cost_usd": ("observations", "totalCost", "sum", "sum_totalCost", False),
}


def resolve_window(
    start: str | None, end: str | None, window_hours: int
) -> tuple[str, str]:
    """Resolve the [start, end] ISO window, defaulting to the last window_hours."""

    end_dt = datetime.fromisoformat(end) if end else datetime.now(UTC)
    start_dt = (
        datetime.fromisoformat(start)
        if start
        else end_dt - timedelta(hours=window_hours)
    )
    return start_dt.isoformat(), end_dt.isoformat()


def _filters(view: str, environment: str | None, agent: str | None) -> list[dict[str, Any]]:
    name_col = "name" if view == "traces" else "traceName"
    filters: list[dict[str, Any]] = []
    if environment:
        filters.append(
            {"column": "environment", "operator": "=", "value": environment, "type": "string"}
        )
    if agent:
        filters.append(
            {"column": name_col, "operator": "contains", "value": agent, "type": "string"}
        )
    return filters


def _num(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    return float(value) if value is not None else 0.0


def _scalar_query(
    metric: str,
    start: str,
    end: str,
    environment: str | None,
    agent: str | None,
    granularity: str | None = None,
) -> dict[str, Any]:
    view, measure, aggregation, _key, _is_int = _SPEC[metric]
    query: dict[str, Any] = {
        "view": view,
        "metrics": [{"measure": measure, "aggregation": aggregation}],
        "filters": _filters(view, environment, agent),
        "fromTimestamp": start,
        "toTimestamp": end,
    }
    if granularity:
        query["timeDimension"] = {"granularity": granularity}
    return query


def _level_query(
    start: str,
    end: str,
    environment: str | None,
    agent: str | None,
    granularity: str | None = None,
) -> dict[str, Any]:
    query: dict[str, Any] = {
        "view": "observations",
        "metrics": [{"measure": "count", "aggregation": "count"}],
        "dimensions": [{"field": "level"}],
        "filters": _filters("observations", environment, agent),
        "fromTimestamp": start,
        "toTimestamp": end,
    }
    if granularity:
        query["timeDimension"] = {"granularity": granularity}
    return query


def _error_rate(rows: list[dict[str, Any]]) -> float:
    total = sum(_num(r, "count_count") for r in rows)
    if total == 0:
        return 0.0
    errors = sum(_num(r, "count_count") for r in rows if r.get("level") == "ERROR")
    return errors / total


async def summary(
    lf: LangfuseClient,
    start: str,
    end: str,
    environment: str | None,
    agent: str | None,
) -> MetricsSummary:
    scalars: dict[str, float] = {}
    for metric in SCALAR_METRICS:
        rows = await lf.query_metrics(
            _scalar_query(metric, start, end, environment, agent)
        )
        key = _SPEC[metric][3]
        scalars[metric] = _num(rows[0], key) if rows else 0.0

    level_rows = await lf.query_metrics(_level_query(start, end, environment, agent))

    return MetricsSummary(
        start=start,
        end=end,
        runs=int(scalars["runs"]),
        latency_p95_seconds=scalars["latency_p95_seconds"],
        tokens=int(scalars["tokens"]),
        cost_usd=scalars["cost_usd"],
        error_rate=_error_rate(level_rows),
    )


async def series(
    lf: LangfuseClient,
    metric: str,
    start: str,
    end: str,
    granularity: str,
    environment: str | None,
    agent: str | None,
) -> MetricSeries:
    if metric == "error_rate":
        points = await _error_rate_series(lf, start, end, granularity, environment, agent)
    else:
        rows = await lf.query_metrics(
            _scalar_query(metric, start, end, environment, agent, granularity)
        )
        key = _SPEC[metric][3]
        points = [
            MetricPoint(ts=str(r.get("time_dimension")), value=_num(r, key))
            for r in rows
            if r.get("time_dimension")
        ]
    return MetricSeries(
        metric=metric, granularity=granularity, start=start, end=end, points=points
    )


async def _error_rate_series(
    lf: LangfuseClient,
    start: str,
    end: str,
    granularity: str,
    environment: str | None,
    agent: str | None,
) -> list[MetricPoint]:
    rows = await lf.query_metrics(
        _level_query(start, end, environment, agent, granularity)
    )
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        ts = row.get("time_dimension")
        if ts:
            buckets.setdefault(str(ts), []).append(row)
    return [
        MetricPoint(ts=ts, value=_error_rate(bucket_rows))
        for ts, bucket_rows in sorted(buckets.items())
    ]
