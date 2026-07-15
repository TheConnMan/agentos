"""Durable approvals (#244): real Postgres + real Valkey round-trip.

Exercises the record lifecycle against the disposable per-run database and the
compose Valkey: idempotent creation on dedupe_key, the resolve-once
compare-and-set (losers get 409 naming the winner, including under genuine
concurrency), SLA expiry (410), and the resume turn enqueued onto a
test-isolated runs stream as a valid frozen-contract QueuedTurn.
"""

import asyncio
import json
import os
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import pytest
import redis
from aci_protocol import QueuedTurn
from agentos_api.config import get_settings
from agentos_api.main import create_app
from agentos_api.models import Approval
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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


def _read_resumed_at(approval_id: str) -> datetime | None:
    """Read ``Approval.resumed_at`` straight from the DB (it is not exposed on
    ``ApprovalOut``). A fresh engine keeps this safe under ``asyncio.run`` from
    the sync test body -- the app's engine is bound to the TestClient's portal
    loop and cannot be reused across event loops."""

    async def _run() -> datetime | None:
        engine = create_async_engine(get_settings().database_url)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessionmaker() as session:
                approval = await session.get(Approval, uuid.UUID(approval_id))
                return None if approval is None else approval.resumed_at
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def test_resolve_endpoint_stays_200_when_enqueue_fails(
    approvals_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
    valkey: redis.Redis,
    runs_stream: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#411: a Valkey blip on the resume enqueue must not dead-end the resolve.

    The resolution itself committed (CAS won, audit written), so the endpoint
    returns 200 with the resolved record, leaves ``resumed_at`` NULL for the
    reconciler to retry, and enqueues nothing. A retry still 409s (CAS intact) --
    the owed wake belongs to the reconciler, never to a client retry (which used
    to 500 then 409-forever).
    """

    created = approvals_client.post(
        "/approvals", json=_payload(), headers=auth_headers
    ).json()

    async def _boom(_turn: Any) -> str:
        raise RuntimeError("valkey unreachable")

    monkeypatch.setattr(approvals_client.app.state.resume_queue, "enqueue", _boom)

    resolved = approvals_client.post(
        f"/approvals/{created['id']}/resolve",
        json={"decision": "approved", "resolved_by": "U9", "actor_channel": "C1"},
        headers=auth_headers,
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "approved"

    # No resume turn on the stream, and the record still owes its wake.
    assert valkey.xrange(runs_stream) == []
    assert _read_resumed_at(created["id"]) is None

    # The CAS is untouched: a retry loses as a normal race, it does not re-enqueue.
    retry = approvals_client.post(
        f"/approvals/{created['id']}/resolve",
        json={"decision": "approved", "resolved_by": "U2", "actor_channel": "C1"},
        headers=auth_headers,
    )
    assert retry.status_code == 409
    assert valkey.xrange(runs_stream) == []


@pytest.fixture
def reconciler_disabled_client(
    _disposable_db: Any, runs_stream: str
) -> Iterator[TestClient]:
    """A TestClient built with the resume reconciler DISABLED, so a failed inline
    enqueue has no backstop. ``raise_server_exceptions=False`` lets the resulting
    500 be asserted as a response rather than re-raised into the test body."""

    os.environ["RESUME_RECONCILER_ENABLED"] = "false"
    get_settings.cache_clear()
    try:
        with TestClient(create_app(), raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        os.environ.pop("RESUME_RECONCILER_ENABLED", None)
        get_settings.cache_clear()


def test_resolve_reraises_when_reconciler_disabled(
    reconciler_disabled_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
    valkey: redis.Redis,
    runs_stream: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#411 hardening: returning 200 on a failed resume enqueue is only safe when a
    reconciler will recover the owed wake. With ``resume_reconciler_enabled=false``
    there is no backstop, so the endpoint must surface the enqueue failure (500)
    rather than silently stranding the suspended session behind a 200.

    The resolution CAS still committed (approved, audit written), leaving the record
    resolved-but-unresumed with nothing on the stream -- exactly the state a
    disabled deployment must not hide. (The default-enabled 200 path is pinned by
    ``test_resolve_endpoint_stays_200_when_enqueue_fails``.)
    """

    created = reconciler_disabled_client.post(
        "/approvals", json=_payload(), headers=auth_headers
    ).json()

    async def _boom(_turn: Any) -> str:
        raise RuntimeError("valkey unreachable")

    monkeypatch.setattr(
        reconciler_disabled_client.app.state.resume_queue, "enqueue", _boom
    )

    resolved = reconciler_disabled_client.post(
        f"/approvals/{created['id']}/resolve",
        json={"decision": "approved", "resolved_by": "U9", "actor_channel": "C1"},
        headers=auth_headers,
    )
    assert resolved.status_code == 500, resolved.text

    # The CAS committed -- the record is resolved -- but its wake is unrecoverably
    # owed (no reconciler) and nothing reached the stream.
    record = reconciler_disabled_client.get(
        f"/approvals/{created['id']}", headers=auth_headers
    )
    assert record.json()["status"] == "approved"
    assert _read_resumed_at(created["id"]) is None
    assert valkey.xrange(runs_stream) == []


def test_happy_path_resolve_marks_resumed(
    approvals_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
    valkey: redis.Redis,
    runs_stream: str,
) -> None:
    """#411: the normal successful resolve now records the wake as delivered by
    setting ``resumed_at``, so the reconciler's work-list excludes it. This adds
    to the resolve-once coverage without weakening the existing assertions."""

    created = approvals_client.post(
        "/approvals", json=_payload(), headers=auth_headers
    ).json()

    resolved = approvals_client.post(
        f"/approvals/{created['id']}/resolve",
        json={"decision": "approved", "resolved_by": "U9", "actor_channel": "C1"},
        headers=auth_headers,
    )
    assert resolved.status_code == 200, resolved.text

    # The resume turn is on the stream (the existing #244 contract) ...
    entries = valkey.xrange(runs_stream)
    assert len(entries) == 1
    turn = QueuedTurn.model_validate(json.loads(entries[0][1]["payload"]))
    assert turn.event_id == f"approval-{created['id']}-resolved"

    # ... and the record is now marked resumed (the new #411 contract).
    assert _read_resumed_at(created["id"]) is not None


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
        json={
            "decision": "approved",
            "resolved_by": "U9",
            "note": "ship it",
            "actor_channel": "C1",
        },
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
        json={"decision": "rejected", "resolved_by": "U2", "actor_channel": "C1"},
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
            json={"decision": "approved", "resolved_by": actor, "actor_channel": "C1"},
            headers=auth_headers,
        )
        return response.status_code

    # Actors distinct from the record's author (U1), so none is blocked as
    # self-approval and the race is purely over the pending claim.
    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(pool.map(attempt, [f"U_race_{i}" for i in range(4)]))

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
        json={"decision": "approved", "resolved_by": "U9", "actor_channel": "C1"},
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
        json={"decision": "approved", "resolved_by": "U9", "actor_channel": "C1"},
        headers=auth_headers,
    )
    assert missing.status_code == 404


