"""Langfuse read proxy powering the Runs view."""

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth import require_api_key
from ..deps import LangfuseDep
from ..langfuse import build_tree
from ..schemas import TraceTree

router = APIRouter(
    prefix="/langfuse",
    tags=["langfuse"],
    dependencies=[Depends(require_api_key)],
)


@router.get("/traces")
async def list_traces(lf: LangfuseDep, limit: int = 20) -> list[dict[str, object]]:
    return await lf.list_traces(limit)


@router.get("/traces/{trace_id}", response_model=TraceTree)
async def get_trace(trace_id: str, lf: LangfuseDep) -> TraceTree:
    observations = await lf.get_observations(trace_id)
    if not observations:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "trace has no observations yet"
        )
    trace = await lf.get_trace(trace_id)
    return TraceTree(trace=trace, tree=build_tree(observations))
