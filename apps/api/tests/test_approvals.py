"""Durable approvals (#244): real Postgres + real Valkey round-trip.

Exercises the record lifecycle against the disposable per-run database and the
compose Valkey: idempotent creation on dedupe_key, the resolve-once
compare-and-set (losers get 409 naming the winner, including under genuine
concurrency), SLA expiry (410), and the resume turn enqueued onto a
test-isolated runs stream as a valid frozen-contract QueuedTurn.
"""

import json
import os
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
import redis
from aci_protocol import QueuedTurn
from agentos_api.config import get_settings
from agentos_api.main import create_app
from fastapi.testclient import TestClient

_VALKEY_HOST = os.environ.get("TEST_VALKEY_HOST", "localhost")
_VALKEY_PORT = int(os.environ.get("TEST_VALKEY_PORT", "26379"))
_VALKEY_PW = os.environ.get("TEST_VALKEY_PW", "valkeypass")


@pytest.fixture
def runs_stream() -> Iterator[str]:
    """A per-test runs stream, so resolutions never feed the shared compose
    worker's real ``agentos:runs`` consumer group."""

    name = f"test:agentos:runs:{uuid.uuid4().hex}"
    os.environ["RUNS_STREAM"] = name
    get_settings.cache_clear()
    yield name
    os.environ.pop("RUNS_STREAM", None)
    get_settings.cache_clear()


@pytest.fixture
def approvals_client(_disposable_db: Any, runs_stream: str) -> Iterator[TestClient]:
    """A TestClient whose app was built after the stream override, so its
    ResumeQueue targets the isolated stream."""

    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def valkey(runs_stream: str) -> Iterator[redis.Redis]:
    client = redis.Redis(
        host=_VALKEY_HOST,
        port=_VALKEY_PORT,
        password=_VALKEY_PW or None,
        decode_responses=True,
    )
    try:
        client.ping()
    except redis.exceptions.RedisError as exc:
        pytest.skip(f"Valkey not reachable: {exc}")
    yield client
    client.delete(runs_stream)
    client.close()


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "conversation_id": f"th-{uuid.uuid4().hex[:8]}",
        "author": "U1",
        "summary": "Give ACME a 20% discount",
        "reply_channel": "C1",
        "reply_placeholder": "p-1",
        "dedupe_key": uuid.uuid4().hex,
    }
    base.update(overrides)
    return base


def test_create_get_list_round_trip(
    approvals_client: TestClient, auth_headers: dict[str, str], clean_db: None
) -> None:
    payload = _payload()
    created = approvals_client.post("/approvals", json=payload, headers=auth_headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "pending"
    assert body["summary"] == payload["summary"]
    assert body["resolved_by"] is None

    got = approvals_client.get(f"/approvals/{body['id']}", headers=auth_headers)
    assert got.status_code == 200
    assert got.json() == body

    listed = approvals_client.get(
        "/approvals",
        params={"status_filter": "pending", "conversation_id": payload["conversation_id"]},
        headers=auth_headers,
    )
    assert [a["id"] for a in listed.json()] == [body["id"]]


def test_create_is_idempotent_on_dedupe_key(
    approvals_client: TestClient, auth_headers: dict[str, str], clean_db: None
) -> None:
    payload = _payload()
    first = approvals_client.post("/approvals", json=payload, headers=auth_headers)
    assert first.status_code == 201
    replay = approvals_client.post("/approvals", json=payload, headers=auth_headers)
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]


def test_resolve_once_and_enqueue_resume_turn(
    approvals_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
    valkey: redis.Redis,
    runs_stream: str,
) -> None:
    payload = _payload(reply_endpoint="http://localhost:9999/api/")
    created = approvals_client.post("/approvals", json=payload, headers=auth_headers).json()

    resolved = approvals_client.post(
        f"/approvals/{created['id']}/resolve",
        json={"decision": "approved", "resolved_by": "U9", "note": "ship it"},
        headers=auth_headers,
    )
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["status"] == "approved"
    assert body["resolved_by"] == "U9"
    assert body["resolved_at"] is not None

    # The resume turn landed on the isolated runs stream as a valid frozen
    # QueuedTurn, addressed back to the requesting thread and placeholder.
    entries = valkey.xrange(runs_stream)
    assert len(entries) == 1
    turn = QueuedTurn.model_validate(json.loads(entries[0][1]["payload"]))
    assert turn.event_id == f"approval-{created['id']}-resolved"
    assert turn.conversation_id == payload["conversation_id"]
    assert turn.author == "U9"
    assert turn.reply_handle.channel == "C1"
    assert turn.reply_handle.placeholder == "p-1"
    assert turn.reply_handle.endpoint == "http://localhost:9999/api/"
    assert "approved by U9" in turn.text
    assert "ship it" in turn.text

    # The loser of the claim race is told who resolved it.
    second = approvals_client.post(
        f"/approvals/{created['id']}/resolve",
        json={"decision": "rejected", "resolved_by": "U2"},
        headers=auth_headers,
    )
    assert second.status_code == 409
    assert "already resolved by U9" in second.json()["detail"]
    # And no second resume turn was enqueued.
    assert len(valkey.xrange(runs_stream)) == 1


def test_concurrent_resolvers_yield_exactly_one_winner(
    approvals_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
    valkey: redis.Redis,
    runs_stream: str,
) -> None:
    created = approvals_client.post(
        "/approvals", json=_payload(), headers=auth_headers
    ).json()

    def attempt(actor: str) -> int:
        response = approvals_client.post(
            f"/approvals/{created['id']}/resolve",
            json={"decision": "approved", "resolved_by": actor},
            headers=auth_headers,
        )
        return response.status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(attempt, [f"U{i}" for i in range(4)]))

    assert sorted(codes) == [200, 409, 409, 409]
    assert len(valkey.xrange(runs_stream)) == 1


def test_expired_approval_cannot_be_resolved(
    approvals_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
    valkey: redis.Redis,
    runs_stream: str,
) -> None:
    created = approvals_client.post(
        "/approvals", json=_payload(expires_in_seconds=1), headers=auth_headers
    ).json()
    assert created["expires_at"] is not None
    time.sleep(1.1)

    resolved = approvals_client.post(
        f"/approvals/{created['id']}/resolve",
        json={"decision": "approved", "resolved_by": "U9"},
        headers=auth_headers,
    )
    assert resolved.status_code == 410
    got = approvals_client.get(f"/approvals/{created['id']}", headers=auth_headers)
    assert got.json()["status"] == "expired"
    # No resume turn for an expired record.
    assert valkey.xrange(runs_stream) == []


def test_unknown_approval_is_404(
    approvals_client: TestClient, auth_headers: dict[str, str], clean_db: None
) -> None:
    missing = approvals_client.post(
        f"/approvals/{uuid.uuid4()}/resolve",
        json={"decision": "approved", "resolved_by": "U9"},
        headers=auth_headers,
    )
    assert missing.status_code == 404


def test_requires_api_key(
    approvals_client: TestClient, clean_db: None
) -> None:
    denied = approvals_client.post("/approvals", json=_payload())
    assert denied.status_code in (401, 403)