def test_requires_api_key(
    approvals_client: TestClient, clean_db: None
) -> None:
    denied = approvals_client.post("/approvals", json=_payload())
    assert denied.status_code in (401, 403)


def test_authorizer_blocks_non_member_and_self_approval(
    approvals_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
    valkey: redis.Redis,
    runs_stream: str,
) -> None:
    """The server-side authorizer (#246): channel membership is proven by the
    attempt's channel, self-approval is blocked unconditionally, and a denied
    attempt neither resolves the record nor enqueues a resume turn."""

    created = approvals_client.post(
        "/approvals", json=_payload(), headers=auth_headers
    ).json()
    resolve_url = f"/approvals/{created['id']}/resolve"

    # Wrong channel: not an approver.
    wrong_channel = approvals_client.post(
        resolve_url,
        json={"decision": "approved", "resolved_by": "U9", "actor_channel": "C_OTHER"},
        headers=auth_headers,
    )
    assert wrong_channel.status_code == 403
    assert "not an approver" in wrong_channel.json()["detail"]

    # No channel evidence at all: not an approver.
    no_channel = approvals_client.post(
        resolve_url,
        json={"decision": "approved", "resolved_by": "U9"},
        headers=auth_headers,
    )
    assert no_channel.status_code == 403

    # Self-approval: blocked even from the right channel (the record's author
    # is U1, see _payload).
    self_approval = approvals_client.post(
        resolve_url,
        json={"decision": "approved", "resolved_by": "U1", "actor_channel": "C1"},
        headers=auth_headers,
    )
    assert self_approval.status_code == 403
    assert "self-approval" in self_approval.json()["detail"]

    # The record is still pending and nothing was enqueued.
    record = approvals_client.get(f"/approvals/{created['id']}", headers=auth_headers)
    assert record.json()["status"] == "pending"
    assert valkey.xrange(runs_stream) == []

    # An authorized member still resolves it afterwards.
    ok = approvals_client.post(
        resolve_url,
        json={"decision": "approved", "resolved_by": "U9", "actor_channel": "C1"},
        headers=auth_headers,
    )
    assert ok.status_code == 200
    assert len(valkey.xrange(runs_stream)) == 1


