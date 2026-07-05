"""Budgets + kill switch control plane (L1, API half).

Kill/resume produce the Valkey signal the worker consumes (SET flag + PUBLISH
event); budgets persist per-agent config the worker passes through at sandbox
boot; cost composes the OB1 metrics module filtered to the agent.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from .. import crud
from .. import metrics as metrics_service
from ..auth import require_api_key
from ..config import get_settings
from ..deps import KillSwitchDep, LangfuseDep, SessionDep
from ..models import Agent
from ..schemas import BudgetConfig, CostReport, KillState

router = APIRouter(
    prefix="/agents/{agent_id}",
    tags=["control"],
    dependencies=[Depends(require_api_key)],
)


async def _load_agent(session: SessionDep, agent_id: uuid.UUID) -> Agent:
    agent = await crud.get_agent(session, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    return agent


@router.post("/kill", response_model=KillState)
async def kill_agent(
    agent_id: uuid.UUID, session: SessionDep, kill_switch: KillSwitchDep
) -> KillState:
    await _load_agent(session, agent_id)
    await kill_switch.kill(agent_id)
    return KillState(killed=True)


@router.post("/resume", response_model=KillState)
async def resume_agent(
    agent_id: uuid.UUID, session: SessionDep, kill_switch: KillSwitchDep
) -> KillState:
    await _load_agent(session, agent_id)
    await kill_switch.resume(agent_id)
    return KillState(killed=False)


@router.get("/kill", response_model=KillState)
async def get_kill_state(
    agent_id: uuid.UUID, session: SessionDep, kill_switch: KillSwitchDep
) -> KillState:
    await _load_agent(session, agent_id)
    return KillState(killed=await kill_switch.is_killed(agent_id))


@router.get("/budget", response_model=BudgetConfig)
async def get_budget(agent_id: uuid.UUID, session: SessionDep) -> BudgetConfig:
    agent = await _load_agent(session, agent_id)
    return BudgetConfig.model_validate(agent)


@router.put("/budget", response_model=BudgetConfig)
async def put_budget(
    agent_id: uuid.UUID, config: BudgetConfig, session: SessionDep
) -> BudgetConfig:
    agent = await _load_agent(session, agent_id)
    updated = await crud.update_budget(
        session,
        agent,
        config.max_usd_per_day,
        config.max_output_tokens_per_run,
    )
    return BudgetConfig.model_validate(updated)


@router.get("/cost", response_model=CostReport)
async def get_cost(
    agent_id: uuid.UUID,
    session: SessionDep,
    lf: LangfuseDep,
    start: str | None = None,
    end: str | None = None,
) -> CostReport:
    agent = await _load_agent(session, agent_id)
    window = get_settings().metrics_default_window_hours
    try:
        start_iso, end_iso = metrics_service.resolve_window(start, end, window)
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"start/end must be ISO 8601 timestamps: {exc}",
        ) from exc
    series = await metrics_service.series(
        lf, "cost_usd", start_iso, end_iso, "day", None, agent.name
    )
    return CostReport(
        start=start_iso,
        end=end_iso,
        total_usd=sum(point.value for point in series.points),
        points=series.points,
    )
