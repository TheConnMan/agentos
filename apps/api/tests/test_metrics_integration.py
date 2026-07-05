"""Metrics endpoints against the REAL Langfuse dev instance.

Faithfulness is checked by comparing each endpoint number to the same aggregate
queried directly from Langfuse (the done-when: verified against Langfuse's own
aggregates). Skips when the dev stack is not reachable so the unit suite stays
runnable standalone.
"""

import datetime
import json
import urllib.parse
from typing import Any

import httpx
import pytest
from agentos_api.config import get_settings


def _stack_up() -> bool:
    try:
        httpx.get(
            f"{get_settings().langfuse_host}/api/public/health", timeout=2.0
        ).raise_for_status()
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(not _stack_up(), reason="dev stack not reachable")


def _lf_metric(query: dict[str, Any]) -> list[dict[str, Any]]:
    settings = get_settings()
    url = (
        f"{settings.langfuse_host}/api/public/metrics?query="
        + urllib.parse.quote(json.dumps(query))
    )
    resp = httpx.get(
        url, auth=(settings.langfuse_public_key, settings.langfuse_secret_key), timeout=10
    )
    resp.raise_for_status()
    data: list[dict[str, Any]] = resp.json()["data"]
    return data


def _window() -> tuple[str, str]:
    now = datetime.datetime.now(datetime.UTC)
    return (now - datetime.timedelta(days=90)).isoformat(), now.isoformat()


def test_summary_matches_langfuse_aggregates(
    client: Any, auth_headers: dict[str, str]
) -> None:
    start, end = _window()
    resp = client.get(
        "/observability/metrics/summary",
        params={"start": start, "end": end},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["runs"] > 0  # a real workload exists from the other lanes

    runs = _lf_metric(
        {
            "view": "traces",
            "metrics": [{"measure": "count", "aggregation": "count"}],
            "fromTimestamp": start,
            "toTimestamp": end,
        }
    )
    assert body["runs"] == int(float(runs[0]["count_count"]))

    cost = _lf_metric(
        {
            "view": "observations",
            "metrics": [{"measure": "totalCost", "aggregation": "sum"}],
            "fromTimestamp": start,
            "toTimestamp": end,
        }
    )
    assert abs(body["cost_usd"] - float(cost[0]["sum_totalCost"])) < 1e-9


def test_runs_series_sums_to_the_summary(
    client: Any, auth_headers: dict[str, str]
) -> None:
    start, end = _window()
    summary = client.get(
        "/observability/metrics/summary",
        params={"start": start, "end": end},
        headers=auth_headers,
    ).json()
    series = client.get(
        "/observability/metrics/series",
        params={"metric": "runs", "start": start, "end": end, "granularity": "day"},
        headers=auth_headers,
    )
    assert series.status_code == 200, series.text
    points = series.json()["points"]
    assert points, "expected at least one time bucket"
    assert sum(p["value"] for p in points) == summary["runs"]


def test_environment_filter_passes_through(
    client: Any, auth_headers: dict[str, str]
) -> None:
    start, end = _window()
    resp = client.get(
        "/observability/metrics/summary",
        params={"start": start, "end": end, "environment": "default"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    filtered = _lf_metric(
        {
            "view": "traces",
            "metrics": [{"measure": "count", "aggregation": "count"}],
            "filters": [
                {"column": "environment", "operator": "=", "value": "default", "type": "string"}
            ],
            "fromTimestamp": start,
            "toTimestamp": end,
        }
    )
    assert resp.json()["runs"] == int(float(filtered[0]["count_count"]))


def test_metrics_require_api_key(client: Any) -> None:
    assert client.get("/observability/metrics/summary").status_code == 401


def test_unknown_metric_is_422(
    client: Any, auth_headers: dict[str, str]
) -> None:
    resp = client.get(
        "/observability/metrics/series",
        params={"metric": "bogus"},
        headers=auth_headers,
    )
    assert resp.status_code == 422
