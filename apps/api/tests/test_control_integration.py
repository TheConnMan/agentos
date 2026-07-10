"""Kill switch + budgets against real Valkey and real Postgres.

Nothing is mocked: the kill test subscribes to the real Valkey channel and
asserts the SET flag and PUBLISH event actually land; the budget test round-trips
through Postgres.
"""

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import redis
from agentos_api.config import get_settings
from agentos_api.killswitch import KILL_CHANNEL, kill_key


@pytest.fixture
def valkey() -> Iterator[redis.Redis]:
    client: redis.Redis = redis.from_url(get_settings().valkey_dsn())
    try:
        yield client
    finally:
        client.close()


def _make_agent(client: Any, headers: dict[str, str]) -> str:
    agent = client.post(
        "/agents",
        json={"name": "kill-agent", "slack_channel": "C000000K01"},
        headers=headers,
    ).json()
    return str(agent["id"])


def test_kill_sets_flag_and_publishes_event(
    client: Any,
    auth_headers: dict[str, str],
    clean_db: None,
    valkey: redis.Redis,
) -> None:
    agent_id = _make_agent(client, auth_headers)
    key = kill_key(uuid.UUID(agent_id))
    valkey.delete(key)

    pubsub = valkey.pubsub()
    pubsub.subscribe(KILL_CHANNEL)
    pubsub.get_message(timeout=2)  # the subscribe confirmation

    try:
        resp = client.post(f"/agents/{agent_id}/kill", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == {"killed": True}

        # The flag is set with no TTL (persists until resume).
        assert valkey.get(key) == b"1"
        assert valkey.ttl(key) == -1

        # A kill event was published on the channel for this agent.
        _await_event(pubsub, agent_id, "kill")

        # GET reflects the killed state; kill is idempotent.
        assert client.get(f"/agents/{agent_id}/kill", headers=auth_headers).json() == {
            "killed": True
        }
        client.post(f"/agents/{agent_id}/kill", headers=auth_headers)
        assert valkey.get(key) == b"1"

        # Resume clears the flag and publishes a resume event.
        resumed = client.post(f"/agents/{agent_id}/resume", headers=auth_headers)
        assert resumed.json() == {"killed": False}
        assert valkey.exists(key) == 0
        _await_event(pubsub, agent_id, "resume")
        assert client.get(f"/agents/{agent_id}/kill", headers=auth_headers).json() == {
            "killed": False
        }
    finally:
        pubsub.close()
        valkey.delete(key)


def test_budget_round_trips_through_postgres(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    agent_id = _make_agent(client, auth_headers)

    # Defaults are null until set.
    assert client.get(f"/agents/{agent_id}/budget", headers=auth_headers).json() == {
        "max_usd_per_day": None,
        "max_output_tokens_per_run": None,
    }

    put = client.put(
        f"/agents/{agent_id}/budget",
        json={"max_usd_per_day": 5.0, "max_output_tokens_per_run": 1000},
        headers=auth_headers,
    )
    assert put.status_code == 200
    assert put.json() == {"max_usd_per_day": 5.0, "max_output_tokens_per_run": 1000}

    # Persisted: a fresh GET reads it back from the database.
    assert client.get(f"/agents/{agent_id}/budget", headers=auth_headers).json() == {
        "max_usd_per_day": 5.0,
        "max_output_tokens_per_run": 1000,
    }


def test_budget_rejects_non_positive(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    agent_id = _make_agent(client, auth_headers)
    resp = client.put(
        f"/agents/{agent_id}/budget",
        json={"max_usd_per_day": -1},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_cost_composes_metrics_for_the_agent(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    agent_id = _make_agent(client, auth_headers)
    resp = client.get(
        f"/agents/{agent_id}/cost",
        params={"start": "2026-04-06T00:00:00+00:00", "end": "2026-07-05T23:59:00+00:00"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "total_usd" in body
    assert isinstance(body["points"], list)
    assert body["total_usd"] == pytest.approx(sum(p["value"] for p in body["points"]))


def test_cost_filters_langfuse_by_the_agent_trace_token(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    # The per-agent cost query must filter Langfuse by the `agent-<id>` trace-name
    # token (a `contains` match), not by the agent's display name -- matching on
    # the name never matched a real runner trace, which read $0 for every agent.
    from agentos_api.deps import get_langfuse

    agent_id = _make_agent(client, auth_headers)

    captured: list[dict[str, Any]] = []

    class CapturingLangfuse:
        async def query_metrics(self, query: dict[str, Any]) -> list[dict[str, Any]]:
            captured.append(query)
            return []

    client.app.dependency_overrides[get_langfuse] = lambda: CapturingLangfuse()
    try:
        resp = client.get(f"/agents/{agent_id}/cost", headers=auth_headers)
        assert resp.status_code == 200
    finally:
        client.app.dependency_overrides.pop(get_langfuse, None)

    assert captured, "cost endpoint issued no Langfuse metrics query"
    filters = captured[0]["filters"]
    assert any(
        f["operator"] == "contains" and f["value"] == f"agent-{agent_id}"
        for f in filters
    ), f"agent trace-token filter missing from {filters}"


def test_control_endpoints_require_api_key(client: Any) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/agents/{missing}/kill").status_code == 401


def test_missing_agent_is_404(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    missing = "00000000-0000-0000-0000-000000000000"
    assert (
        client.post(f"/agents/{missing}/kill", headers=auth_headers).status_code == 404
    )


def _await_event(pubsub: Any, agent_id: str, action: str) -> dict[str, Any]:
    """Read the channel until the event for this agent + action arrives.

    Tolerates intervening events (idempotent re-kills, other tests sharing the
    global channel) by matching on agent_id and action.
    """

    for _ in range(20):
        message = pubsub.get_message(ignore_subscribe_messages=True, timeout=2)
        if message and message.get("type") == "message":
            data: dict[str, Any] = json.loads(message["data"])
            if data.get("agent_id") == agent_id and data.get("action") == action:
                return data
    raise AssertionError(f"no {action} event for {agent_id} on the kill channel")
