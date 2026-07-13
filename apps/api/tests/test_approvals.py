"""Approval records: resolve-once, self-approval block, and the resume wake-up.

Nothing mocked. The durable record + compare-and-set run against the disposable
migrated Postgres; the resolve wake-up is asserted on the real compose Valkey.
Pending rows are seeded through the worker's own ``ApprovalStore``, so this also
covers ``create_pending`` against real Postgres (the write path the kernel uses).
"""

import asyncio
import json
import time
import uuid
from typing import Any

import redis
from agentos_api.approvals import APPROVAL_CHANNEL
from agentos_api.config import get_settings
from agentos_worker.approvals import ApprovalStore
from agentos_worker.config import WorkerConfig
from sqlalchemy.ext.asyncio import create_async_engine


def _agent(client: Any, headers: dict[str, str]) -> str:
    resp = client.post(
        "/agents",
        json={"name": "approval-agent", "slack_channel": "C000000A01"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    agent_id: str = resp.json()["id"]
    return agent_id


def _seed_pending(
    agent_id: str,
    *,
    requested_by: str = "U_REQ",
    tool_use_id: str = "toolu_1",
    conversation_id: str = "th-1",
) -> str:
    """Insert a pending approval via the worker ApprovalStore (real Postgres)."""

    async def go() -> uuid.UUID:
        engine = create_async_engine(get_settings().database_url)
        try:
            store = ApprovalStore(engine, WorkerConfig())
            return await store.create_pending(
                agent_id=uuid.UUID(agent_id),
                conversation_id=conversation_id,
                session_id="sdk-sess-1",
                channel="C000000A01",
                reply_placeholder="p-1",
                reply_endpoint=None,
                tool="apply_discount",
                tool_use_id=tool_use_id,
                input_digest="sha256:abc",
                prompt="Apply a 30% discount to ACME-1?",
                requested_by=requested_by,
            )
        finally:
            await engine.dispose()

    return str(asyncio.run(go()))


def _read_published(pubsub: Any, timeout: float = 3.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
        if message and message.get("type") == "message":
            data: dict[str, Any] = json.loads(message["data"])
            return data
    raise AssertionError("no approval event was published")


def test_resolve_approves_and_publishes_wakeup(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    approval_id = _seed_pending(aid, requested_by="U_REQ")

    sub = redis.Redis.from_url(get_settings().valkey_dsn(), decode_responses=True)
    pubsub = sub.pubsub()
    pubsub.subscribe(APPROVAL_CHANNEL)
    pubsub.get_message(timeout=1.0)  # drain the subscribe confirmation
    try:
        resp = client.post(
            f"/agents/{aid}/approvals/{approval_id}/resolve",
            json={"decision": "approved", "actor": "U_APPROVER"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "approved"
        assert resp.json()["resolved_by"] == "U_APPROVER"

        event = _read_published(pubsub)
        assert event["approval_id"] == approval_id
        assert event["decision"] == "approved"
    finally:
        pubsub.close()
        sub.close()


def test_self_approval_is_blocked(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    approval_id = _seed_pending(aid, requested_by="U_REQ")

    resp = client.post(
        f"/agents/{aid}/approvals/{approval_id}/resolve",
        json={"decision": "approved", "actor": "U_REQ"},
        headers=auth_headers,
    )
    assert resp.status_code == 403, resp.text
    # And it stays pending, resolvable by someone else.
    got = client.get(f"/agents/{aid}/approvals/{approval_id}", headers=auth_headers)
    assert got.json()["status"] == "pending"


def test_second_resolver_gets_already_resolved(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    approval_id = _seed_pending(aid, requested_by="U_REQ")

    first = client.post(
        f"/agents/{aid}/approvals/{approval_id}/resolve",
        json={"decision": "approved", "actor": "U_A"},
        headers=auth_headers,
    )
    assert first.status_code == 200

    second = client.post(
        f"/agents/{aid}/approvals/{approval_id}/resolve",
        json={"decision": "rejected", "actor": "U_B"},
        headers=auth_headers,
    )
    assert second.status_code == 409, second.text
    assert "already resolved by U_A" in second.json()["detail"]
    # The first decision stands; the loser did not flip it.
    got = client.get(f"/agents/{aid}/approvals/{approval_id}", headers=auth_headers)
    assert got.json()["status"] == "approved"


def test_get_and_list_by_status(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    a1 = _seed_pending(aid, tool_use_id="toolu_1", conversation_id="th-1")
    a2 = _seed_pending(aid, tool_use_id="toolu_2", conversation_id="th-2")

    listed = client.get(
        f"/agents/{aid}/approvals?status_filter=pending", headers=auth_headers
    )
    assert listed.status_code == 200
    assert {a["id"] for a in listed.json()} == {a1, a2}

    one = client.get(f"/agents/{aid}/approvals/{a1}", headers=auth_headers)
    assert one.status_code == 200
    assert one.json()["tool"] == "apply_discount"


def test_resolve_unknown_approval_is_404(
    client: Any, auth_headers: dict[str, str], clean_db: None
) -> None:
    aid = _agent(client, auth_headers)
    missing = "00000000-0000-0000-0000-000000000000"
    resp = client.post(
        f"/agents/{aid}/approvals/{missing}/resolve",
        json={"decision": "approved", "actor": "U_A"},
        headers=auth_headers,
    )
    assert resp.status_code == 404, resp.text
