"""Eval Job entrypoint: run a suite against a runner, record, report pass/fail.

This is what an eval Job runs (the API fans out Jobs per version @ sha on a PR).
It loads a suite from a JSON file, runs it against a runner endpoint, optionally
records the per-case scores to Langfuse (for the eval matrix), prints the
``EvalRunResult`` as JSON (for the PR-check reporter to read), and exits non-zero
if any case failed so the Job / GitHub check reflects the result.

Env:
    AGENTOS_EVAL_SUITE        path to the suite JSON (EvalSuite shape)
    AGENTOS_EVAL_TARGET_URL   runner base_url to evaluate against
    AGENTOS_EVAL_VERSION      version/sha tag to key results by (default "local")
    LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
                              if all set, per-case scores are recorded to Langfuse
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Mapping
from pathlib import Path

import httpx

from ..runner_client import RunnerClient
from .models import EvalRunResult, EvalSuite
from .recorder import LangfuseEvalRecorder
from .runner import EvalRunner
from .sampling import AggregationPolicy, SampleConfig


def load_suite(path: str | Path) -> EvalSuite:
    return EvalSuite.model_validate_json(Path(path).read_text())


async def run_eval_suite(
    suite: EvalSuite,
    *,
    base_url: str,
    version: str,
    recorder: LangfuseEvalRecorder | None = None,
    token: str | None = None,
    model: str | None = None,
    fake: bool = False,
    samples: SampleConfig | None = None,
) -> EvalRunResult:
    """Run a suite against a runner endpoint and, if configured, record it.

    ``model`` is the model id the suite is being run under; it is threaded onto
    the ``EvalRunResult`` so the recorder can tag the model dimension and the
    eval matrix can slice pass-rate/cost by model. ``fake`` says that runner is
    the fake model, whose turns are never graded (ADR-0055). ``samples`` is the
    multi-sample / variance-aware-grading policy (#332); the default (``None`` ->
    ``n=1``) runs each case once, unchanged.
    """
    async with RunnerClient() as runner:
        result = await EvalRunner(runner, samples=samples).run(
            suite,
            base_url=base_url,
            version=version,
            token=token,
            model=model,
            fake=fake,
        )
    if recorder is not None:
        await recorder.record(result)
    return result


def _sample_config_from_env(env: Mapping[str, str]) -> SampleConfig:
    """Build the multi-sample policy from the eval Job's env (#332).

    ``AGENTOS_EVAL_SAMPLES`` (default 1) sets N; ``AGENTOS_EVAL_AGGREGATION``
    (``majority`` | ``pass_at_k``, default ``majority``) the policy; and
    ``AGENTOS_EVAL_PASS_AT_K`` (default 1) the pass@k threshold. Absent/unset
    keys yield the backward-compatible ``n=1`` default.
    """
    n = int(env.get("AGENTOS_EVAL_SAMPLES", "1"))
    policy = AggregationPolicy(env.get("AGENTOS_EVAL_AGGREGATION", AggregationPolicy.MAJORITY))
    k = int(env.get("AGENTOS_EVAL_PASS_AT_K", "1"))
    return SampleConfig(n=n, policy=policy, k=k)


async def _main_async(env: Mapping[str, str]) -> int:
    suite = load_suite(env["AGENTOS_EVAL_SUITE"])
    base_url = env["AGENTOS_EVAL_TARGET_URL"]
    version = env.get("AGENTOS_EVAL_VERSION", "local")
    # The model dimension: the model this eval Job's runner is configured with
    # (the same AGENTOS_MODEL the runner authenticates from). Empty/unset means
    # "model unknown", recorded as no model tag.
    model = env.get("AGENTOS_MODEL") or None
    samples = _sample_config_from_env(env)

    lf_keys = ("LANGFUSE_HOST", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
    if all(k in env for k in lf_keys):
        async with httpx.AsyncClient(timeout=30.0) as client:
            recorder = LangfuseEvalRecorder(
                base_url=env["LANGFUSE_HOST"],
                public_key=env["LANGFUSE_PUBLIC_KEY"],
                secret_key=env["LANGFUSE_SECRET_KEY"],
                client=client,
            )
            result = await run_eval_suite(
                suite, base_url=base_url, version=version, recorder=recorder,
                model=model, samples=samples,
            )
    else:
        result = await run_eval_suite(
            suite, base_url=base_url, version=version, model=model, samples=samples
        )

    print(result.model_dump_json())
    # The exit code answers "did anything break", not "did anything pass": a run
    # whose cases were never graded (the fake tier) broke nothing, so failing the
    # Job on it would report a failure that did not happen. A real FAIL -- including
    # a fake turn that never completed -- is still non-zero.
    return 0 if result.completed_without_failure() else 1


def main(env: Mapping[str, str] | None = None) -> None:
    sys.exit(asyncio.run(_main_async(env if env is not None else os.environ)))


if __name__ == "__main__":
    main()
