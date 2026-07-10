"""Record eval results to Langfuse so the matrix endpoint can read the grid.

Per the architecture (detailed-architecture section 4), eval Jobs write scores to
Langfuse and the eval matrix reads the grid back keyed by version tag. This
recorder posts, for each case, a Langfuse trace (input/output/metadata, tagged
with the version and suite) plus an ``eval_pass`` numeric score (1.0 / 0.0) to the
public ingestion API. The API server's matrix endpoint (a J1/observability
handoff, not built here) then queries scores/traces filtered by the version tag to
assemble the grid across N pinned versions.

Ingestion is asynchronous on Langfuse v3 (queued, then materialized in
ClickHouse), so a read-back is eventually consistent, not immediate.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from .models import EvalCaseResult, EvalRunResult

SCORE_NAME = "eval_pass"


class IngestionError(Exception):
    """Langfuse accepted the ingestion request but rejected one or more events."""


class LangfuseEvalRecorder:
    """Writes eval traces + scores to a Langfuse project via the ingestion API."""

    def __init__(
        self,
        *,
        base_url: str,
        public_key: str,
        secret_key: str,
        client: httpx.AsyncClient,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._auth = (public_key, secret_key)
        self._client = client

    async def record(self, run: EvalRunResult) -> list[str]:
        """Ingest one trace + score per case. Returns the created trace ids."""
        if not run.results:
            # No cases: don't POST a hollow ingestion batch (an empty trace shell
            # in Langfuse). Nothing to record, so return before touching the API.
            return []
        now = _now_iso()
        batch: list[dict[str, Any]] = []
        trace_ids: list[str] = []
        for result in run.results:
            trace_id = uuid.uuid4().hex
            trace_ids.append(trace_id)
            batch.append(self._trace_event(trace_id, run, result, now))
            batch.append(self._score_event(trace_id, result, now))

        resp = await self._client.post(
            f"{self._base}/api/public/ingestion",
            json={"batch": batch},
            auth=self._auth,
        )
        resp.raise_for_status()
        # Ingestion returns 207 with a per-event errors array; a 2xx alone does
        # not mean every event was accepted, so surface partial failures.
        errors = resp.json().get("errors") or []
        if errors:
            raise IngestionError(f"Langfuse rejected {len(errors)} eval event(s): {errors}")
        return trace_ids

    def _trace_event(
        self, trace_id: str, run: EvalRunResult, result: EvalCaseResult, now: str
    ) -> dict[str, Any]:
        return {
            "id": uuid.uuid4().hex,
            "type": "trace-create",
            "timestamp": now,
            "body": {
                "id": trace_id,
                "name": f"eval:{run.suite}:{result.case_id}",
                "timestamp": now,
                "tags": ["eval", f"version:{run.version}", f"suite:{run.suite}"],
                "input": None,
                "output": result.output,
                "metadata": {
                    "version": run.version,
                    "suite": run.suite,
                    "case_id": result.case_id,
                    "passed": result.passed,
                    "latency_ms": result.latency_ms,
                    "error": result.error,
                },
            },
        }

    def _score_event(self, trace_id: str, result: EvalCaseResult, now: str) -> dict[str, Any]:
        return {
            "id": uuid.uuid4().hex,
            "type": "score-create",
            "timestamp": now,
            "body": {
                "id": uuid.uuid4().hex,
                "traceId": trace_id,
                "name": SCORE_NAME,
                "value": 1.0 if result.passed else 0.0,
                "dataType": "NUMERIC",
            },
        }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
