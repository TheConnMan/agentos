"""Durable approvals: create, inspect, and resolve-once (#244, ADR-0010).

The worker creates a record when a run ends awaiting-approval and suspends the
session; resolving the record enqueues the resume turn back onto the runs
stream, so the whole pause/resume survives every component restarting. The
resolve endpoint owns the claim race: a conditional UPDATE picks exactly one
winner, losers get 409 with who resolved it, and a past-SLA record flips to
expired (410) instead of resolving.

Authorization today is the shared API key, like every router. WHO may resolve
(channel membership, self-approval block) is the server-side authorizer of
#246 and slots in at this endpoint -- the decision point is deliberately here,
on the server that owns the record, never inside the sandbox.
"""

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError

from .. import crud
from ..auth import require_api_key
from ..authorizer import Authorizer, ChannelMembershipAuthorizer
from ..deps import ResumeQueueDep, SessionDep
from ..models import Approval, ApprovalStatus
from ..resumequeue import build_resume_turn
from ..schemas import ApprovalCreate, ApprovalOut, ApprovalResolve

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/approvals", tags=["approvals"], dependencies=[Depends(require_api_key)]
)

# The server-side authorizer (#246): channel membership is the first
# implementation of the swappable Authorizer seam (user-group, user-list, and
# platform-RBAC come later); it is module-level state because the decision is
# pure over the record and the attempt.
_AUTHORIZER: Authorizer = ChannelMembershipAuthorizer()


def _expired(approval: Approval) -> bool:
    """True when a pending record's SLA has passed (naive-UTC comparison,
    matching the DateTime columns)."""

    return (
        approval.expires_at is not None
        and approval.expires_at <= datetime.now(UTC).replace(tzinfo=None)
    )


@router.post("", response_model=ApprovalOut, status_code=status.HTTP_201_CREATED)
async def create_approval(
    data: ApprovalCreate, session: SessionDep, response: Response
) -> ApprovalOut:
    """Create a pending approval; idempotent on ``dedupe_key``.

    A redelivered worker turn that re-requests the same approval gets the
    existing record back (200) instead of forking a second pending record for
    one human decision.
    """

    try:
        approval = await crud.create_approval(session, data)
    except IntegrityError as exc:
        await session.rollback()
        existing = await crud.get_approval_by_dedupe_key(session, data.dedupe_key)
        if existing is None:  # raced with a delete; surface the conflict as-is
            raise HTTPException(
                status.HTTP_409_CONFLICT, "approval violates a uniqueness constraint"
            ) from exc
        response.status_code = status.HTTP_200_OK
        return ApprovalOut.model_validate(existing)
    return ApprovalOut.model_validate(approval)


@router.get("", response_model=list[ApprovalOut])
async def list_approvals(
    session: SessionDep,
    status_filter: str | None = None,
    agent_id: uuid.UUID | None = None,
    conversation_id: str | None = None,
    limit: int = 50,
) -> list[ApprovalOut]:
    approvals = await crud.list_approvals(
        session,
        status=status_filter,
        agent_id=agent_id,
        conversation_id=conversation_id,
        limit=min(max(limit, 1), 200),
    )
    return [ApprovalOut.model_validate(a) for a in approvals]


@router.get("/{approval_id}", response_model=ApprovalOut)
async def get_approval(approval_id: uuid.UUID, session: SessionDep) -> ApprovalOut:
    approval = await crud.get_approval(session, approval_id)
    if approval is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
    return ApprovalOut.model_validate(approval)


@router.post("/{approval_id}/resolve", response_model=ApprovalOut)
async def resolve_approval(
    approval_id: uuid.UUID,
    data: ApprovalResolve,
    session: SessionDep,
    resume_queue: ResumeQueueDep,
) -> ApprovalOut:
    """Claim the resolution (resolve-once) and wake the suspended session.

    The authorizer runs first, server-side (#246): self-approval is blocked
    and channel membership is checked against the attempt's channel; a denied
    actor gets 403 with the reason. Then exactly one authorized resolver wins
    the conditional UPDATE; a loser gets 409 naming who resolved it, and a
    past-SLA record flips to expired and returns 410. The winner's response is
    sent only after the resume turn is enqueued.
    """

    approval = await crud.get_approval(session, approval_id)
    if approval is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")

    decision = _AUTHORIZER.authorize(approval, data.resolved_by, data.actor_channel)
    if not decision.allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, decision.reason)

    if approval.status == ApprovalStatus.pending and _expired(approval):
        expired = await crud.expire_approval(session, approval_id)
        # None means a concurrent resolution won the CAS before the expiry did;
        # fall through to the claim below, which will lose and report the winner.
        if expired is not None:
            raise HTTPException(
                status.HTTP_410_GONE,
                f"approval expired at {expired.expires_at} and can no longer be resolved",
            )

    claimed = await crud.claim_approval_resolution(
        session,
        approval_id,
        decision=data.decision,
        resolved_by=data.resolved_by,
        note=data.note,
    )
    if claimed is None:
        current = await crud.get_approval(session, approval_id)
        if current is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
        if current.status == ApprovalStatus.expired:
            raise HTTPException(
                status.HTTP_410_GONE,
                "approval expired and can no longer be resolved",
            )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"already resolved by {current.resolved_by} ({current.status})",
        )

    # Wake the suspended session: the resume turn rides the normal runs stream,
    # so the kernel's ordinary consume -> claim path rehydrates the thread
    # (ADR-0003) and delivers the decision. The turn's event_id is deterministic
    # per approval, so the worker's done-marker absorbs a duplicate enqueue.
    stream_id = await resume_queue.enqueue(build_resume_turn(claimed))
    logger.info(
        "approval %s %s by %s; resume turn enqueued (%s)",
        approval_id,
        claimed.status,
        claimed.resolved_by,
        stream_id,
    )
    return ApprovalOut.model_validate(claimed)
