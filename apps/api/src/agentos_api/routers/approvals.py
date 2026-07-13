"""Approval records: list, read, and resolve-once (#22, ADR-0010).

The durable ``Approval`` row is created by the worker when a session pauses on an
approval gate; this router is the read + resolve surface. Resolve is server-side
and resolve-once: a compare-and-set flips ``pending`` to the decision exactly
once (the loser of a click race gets 409), self-approval is refused (403), and on
a winning resolve a wake-up is published so the worker resumes the suspended
session. The Slack click path (#246) calls this endpoint after its
channel-membership authorizer check; the check is deliberately layered on top of
this server-side resolve, not a substitute for it.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from .. import crud
from ..auth import require_api_key
from ..deps import ApprovalNotifierDep, SessionDep
from ..schemas import ApprovalOut, ApprovalResolveIn

router = APIRouter(
    prefix="/agents/{agent_id}",
    tags=["approvals"],
    dependencies=[Depends(require_api_key)],
)


@router.get("/approvals", response_model=list[ApprovalOut])
async def list_approvals(
    agent_id: uuid.UUID, session: SessionDep, status_filter: str | None = None
) -> list[ApprovalOut]:
    if await crud.get_agent(session, agent_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    approvals = await crud.list_approvals(session, agent_id, status_filter)
    return [ApprovalOut.model_validate(a) for a in approvals]


@router.get("/approvals/{approval_id}", response_model=ApprovalOut)
async def get_approval(
    agent_id: uuid.UUID, approval_id: uuid.UUID, session: SessionDep
) -> ApprovalOut:
    approval = await crud.get_approval(session, approval_id)
    if approval is None or approval.agent_id != agent_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
    return ApprovalOut.model_validate(approval)


@router.post("/approvals/{approval_id}/resolve", response_model=ApprovalOut)
async def resolve_approval(
    agent_id: uuid.UUID,
    approval_id: uuid.UUID,
    data: ApprovalResolveIn,
    session: SessionDep,
    notifier: ApprovalNotifierDep,
) -> ApprovalOut:
    # Scope the resolve to the agent in the path first, so a cross-agent id is a
    # 404 and never reaches the compare-and-set.
    existing = await crud.get_approval(session, approval_id)
    if existing is None or existing.agent_id != agent_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")

    outcome, approval = await crud.resolve_approval(
        session, approval_id, data.decision, data.actor
    )
    if outcome == "self_approval":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "you cannot resolve your own approval request"
        )
    if outcome == "already_resolved":
        assert approval is not None
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"already resolved by {approval.resolved_by}",
        )
    # A winning resolve: publish the wake-up so a live worker resumes promptly.
    assert approval is not None
    await notifier.resolved(approval_id, agent_id, data.decision)
    return ApprovalOut.model_validate(approval)
