"""Approval resolve notification over Valkey (#22, API half).

Mirrors the kill-switch seam (``killswitch.py``): the durable ``Approval`` row in
Postgres is the source of truth; this publishes a wake-up so a running worker
resumes the suspended session within seconds instead of waiting for its periodic
reconcile sweep. A resolve that happens while every worker is down reaches no
subscriber, which is fine -- the worker's startup reconcile finds the
resolved-but-not-resumed row and resumes it. So this signal is an optimization,
never the mechanism of record.

- event: PUBLISH on ``agentos:approval-events`` a JSON {approval_id, agent_id,
  decision, ts}. The worker loads the full record by ``approval_id`` and resumes.
"""

import json
import uuid
from datetime import UTC, datetime

import redis.asyncio as redis

APPROVAL_CHANNEL = "agentos:approval-events"


class ApprovalNotifier:
    def __init__(self, client: redis.Redis) -> None:
        self._client = client

    async def resolved(
        self, approval_id: uuid.UUID, agent_id: uuid.UUID, decision: str
    ) -> None:
        """Publish that an approval was resolved. Best-effort wake-up only."""

        event = {
            "approval_id": str(approval_id),
            "agent_id": str(agent_id),
            "decision": decision,
            "ts": datetime.now(UTC).isoformat(),
        }
        await self._client.publish(APPROVAL_CHANNEL, json.dumps(event))
