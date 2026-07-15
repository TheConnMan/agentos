"""Approval-resolution fan-out: enqueue the resume turn onto the runs stream.

Resolving a pending approval (#244, ADR-0010) must wake the suspended session.
Rather than a bespoke resume channel, the API appends a normal ``QueuedTurn``
onto the same ``agentos:runs`` stream the dispatcher feeds, so the resume walks
the identical consumer -> kernel -> claim path a Slack mention takes: the kernel
finds the thread's route suspended and rehydrates it (ADR-0003), and the
platform-authored resolution text becomes the turn that continues the run.

Wire encoding mirrors the dispatcher's seam exactly (a single ``payload`` field
holding the model's JSON). The turn's ``event_id`` is deterministic per
approval, so the worker's done-marker dedupes any double-enqueue.
"""

from datetime import UTC, datetime
from typing import Any

import redis.asyncio as redis
from aci_protocol import QueuedTurn, ReplyHandle

from .models import Approval

RUNS_STREAM = "agentos:runs"
STREAM_PAYLOAD_FIELD = "payload"


def resume_event_id(approval_id: object) -> str:
    """The deterministic idempotency key of an approval's resume turn."""

    return f"approval-{approval_id}-resolved"


def build_resume_turn(approval: Approval) -> QueuedTurn:
    """The turn that continues a suspended session with the human decision.

    The text is platform-authored (not user input): it tells the model what was
    decided, by whom, and to carry on accordingly. The reply handle replays the
    requesting turn's placeholder so the resumed run streams into the same
    message the "awaiting approval" notice was left on.
    """

    decision = approval.status
    note = f" Note: {approval.resolution_note}." if approval.resolution_note else ""
    text = (
        f"[approval resolved] The request \"{approval.summary}\" was {decision} "
        f"by {approval.resolved_by}.{note} Continue the task accordingly: proceed "
        "with the approved action, or acknowledge the rejection and stop."
    )
    return QueuedTurn(
        event_id=resume_event_id(approval.id),
        conversation_id=approval.conversation_id,
        author=approval.resolved_by or "approver",
        text=text,
        reply_handle=ReplyHandle(
            channel=approval.reply_channel,
            placeholder=approval.reply_placeholder,
            endpoint=approval.reply_endpoint,
        ),
        received_at=datetime.now(UTC).isoformat(),
    )


class ResumeQueue:
    """Producer half of the resume seam (the worker's runs consumer is the other)."""

    def __init__(self, client: redis.Redis, stream: str = RUNS_STREAM) -> None:
        self._client = client
        self._stream = stream

    async def enqueue(self, turn: QueuedTurn) -> str:
        fields: dict[Any, Any] = {STREAM_PAYLOAD_FIELD: turn.model_dump_json()}
        stream_id = await self._client.xadd(self._stream, fields)
        return stream_id.decode() if isinstance(stream_id, bytes) else str(stream_id)