def test_route_bound_approval_authorizes_against_card_channel(
    approvals_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
    valkey: redis.Redis,
    runs_stream: str,
) -> None:
    """#247: when a route binding placed the card in another channel, THAT
    channel's members are the approvers; the requesting channel no longer is."""

    created = approvals_client.post(
        "/approvals",
        json=_payload(route="managers", card_channel="C_MGRS"),
        headers=auth_headers,
    ).json()
    assert created["route"] == "managers"
    assert created["card_channel"] == "C_MGRS"
    resolve_url = f"/approvals/{created['id']}/resolve"

    # The requesting channel (C1) is NOT the approvers' channel anymore.
    from_requesting = approvals_client.post(
        resolve_url,
        json={"decision": "approved", "resolved_by": "U9", "actor_channel": "C1"},
        headers=auth_headers,
    )
    assert from_requesting.status_code == 403

    # A member of the route-bound channel resolves it.
    ok = approvals_client.post(
        resolve_url,
        json={"decision": "approved", "resolved_by": "U9", "actor_channel": "C_MGRS"},
        headers=auth_headers,
    )
    assert ok.status_code == 200
    assert len(valkey.xrange(runs_stream)) == 1


def test_audit_log_records_attempts_with_authorizer_snapshots(
    approvals_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
    valkey: redis.Redis,
    runs_stream: str,
) -> None:
    """The #247 acceptance read-back: denied and resolved attempts each leave
    an append-only audit entry naming the actor, the channel evidence, and the
    authorizer snapshot that counted (or refused) them."""

    created = approvals_client.post(
        "/approvals", json=_payload(), headers=auth_headers
    ).json()
    resolve_url = f"/approvals/{created['id']}/resolve"

    denied = approvals_client.post(
        resolve_url,
        json={"decision": "approved", "resolved_by": "U_OUT", "actor_channel": "C_X"},
        headers=auth_headers,
    )
    assert denied.status_code == 403
    resolved = approvals_client.post(
        resolve_url,
        json={"decision": "approved", "resolved_by": "U9", "actor_channel": "C1"},
        headers=auth_headers,
    )
    assert resolved.status_code == 200
    late = approvals_client.post(
        resolve_url,
        json={"decision": "rejected", "resolved_by": "U_LATE", "actor_channel": "C1"},
        headers=auth_headers,
    )
    assert late.status_code == 409

    audit = approvals_client.get(
        f"/approvals/{created['id']}/audit", headers=auth_headers
    )
    assert audit.status_code == 200
    entries = audit.json()
    assert [e["action"] for e in entries] == ["denied", "resolved", "race_lost"]

    denied_entry, resolved_entry, race_entry = entries
    assert denied_entry["actor"] == "U_OUT"
    assert denied_entry["actor_channel"] == "C_X"
    assert denied_entry["authorized"] is False
    assert denied_entry["authorizer"] == "ChannelMembershipAuthorizer"
    assert "not an approver" in denied_entry["reason"]

    assert resolved_entry["actor"] == "U9"
    assert resolved_entry["authorized"] is True
    assert resolved_entry["decision"] == "approved"
    assert resolved_entry["authorizer"] == "ChannelMembershipAuthorizer"

    assert race_entry["actor"] == "U_LATE"
    assert "already resolved by U9" in race_entry["reason"]

    # The audit endpoint 404s for an unknown approval.
    missing = approvals_client.get(
        f"/approvals/{uuid.uuid4()}/audit", headers=auth_headers
    )
    assert missing.status_code == 404
