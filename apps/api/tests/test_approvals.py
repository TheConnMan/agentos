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
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import redis
import redis.asyncio as aioredis
from aci_protocol import QueuedTurn
from agentos_api import crud
from agentos_api.config import get_settings
from agentos_api.main import create_app
from agentos_api.models import Approval
from agentos_api.resumequeue import ResumeQueue
from agentos_api.sandbox_token import mint
from agentos_api.sweeper import run_expiry_sweeper, sweep_expired_approvals
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


def test_expired_approval_resolve_returns_410_and_resumes(
    approvals_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
    valkey: redis.Redis,
    runs_stream: str,
) -> None:
    payload = _payload(expires_in_seconds=1)
    created = approvals_client.post(
        "/approvals", json=payload, headers=auth_headers
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

    # #412: a resolver landing after the SLA lapsed (but before the next
    # sweep) must not strand the session. The late resolve still gets 410
    # and the record still flips to expired, but it now enqueues the same
    # deterministic expiry resume turn the sweeper would have produced, so
    # the conversation resumes down its timeout branch instead of hanging.
    entries = valkey.xrange(runs_stream)
    assert len(entries) == 1
    turn = QueuedTurn.model_validate(json.loads(entries[0][1]["payload"]))
    assert turn.event_id == f"approval-{created['id']}-resolved"
    assert turn.conversation_id == payload["conversation_id"]
    assert turn.author == "system"
    assert turn.reply_handle.channel == "C1"
    assert turn.reply_handle.placeholder == "p-1"
    assert "expired" in turn.text
    assert payload["summary"] in turn.text


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


def test_scoped_state_token_cannot_resolve_an_approval(
    approvals_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
) -> None:
    """The core #410 security assertion: a scoped sandbox "state" token is a
    least-privilege credential for the agent's own state namespace ONLY. It must
    NOT authorize resolving an approval -- otherwise a sandboxed agent could
    forward its own state token to /approvals/{id}/resolve and self-approve its
    own gated tool call, defeating the server-side authorizer (ADR-0010)."""

    created = approvals_client.post(
        "/approvals", json=_payload(), headers=auth_headers
    ).json()
    resolve_url = f"/approvals/{created['id']}/resolve"

    scoped = mint(
        get_settings().api_key,
        agent=str(uuid.uuid4()),
        scope="state",
        exp=4102444800,  # 2100-01-01, valid at test time
    )
    denied = approvals_client.post(
        resolve_url,
        json={"decision": "approved", "resolved_by": "U9", "actor_channel": "C1"},
        headers={"X-API-Key": scoped},
    )
    assert denied.status_code == 401, denied.text
    # The record is untouched by the rejected attempt.
    record = approvals_client.get(
        f"/approvals/{created['id']}", headers=auth_headers
    )
    assert record.json()["status"] == "pending"

    # The platform key still resolves it (no regression).
    ok = approvals_client.post(
        resolve_url,
        json={"decision": "approved", "resolved_by": "U9", "actor_channel": "C1"},
        headers=auth_headers,
    )
    assert ok.status_code == 200, ok.text


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


# --- expiry sweeper (#412) ----------------------------------------------------
#
# These tests exercise the periodic sweeper that flips lapsed pending approvals
# to `expired` and enqueues a platform-authored resume turn, so the suspended
# session resumes down its timeout branch instead of stranding. Real Postgres +
# real Valkey, no mocks. Because `sweep_expired_approvals` is async and asyncpg
# connections are loop-bound, each test creates records via the SYNC HTTP client
# (they land in the disposable DB regardless of loop), then runs ONE
# `asyncio.run` scenario that builds its OWN async engine + sessionmaker + redis
# client + ResumeQueue against the same DB/stream, and asserts DB state via the
# sync HTTP client and stream contents via the sync `valkey` fixture. Real sleeps
# are avoided by creating records with `expires_in_seconds=1` and sweeping with
# an explicit `now` two seconds in the future (naive UTC, which also pins the
# timezone edge case).


def _naive_utc(seconds_ahead: float = 0.0) -> datetime:
    """Naive-UTC clock matching the sweeper's `now`/`expires_at` comparison."""

    return datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=seconds_ahead)


@asynccontextmanager
async def _sweeper_stack(
    stream: str,
) -> AsyncIterator[tuple[async_sessionmaker[Any], ResumeQueue, aioredis.Redis]]:
    """A self-owned async engine + sessionmaker + resume queue on the test's DB
    and isolated runs stream, disposed on exit. Built inside the running loop so
    the asyncpg/redis pools bind to it, never to the TestClient's lifespan loop."""

    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async_client = aioredis.from_url(settings.valkey_dsn())
    queue = ResumeQueue(async_client, stream=stream)
    try:
        yield sessionmaker, queue, async_client
    finally:
        await async_client.aclose()
        await engine.dispose()


