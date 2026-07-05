"""Langfuse read proxy for the Runs view.

The UI reads traces through this API rather than talking to Langfuse directly
(detailed-architecture.md section 8, decision 4: proxy through our API for a
single auth domain). The observation tree is reconstructed from the flat
observation list via parentObservationId linkage, the PT-4-proven pattern (see
prototypes/observability/read_tree.py).
"""

from typing import Any

import httpx

from .config import Settings
from .schemas import ObservationNode


def build_tree(observations: list[dict[str, Any]]) -> list[ObservationNode]:
    """Reconstruct the nested observation tree from a flat observation list.

    Nodes are linked by parentObservationId; any observation whose parent is
    missing from the set (or absent) is treated as a root. Children are ordered
    by startTime so the tree renders in execution order.
    """

    children: dict[str | None, list[dict[str, Any]]] = {}
    for obs in observations:
        children.setdefault(obs.get("parentObservationId"), []).append(obs)
    known_ids = {obs["id"] for obs in observations}

    def node_for(obs: dict[str, Any]) -> ObservationNode:
        kids = sorted(
            children.get(obs["id"], []), key=lambda o: o.get("startTime") or ""
        )
        return ObservationNode(
            id=obs["id"],
            type=obs.get("type", ""),
            name=obs.get("name"),
            startTime=obs.get("startTime"),
            model=obs.get("model"),
            usageDetails=obs.get("usageDetails"),
            children=[node_for(k) for k in kids],
        )

    roots = [
        obs
        for obs in observations
        if not obs.get("parentObservationId")
        or obs.get("parentObservationId") not in known_ids
    ]
    roots.sort(key=lambda o: o.get("startTime") or "")
    return [node_for(root) for root in roots]


class LangfuseClient:
    """Thin async client over Langfuse's public read API."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._base = settings.langfuse_host.rstrip("/")
        self._auth = (settings.langfuse_public_key, settings.langfuse_secret_key)
        self._client = client

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        resp = await self._client.get(
            f"{self._base}{path}", params=params, auth=self._auth
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def list_traces(self, limit: int) -> list[dict[str, Any]]:
        body = await self._get("/api/public/traces", {"limit": limit})
        traces: list[dict[str, Any]] = body.get("data", [])
        return traces

    async def get_trace(self, trace_id: str) -> dict[str, Any]:
        return await self._get(f"/api/public/traces/{trace_id}", {})

    async def get_observations(self, trace_id: str) -> list[dict[str, Any]]:
        body = await self._get(
            "/api/public/observations", {"traceId": trace_id, "limit": 100}
        )
        observations: list[dict[str, Any]] = body.get("data", [])
        return observations
