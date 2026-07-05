"""Proxy endpoint tests with the external Langfuse HTTP calls mocked.

Only the external Langfuse API is mocked (permitted); the reconstruction and the
FastAPI wiring are exercised for real.
"""

from typing import Any

from agentos_api.deps import get_langfuse
from agentos_api.main import create_app
from fastapi.testclient import TestClient


class FakeLangfuse:
    def __init__(self, observations: list[dict[str, Any]]) -> None:
        self._observations = observations

    async def get_observations(self, trace_id: str) -> list[dict[str, Any]]:
        return self._observations

    async def get_trace(self, trace_id: str) -> dict[str, Any]:
        return {"id": trace_id, "name": "demo"}


def _app_with(observations: list[dict[str, Any]]) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_langfuse] = lambda: FakeLangfuse(observations)
    return TestClient(app)


def test_get_trace_returns_reconstructed_tree(
    auth_headers: dict[str, str],
) -> None:
    observations = [
        {"id": "r", "type": "SPAN", "name": "agent.run", "startTime": "1"},
        {
            "id": "g",
            "type": "GENERATION",
            "name": "gen",
            "startTime": "2",
            "parentObservationId": "r",
        },
    ]
    with _app_with(observations) as client:
        resp = client.get("/langfuse/traces/abc", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["trace"]["id"] == "abc"
    assert body["tree"][0]["name"] == "agent.run"
    assert body["tree"][0]["children"][0]["type"] == "GENERATION"


def test_get_trace_404_when_no_observations(
    auth_headers: dict[str, str],
) -> None:
    with _app_with([]) as client:
        resp = client.get("/langfuse/traces/abc", headers=auth_headers)
    assert resp.status_code == 404


def test_proxy_requires_api_key() -> None:
    with _app_with([]) as client:
        resp = client.get("/langfuse/traces/abc")
    assert resp.status_code == 401