def test_sweeper_expires_lapsed_pending_and_enqueues_resume_turn(
    approvals_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
    valkey: redis.Redis,
    runs_stream: str,
) -> None:
    payload = _payload(
        expires_in_seconds=1, reply_endpoint="http://localhost:9999/api/"
    )
    created = approvals_client.post(
        "/approvals", json=payload, headers=auth_headers
    ).json()
    assert created["expires_at"] is not None

    now = _naive_utc(2)

    async def _scenario() -> int:
        async with _sweeper_stack(runs_stream) as (sessionmaker, queue, _client):
            async with sessionmaker() as session:
                return await sweep_expired_approvals(session, queue, now=now)

    flipped = asyncio.run(_scenario())
    assert flipped == 1

    # DB state: flipped to expired by the platform, no human resolver recorded.
    got = approvals_client.get(
        f"/approvals/{created['id']}", headers=auth_headers
    ).json()
    assert got["status"] == "expired"
    assert got["resolved_by"] is None
    assert got["resolved_at"] is not None

    # Exactly one resume turn, a valid frozen QueuedTurn addressed back to the
    # requesting thread's placeholder and conveying the expiry.
    entries = valkey.xrange(runs_stream)
    assert len(entries) == 1
    turn = QueuedTurn.model_validate(json.loads(entries[0][1]["payload"]))
    assert turn.event_id == f"approval-{created['id']}-resolved"
    assert turn.conversation_id == payload["conversation_id"]
    assert turn.author == "system"
    assert turn.reply_handle.channel == "C1"
    assert turn.reply_handle.placeholder == "p-1"
    assert turn.reply_handle.endpoint == "http://localhost:9999/api/"
    assert "expired" in turn.text
    assert payload["summary"] in turn.text

    # The autonomous flip is audited consistently with #247.
    audit = approvals_client.get(
        f"/approvals/{created['id']}/audit", headers=auth_headers
    )
    assert audit.status_code == 200
    entries_audit = audit.json()
    assert len(entries_audit) == 1
    row = entries_audit[0]
    assert row["action"] == "expired"
    assert row["actor"] == "system"
    assert row["authorizer"] == "ExpirySweeper"
    assert row["authorized"] is True
    assert row["decision"] == ""


def test_sweeper_ignores_unexpired_and_unbounded_records(
    approvals_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
    valkey: redis.Redis,
    runs_stream: str,
) -> None:
    future = approvals_client.post(
        "/approvals", json=_payload(expires_in_seconds=3600), headers=auth_headers
    ).json()
    unbounded = approvals_client.post(
        "/approvals", json=_payload(), headers=auth_headers
    ).json()
    assert unbounded["expires_at"] is None

    now = _naive_utc()

    async def _scenario() -> int:
        async with _sweeper_stack(runs_stream) as (sessionmaker, queue, _client):
            async with sessionmaker() as session:
                return await sweep_expired_approvals(session, queue, now=now)

    flipped = asyncio.run(_scenario())
    assert flipped == 0

    for record in (future, unbounded):
        got = approvals_client.get(
            f"/approvals/{record['id']}", headers=auth_headers
        ).json()
        assert got["status"] == "pending"
    assert valkey.xrange(runs_stream) == []


def test_sweeper_second_pass_is_a_no_op(
    approvals_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
    valkey: redis.Redis,
    runs_stream: str,
) -> None:
    created = approvals_client.post(
        "/approvals", json=_payload(expires_in_seconds=1), headers=auth_headers
    ).json()

    now = _naive_utc(2)

    async def _scenario() -> tuple[int, int]:
        async with _sweeper_stack(runs_stream) as (sessionmaker, queue, _client):
            async with sessionmaker() as session:
                first = await sweep_expired_approvals(session, queue, now=now)
            async with sessionmaker() as session:
                second = await sweep_expired_approvals(session, queue, now=now)
            return first, second

    first, second = asyncio.run(_scenario())
    assert first == 1
    assert second == 0

    # No double-flip, no second turn, no second audit row.
    assert len(valkey.xrange(runs_stream)) == 1
    audit = approvals_client.get(
        f"/approvals/{created['id']}/audit", headers=auth_headers
    ).json()
    assert [e["action"] for e in audit] == ["expired"]


