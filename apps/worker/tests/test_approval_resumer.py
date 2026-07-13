"""ApprovalResumer: a resolved approval becomes a synthetic resume turn (#22).

Against the real compose Valkey (never mocked): the resumer enqueues onto a real
stream and we read it back. The ApprovalStore is a recording double so the test
does not need Postgres; the store's real SQL is covered in the API suite. What is
under test is the resume-enqueue logic (stable event id, rebuilt reply handle,
outcome text) and the reconcile/pubsub entry points.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from agentos_dispatcher.queue import from_stream_fields
from agentos_worker.approval_resumer import ApprovalResumer
from agentos_worker.approvals import ApprovalRecord
from agentos_worker.config import WorkerConfig
from redis.asyncio import Redis as AsyncRedis

_VALKEY_HOST = os.environ.get("TEST_VALKEY_HOST", "localhost")
_VALKEY_PORT = int(os.environ.get("TEST_VALKEY_PORT", "26379"))
_VALKEY_PW = os.environ.get("TEST_VALKEY_PW", "valkeypass")


class FakeStore:
    """Records mark_resumed; serves a canned resolved-unresumed set."""

    def __init__(self, records: list[ApprovalRecord]) -> None:
        self._records = records
        self.resumed: list[uuid.UUID] = []

    async def list_resolved_unresumed(self) -> list[ApprovalRecord]:
        return list(self._records)

    async def get(self, approval_id: uuid.UUID) -> ApprovalRecord | None:
        return next((r for r in self._records if r.id == approval_id), None)

    async def mark_resumed(self, approval_id: uuid.UUID) -> bool:
        self.resumed.append(approval_id)
        return True


def _record(status: str, *, tool: str = "apply_discount") -> ApprovalRecord:
    return ApprovalRecord(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        conversation_id="th-9",
        session_id="sdk-sess-1",
        channel="C000000A01",
        reply_placeholder="p-9",
        reply_endpoint=None,
        tool=tool,
        prompt="Apply a 30% discount to ACME-1?",
        status=status,
    )


@asynccontextmanager
async def _resumer(
    records: list[ApprovalRecord],
) -> AsyncIterator[tuple[ApprovalResumer, FakeStore, AsyncRedis, str]]:
    client: AsyncRedis = AsyncRedis(
        host=_VALKEY_HOST,
        port=_VALKEY_PORT,
        password=_VALKEY_PW or None,
        decode_responses=True,
    )
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        pytest.skip(f"Valkey not reachable: {exc}")
    stream = f"test:agentos:runs:{uuid.uuid4().hex}"
    store = FakeStore(records)
    config = WorkerConfig(stream=stream)
    resumer = ApprovalResumer(redis=client, store=store, config=config)
    try:
        yield resumer, store, client, stream
    finally:
        await client.delete(stream)
        await client.aclose()


def test_reconcile_enqueues_synthetic_resume_turn() -> None:
    async def go() -> None:
        rec = _record("approved")
        async with _resumer([rec]) as (resumer, store, client, stream):
            count = await resumer.reconcile_once()
            assert count == 1
            assert store.resumed == [rec.id]

            entries = await client.xrange(stream)
            assert len(entries) == 1
            _entry_id, fields = entries[0]
            turn = from_stream_fields(fields)
            # Stable event id -> the kernel's done-marker dedupes a re-enqueue.
            assert turn.event_id == f"approval-resume-{rec.id}"
            assert turn.conversation_id == "th-9"
            # Reply handle rebuilt from the durable record, not a live route.
            assert turn.reply_handle.channel == "C000000A01"
            assert turn.reply_handle.placeholder == "p-9"
            assert "approved" in turn.text.lower()

    asyncio.run(go())


def test_reject_resume_carries_the_rejection() -> None:
    async def go() -> None:
        rec = _record("rejected")
        async with _resumer([rec]) as (resumer, _store, client, stream):
            await resumer.reconcile_once()
            entries = await client.xrange(stream)
            turn = from_stream_fields(entries[0][1])
            assert "rejected" in turn.text.lower()

    asyncio.run(go())


def test_pubsub_handle_resumes_the_named_approval() -> None:
    async def go() -> None:
        rec = _record("approved")
        async with _resumer([rec]) as (resumer, store, client, stream):
            message = {"data": json.dumps({"approval_id": str(rec.id)})}
            await resumer._handle(message)
            assert store.resumed == [rec.id]
            entries = await client.xrange(stream)
            assert len(entries) == 1
            assert from_stream_fields(entries[0][1]).conversation_id == "th-9"

    asyncio.run(go())
