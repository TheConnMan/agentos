"""Langfuse read proxy powering the Runs view."""

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import require_api_key
from ..deps import LangfuseDep
from ..langfuse import build_tree, hoist_sandbox_id
from ..metrics import agent_trace_filter
from ..schemas import TraceTree

router = APIRouter(
    prefix="/langfuse",
    tags=["langfuse"],
    dependencies=[Depends(require_api_key)],
)


@router.get("/traces")
async def list_traces(
    lf: LangfuseDep, limit: int = 20, agent_id: str | None = None
) -> list[dict[str, object]]:
    # With agent_id, filter to that agent's runs by the trace-name token
    # (agent-<id>); without it, list all recent traces.
    name_contains = agent_trace_filter(agent_id) if agent_id else None
    return await lf.list_traces(limit, name_contains)


@router.get("/traces/{trace_id}", response_model=TraceTree)
async def get_trace(trace_id: str, lf: LangfuseDep) -> TraceTree:
    observations = await lf.get_observations(trace_id)
    if not observations:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "trace has no observations yet"
        )
    trace = await lf.get_trace(trace_id)
    return TraceTree(
        trace=trace,
        tree=build_tree(observations),
        sandbox_id=hoist_sandbox_id(trace, observations),
    )