def test_sweeper_skips_already_resolved_records(
    approvals_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
    valkey: redis.Redis,
    runs_stream: str,
) -> None:
    created = approvals_client.post(
        "/approvals", json=_payload(expires_in_seconds=1), headers=auth_headers
    ).json()

    # Resolve it via the normal path BEFORE the SLA lapses (approver from C1).
    resolved = approvals_client.post(
        f"/approvals/{created['id']}/resolve",
        json={"decision": "approved", "resolved_by": "U9", "actor_channel": "C1"},
        headers=auth_headers,
    )
    assert resolved.status_code == 200
    assert len(valkey.xrange(runs_stream)) == 1  # the resolve turn

    now = _naive_utc(2)

    async def _scenario() -> int:
        async with _sweeper_stack(runs_stream) as (sessionmaker, queue, _client):
            async with sessionmaker() as session:
                return await sweep_expired_approvals(session, queue, now=now)

    flipped = asyncio.run(_scenario())
    assert flipped == 0

    got = approvals_client.get(
        f"/approvals/{created['id']}", headers=auth_headers
    ).json()
    assert got["status"] == "approved"

    # Exactly the one resolve turn, authored by the human resolver; no expiry
    # turn was appended.
    entries = valkey.xrange(runs_stream)
    assert len(entries) == 1
    turn = QueuedTurn.model_validate(json.loads(entries[0][1]["payload"]))
    assert turn.author == "U9"


# --- fault injection (#412 hardening) -----------------------------------------
#
# Two defects an independent review surfaced, each pinned by driving a
# Valkey enqueue failure at the exact seam that used to be unguarded:
#   A. the resolve path 500s instead of 410 when the expiry resume enqueue fails;
#   B. the sweeper must isolate a per-record enqueue failure so the batch
#      continues (one poisoned record cannot strand the rest).


