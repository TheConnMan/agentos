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


def _build_turn(approval: Approval, *, author: str, text: str) -> QueuedTurn:
    """Assemble the resume turn shared by the resolve and expiry paths: same
    deterministic event_id, conversation, reply handle, and timestamp; only the
    author and platform-authored text differ.
    """

    return QueuedTurn(
        event_id=resume_event_id(approval.id),
        conversation_id=approval.conversation_id,
        author=author,
        text=text,
        reply_handle=ReplyHandle(
            channel=approval.reply_channel,
            placeholder=approval.reply_placeholder,
            endpoint=approval.reply_endpoint,
        ),
        received_at=datetime.now(UTC).isoformat(),
    )


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
    return _build_turn(approval, author=approval.resolved_by or "approver", text=text)


def build_expiry_resume_turn(approval: Approval) -> QueuedTurn:
    """The turn that continues a suspended session after its approval EXPIRED (#412).

    Same shape as ``build_resume_turn`` but platform-authored for an SLA lapse
    with no human decision: the text states the request timed out, that nobody
    approved or rejected it, and that the agent must proceed down its timeout
    branch WITHOUT performing the gated action. ``author`` is "system" because
    no person acted (``resolved_by`` is None on expiry).

    The ``event_id`` reuses ``resume_event_id`` -- the SAME deterministic key
    the resolve path uses. This is by design, so a redelivery of an
    ALREADY-FINISHED turn for this approval is recognized by the worker's
    done-marker and not re-run; the ``-resolved`` suffix in the key is
    historical and must NOT be forked, or that redelivery guard silently
    breaks. This is not a mid-turn dedupe: the single-wakeup guarantee for the
    expiry vs. resolve race comes from the CAS in ``crud.expire_approval``, not
    from this shared key.
    """

    text = (
        f"[approval expired] The request \"{approval.summary}\" was not approved "
        "or rejected before its deadline and has expired. Do not perform the "
        "gated action. Continue the task down its timeout path: acknowledge the "
        "expiry and proceed or stop accordingly."
    )
    return _build_turn(approval, author="system", text=text)


class ResumeQueue:
    """Producer half of the resume seam (the worker's runs consumer is the other)."""

    def __init__(self, client: redis.Redis, stream: str = RUNS_STREAM) -> None:
        self._client = client
        self._stream = stream

    async def enqueue(self, turn: QueuedTurn) -> str:
        fields: dict[Any, Any] = {STREAM_PAYLOAD_FIELD: turn.model_dump_json()}
        stream_id = await self._client.xadd(self._stream, fields)
        return stream_id.decode() if isinstance(stream_id, bytes) else str(stream_id)
