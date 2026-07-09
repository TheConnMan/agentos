"""LangfuseEvalRecorder against the REAL compose Langfuse (never mocked).

Records a run's per-case results and reads them back through Langfuse's public
API, filtered by the run's unique version tag (so the read-back sees only this
run's traces). Langfuse v3 ingestion is asynchronous (queued -> ClickHouse), so
the read-back polls with a budget rather than expecting immediate consistency.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import httpx
import pytest
from agentos_worker.eval import EvalCaseResult, EvalRunResult, LangfuseEvalRecorder
from agentos_worker.eval.recorder import SCORE_NAME

_LF_HOST = os.environ.get("TEST_LANGFUSE_HOST", "http://localhost:23000")
_LF_PK = os.environ.get("TEST_LANGFUSE_PUBLIC_KEY", "pk-lf-agentos-dev")
_LF_SK = os.environ.get("TEST_LANGFUSE_SECRET_KEY", "sk-lf-agentos-dev")


async def _traces_for_version(client: httpx.AsyncClient, version: str) -> list[dict[str, Any]]:
    resp = await client.get(
        f"{_LF_HOST}/api/public/traces",
        params={"tags": f"version:{version}"},
        auth=(_LF_PK, _LF_SK),
    )
    if resp.status_code != 200:
        return []
    data: list[dict[str, Any]] = resp.json().get("data", [])
    return data


async def _score_for_trace(client: httpx.AsyncClient, trace_id: str) -> float | None:
    resp = await client.get(f"{_LF_HOST}/api/public/traces/{trace_id}", auth=(_LF_PK, _LF_SK))
    if resp.status_code != 200:
        return None
    for score in resp.json().get("scores") or []:
        if score.get("name") == SCORE_NAME:
            return float(score["value"])
    return None


def test_empty_run_skips_the_ingestion_post() -> None:
    # A run with no case results must not POST a hollow ingestion batch; it
    # returns [] before touching the network.
    async def go() -> None:
        posts: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            posts.append(request)
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            recorder = LangfuseEvalRecorder(
                base_url="http://langfuse.invalid",
                public_key="pk",
                secret_key="sk",
                client=client,
            )
            run = EvalRunResult(version="v-empty", suite="empty-suite", results=[])
            assert await recorder.record(run) == []
        assert posts == []  # no /api/public/ingestion call was made

    asyncio.run(go())


def test_records_per_case_results_and_reads_them_back() -> None:
    async def go() -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                (await client.get(f"{_LF_HOST}/api/public/health")).raise_for_status()
            except httpx.HTTPError as exc:
                pytest.skip(f"Langfuse not reachable at {_LF_HOST}: {exc}")

            recorder = LangfuseEvalRecorder(
                base_url=_LF_HOST, public_key=_LF_PK, secret_key=_LF_SK, client=client
            )
            version = f"v-{uuid.uuid4().hex[:8]}"
            run = EvalRunResult(
                version=version,
                suite="recorder-test",
                results=[
                    EvalCaseResult(case_id="pass-case", passed=True, output="4", latency_ms=1.0),
                    EvalCaseResult(case_id="fail-case", passed=False, output="x", latency_ms=1.0),
                ],
            )
            await recorder.record(run)

            # Poll for the async-ingested traces (keyed by the unique version tag).
            traces: list[dict[str, Any]] = []
            for _ in range(40):
                traces = await _traces_for_version(client, version)
                if len(traces) >= 2:
                    break
                await asyncio.sleep(1)
            assert len(traces) == 2, f"eval traces did not materialize for {version}: {traces}"

            passed_by_name = {t["name"]: (t.get("metadata") or {}).get("passed") for t in traces}
            assert passed_by_name == {
                "eval:recorder-test:pass-case": True,
                "eval:recorder-test:fail-case": False,
            }

            # Each trace carries its eval_pass score matching pass/fail.
            for trace in traces:
                expected = 1.0 if (trace.get("metadata") or {}).get("passed") else 0.0
                assert await _score_for_trace(client, trace["id"]) == expected

    asyncio.run(go())