def test_resolve_path_expiry_returns_410_even_if_enqueue_fails(
    approvals_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
    valkey: redis.Redis,
    runs_stream: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late resolver on a lapsed approval must still get 410 when enqueuing the
    expiry resume turn fails (a Valkey blip).

    The flip to ``expired`` already committed (``expire_approval`` commits before
    the enqueue), so the resolve path owes the caller a clean 410 conveying the
    SLA outcome -- not a 500 that buries it behind an infra error. This pins the
    #412 regression where the unguarded ``resume_queue.enqueue`` on the expiry
    branch turns the 410 into an uncaught 500.
    """

    payload = _payload(expires_in_seconds=1)
    created = approvals_client.post(
        "/approvals", json=payload, headers=auth_headers
    ).json()
    assert created["expires_at"] is not None
    time.sleep(1.1)

    async def _boom(*a: Any, **k: Any) -> str:
        raise RuntimeError("valkey down")

    # The resolve endpoint reads request.app.state.resume_queue; make its enqueue
    # fail so the expiry branch's enqueue raises.
    monkeypatch.setattr(approvals_client.app.state.resume_queue, "enqueue", _boom)

    # Observe the real 500 the error middleware returns in production, rather
    # than letting TestClient (raise_server_exceptions=True) re-raise it -- the
    # regression being pinned is precisely "500 leaks out where 410 is owed".
    monkeypatch.setattr(
        approvals_client._transport, "raise_server_exceptions", False
    )

    resolved = approvals_client.post(
        f"/approvals/{created['id']}/resolve",
        json={"decision": "approved", "resolved_by": "U9", "actor_channel": "C1"},
        headers=auth_headers,
    )
    # RED today: the unguarded enqueue raises, so the endpoint 500s (or the
    # RuntimeError propagates) instead of returning the 410 the SLA lapse owes.
    assert resolved.status_code == 410, resolved.text

    # The SLA flip still committed independently of the failed enqueue.
    got = approvals_client.get(f"/approvals/{created['id']}", headers=auth_headers)
    assert got.json()["status"] == "expired"

    # The enqueue failed, so no resume turn reached the stream.
    assert valkey.xrange(runs_stream) == []


def test_sweeper_isolates_a_failing_record(
    approvals_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
    valkey: redis.Redis,
    runs_stream: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Valkey enqueue failure on ONE lapsed record must not stop the sweeper
    from flipping and resuming the others: the per-record try/except in
    ``sweep_expired_approvals`` isolates the failure so the batch continues.

    Ordering note (robust form): ``list_expired_pending_approvals`` orders by
    ``expires_at``, so which of the two records the sweep processes first is not
    asserted here. The flaky enqueue raises on its FIRST call regardless of which
    record that is; the assertions only pin the isolation contract -- the sweep
    flips BOTH records, returns 1 (exactly one enqueue succeeded), and lands
    exactly one resume turn, belonging to whichever record was processed second.

    This may already be GREEN: the per-record ``except`` exists today. Its job is
    to lock the isolation contract so it cannot regress -- the accompanying #412
    production fix also adds a ``session.rollback()`` in that ``except`` to guard
    the rarer DB-commit-failure case, matching the convention in
    routers/approvals.py and routers/agents.py.
    """

    first = approvals_client.post(
        "/approvals", json=_payload(expires_in_seconds=1), headers=auth_headers
    ).json()
    second = approvals_client.post(
        "/approvals", json=_payload(expires_in_seconds=1), headers=auth_headers
    ).json()
    assert first["expires_at"] is not None
    assert second["expires_at"] is not None

    ids = {first["id"], second["id"]}
    # Map conversation_id -> record id so we can identify which record the single
    # surviving turn belongs to, without assuming processing order.
    conversation_to_id = {
        first["conversation_id"]: first["id"],
        second["conversation_id"]: second["id"],
    }
    now = _naive_utc(2)

    async def _scenario() -> int:
        async with _sweeper_stack(runs_stream) as (sessionmaker, queue, _client):
            orig = queue.enqueue
            calls = {"n": 0}

            async def _flaky_enqueue(turn: QueuedTurn) -> str:
                # Raise on the first record's enqueue (a Valkey blip), then let
                # every later record enqueue for real.
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("valkey down")
                return await orig(turn)

            monkeypatch.setattr(queue, "enqueue", _flaky_enqueue)
            async with sessionmaker() as session:
                return await sweep_expired_approvals(session, queue, now=now)

    swept = asyncio.run(_scenario())
    # One enqueue raised (record processed first), one succeeded (processed
    # second): the poisoned record did not block the batch.
    assert swept == 1

    # Both flips committed -- expire_approval commits before the enqueue runs, so
    # the record whose enqueue failed is still expired.
    for record in (first, second):
        got = approvals_client.get(
            f"/approvals/{record['id']}", headers=auth_headers
        ).json()
        assert got["status"] == "expired"

    # Exactly one resume turn on the stream, for whichever record was second.
    entries = valkey.xrange(runs_stream)
    assert len(entries) == 1
    turn = QueuedTurn.model_validate(json.loads(entries[0][1]["payload"]))
    assert turn.conversation_id in conversation_to_id
    resumed_id = conversation_to_id[turn.conversation_id]
    assert resumed_id in ids
    assert turn.event_id == f"approval-{resumed_id}-resolved"
    assert turn.author == "system"


def test_sweeper_disabled_when_interval_nonpositive(
    _disposable_db: Any,
) -> None:
    prior = os.environ.get("APPROVAL_SWEEP_INTERVAL_S")
    os.environ["APPROVAL_SWEEP_INTERVAL_S"] = "0"
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            # Disabled: no task started, and shutdown (the `with` exit) is
            # unconditional-safe against the None state.
            assert client.app.state.sweeper_task is None
    finally:
        if prior is None:
            os.environ.pop("APPROVAL_SWEEP_INTERVAL_S", None)
        else:
            os.environ["APPROVAL_SWEEP_INTERVAL_S"] = prior
        get_settings.cache_clear()


def test_run_expiry_sweeper_loop_sweeps_and_stops(
    approvals_client: TestClient,
    auth_headers: dict[str, str],
    clean_db: None,
    valkey: redis.Redis,
    runs_stream: str,
) -> None:
    created = approvals_client.post(
        "/approvals", json=_payload(expires_in_seconds=1), headers=auth_headers
    ).json()
    approval_id = uuid.UUID(created["id"])

    async def _scenario() -> None:
        async with _sweeper_stack(runs_stream) as (sessionmaker, queue, client):
            stop = asyncio.Event()
            task = asyncio.create_task(
                run_expiry_sweeper(sessionmaker, queue, 0.05, stop)
            )
            try:
                # Poll (bounded) until the loop observes the lapse and flips it.
                deadline = asyncio.get_running_loop().time() + 5.0
                status = created["status"]
                while status != "expired":
                    assert asyncio.get_running_loop().time() < deadline, (
                        "sweeper loop never flipped the lapsed record"
                    )
                    await asyncio.sleep(0.1)
                    async with sessionmaker() as session:
                        record = await crud.get_approval(session, approval_id)
                        assert record is not None
                        status = record.status
                # The loop also enqueued the expiry resume turn.
                assert len(await client.xrange(runs_stream)) == 1
            finally:
                stop.set()
                # Wait-first loop wakes immediately on stop; it must finish
                # promptly, not hang.
                await asyncio.wait_for(task, 2)
            assert task.done()

    asyncio.run(_scenario())
