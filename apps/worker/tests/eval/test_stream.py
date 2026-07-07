"""F3 eval-stream consumer against real Valkey + real MinIO + real Langfuse, with
only the platform-report HTTP POST mocked (the external-service rule).

The consumer reads ``agentos:evals``, loads the suite from the version's MinIO
bundle, runs it against either the payload's ``target_url`` (the dev/test shortcut)
or a runner it provisions via the G1 substrate, records per-case scores to
Langfuse, POSTs a summary to the platform API, and only then acks. These tests
provoke each contract: the full seam cycle, the poison-pill drop, a missing-bundle
failed run, ack-after-report even when the report terminally fails, and a
provisioned-runner end-to-end (no ``target_url``) that tears the sandbox down.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import redis
from agentos_worker.binding import BUDGET_ENV, BUNDLE_REF_ENV
from agentos_worker.bundle_store import BundleStore
from agentos_worker.config import WorkerConfig
from agentos_worker.eval import (
    EvalCase,
    EvalReporter,
    EvalStreamConsumer,
    EvalSuite,
    EvalWorkItem,
    Grader,
    GraderKind,
    LangfuseEvalRecorder,
)
from agentos_worker.sandbox import AffinityStore, SandboxSubstrate, SubstrateConfig
from agentos_worker.sandbox.types import ClaimView, SandboxView
from redis.asyncio import Redis as AsyncRedis

_VH = os.environ.get("TEST_VALKEY_HOST", "localhost")
_VP = int(os.environ.get("TEST_VALKEY_PORT", "26379"))
_VPW = os.environ.get("TEST_VALKEY_PW", "valkeypass")
CONTAINS = GraderKind.CONTAINS


async def _wait_until(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


class _StubRepo:
    """The B1 repo lookup, stubbed: a channel/agent resolves to a GitHub repo."""

    async def repo_full_name(self, _agent_id: uuid.UUID) -> str:
        return "owner/repo"


class _UnusedSubstrate:
    """A substrate that must never be touched. Passed to the target_url tests so a
    provisioning call is a hard failure, proving the shortcut path bypasses G1."""

    def claim(self, *_args: object, **_kwargs: object) -> Any:
        raise AssertionError("target_url path must not provision a sandbox")

    def release(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("target_url path must not release a sandbox")


# --- Fake Kubernetes client for the provisioned-runner test -------------------
# Sandboxes resolve to 127.0.0.1 so the real RunnerClient dials the in-process
# fake eval runner (only the model behind the runner is faked).


@dataclass
class _FakeClaim:
    name: str
    sandbox_name: str
    labels: dict[str, str]
    env: dict[str, str]


@dataclass
class _FakeK8s:
    namespace: str = "test-ns"
    claims: dict[str, _FakeClaim] = field(default_factory=dict)
    claim_envs: list[dict[str, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def create_claim(
        self,
        name: str,
        *,
        pool: str,
        env: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> None:
        self.claim_envs.append(dict(env or {}))
        self.claims[name] = _FakeClaim(
            name=name,
            sandbox_name=f"sbx-{name}",
            labels={"agentos.dev/managed-by": "agentos-sandbox-substrate", **(labels or {})},
            env=dict(env or {}),
        )

    def get_claim(self, name: str) -> ClaimView | None:
        claim = self.claims.get(name)
        if claim is None:
            return None
        return ClaimView(
            name=claim.name, ready=True, sandbox_name=claim.sandbox_name, labels=dict(claim.labels)
        )

    def delete_claim(self, name: str) -> None:
        self.claims.pop(name, None)
        self.deleted.append(name)

    def list_claims(self, *, label_selector: str) -> list[ClaimView]:
        key, _, value = label_selector.partition("=")
        out = []
        for claim in self.claims.values():
            if claim.labels.get(key) == value:
                view = self.get_claim(claim.name)
                assert view is not None
                out.append(view)
        return out

    def get_sandbox(self, name: str) -> SandboxView | None:
        if not any(c.sandbox_name == name for c in self.claims.values()):
            return None
        return SandboxView(
            name=name, ready=True, service_fqdn="127.0.0.1", operating_mode="Running"
        )

    def set_sandbox_mode(self, name: str, mode: str) -> None:  # pragma: no cover - unused here
        pass


def _cfg(stream: str, group: str, **overrides: object) -> WorkerConfig:
    base: dict[str, object] = {
        "valkey_host": _VH,
        "valkey_port": _VP,
        "valkey_password": _VPW,
        "eval_stream": stream,
        "eval_consumer_group": group,
        "read_block_ms": 100,
    }
    base.update(overrides)
    return WorkerConfig(**base)


def _item(
    *, suite: str, sha: str, bundle_ref: str | None, target_url: str | None
) -> EvalWorkItem:
    return EvalWorkItem(
        agent_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        sha=sha,
        suite=suite,
        bundle_ref=bundle_ref,
        target_url=target_url,
        requested_at="2026-07-05T00:00:00+00:00",
    )


def _build_consumer(
    *,
    redis_client: AsyncRedis,
    cfg: WorkerConfig,
    bundle_store: BundleStore,
    substrate: Any,
    reports: list[dict[str, Any]],
    lf_client: httpx.AsyncClient,
    report_status: int = 200,
) -> EvalStreamConsumer:
    def handler(request: httpx.Request) -> httpx.Response:
        reports.append(json.loads(request.content))
        return httpx.Response(report_status, json={"ok": report_status < 400})

    report_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reporter = EvalReporter(
        api_base_url="http://api.local",
        api_key="k",
        client=report_client,
        max_attempts=2,
        backoff_base_s=0.001,
    )
    recorder = LangfuseEvalRecorder(
        base_url=cfg.langfuse_host,
        public_key=cfg.langfuse_public_key,
        secret_key=cfg.langfuse_secret_key,
        client=lf_client,
    )
    return EvalStreamConsumer(
        redis=redis_client,
        config=cfg,
        bundle_store=bundle_store,
        substrate=substrate,
        reporter=reporter,
        recorder=recorder,
        repo_lookup=_StubRepo(),
    )


async def _drain_one(consumer: EvalStreamConsumer, reports: list[dict[str, Any]]) -> None:
    task = asyncio.create_task(consumer.run())
    try:
        await _wait_until(lambda: bool(reports))
    finally:
        consumer.request_stop()
        await task


def test_seam_full_consume_eval_report_cycle(make_eval_harness, bundles) -> None:
    """XADD the exact stream payload -> one full consume->eval->report cycle: the
    suite is loaded from the real MinIO bundle, run against target_url, scored to
    Langfuse keyed by version, reported with resolved repo + counts, and acked."""
    store, upload = bundles

    async def go() -> None:
        async with make_eval_harness() as (base_url, fake, _client):
            fake.responses = {"2+2": "the answer is 4", "cap of france": "London"}
            bundle_ref = upload(
                EvalSuite(
                    name="basics",
                    cases=[
                        EvalCase(id="m", input="2+2", grader=Grader(kind=CONTAINS, expected="4")),
                        EvalCase(
                            id="g",
                            input="cap of france",
                            grader=Grader(kind=CONTAINS, expected="Paris"),
                        ),
                    ],
                )
            )
            token = uuid.uuid4().hex[:8]
            cfg = _cfg(f"test:evals:{token}", f"g-{token}")
            client = AsyncRedis(host=_VH, port=_VP, password=_VPW, decode_responses=True)
            reports: list[dict[str, Any]] = []
            async with httpx.AsyncClient(timeout=30.0) as lf_client:
                consumer = _build_consumer(
                    redis_client=client,
                    cfg=cfg,
                    bundle_store=store,
                    substrate=_UnusedSubstrate(),
                    reports=reports,
                    lf_client=lf_client,
                )
                await consumer.ensure_group()
                sha = f"sha-{token}"
                item = _item(suite="basics", sha=sha, bundle_ref=bundle_ref, target_url=base_url)
                await client.xadd(cfg.eval_stream, {"payload": item.model_dump_json()})

                await _drain_one(consumer, reports)

                # The exact payload drove one full cycle: suite ran (1/2 passed),
                # the report carries the resolved repo + sha + counts, entry acked.
                assert reports[0]["repo_full_name"] == "owner/repo"
                assert reports[0]["sha"] == sha
                assert reports[0]["passed_count"] == 1
                assert reports[0]["total"] == 2
                assert reports[0]["target_url"] == base_url
                summary = await client.xpending(cfg.eval_stream, cfg.eval_consumer_group)
                assert summary["pending"] == 0

                # Scores were recorded to the real Langfuse keyed by the version tag.
                await _assert_langfuse_traces(lf_client, cfg, sha, expected=2)

            await client.delete(cfg.eval_stream)
            await client.aclose()

    asyncio.run(go())


def test_malformed_payload_is_acked_and_dropped(make_eval_harness, bundles) -> None:
    """A payload that will never parse on any redelivery is logged, acked, and
    dropped -- never reported, never stuck pending."""
    store, _upload = bundles

    async def go() -> None:
        async with make_eval_harness() as (_base_url, _fake, _client):
            token = uuid.uuid4().hex[:8]
            cfg = _cfg(f"test:evals:{token}", f"g-{token}")
            client = AsyncRedis(host=_VH, port=_VP, password=_VPW, decode_responses=True)
            reports: list[dict[str, Any]] = []
            async with httpx.AsyncClient(timeout=30.0) as lf_client:
                consumer = _build_consumer(
                    redis_client=client,
                    cfg=cfg,
                    bundle_store=store,
                    substrate=_UnusedSubstrate(),
                    reports=reports,
                    lf_client=lf_client,
                )
                await consumer.ensure_group()
                await client.xadd(cfg.eval_stream, {"payload": "not valid json {"})

                task = asyncio.create_task(consumer.run())
                await asyncio.sleep(0.5)  # poison acks fast; give the loop a few cycles
                consumer.request_stop()
                await task

                assert reports == []  # never reported
                summary = await client.xpending(cfg.eval_stream, cfg.eval_consumer_group)
                assert summary["pending"] == 0  # acked and dropped, not stuck pending

            await client.delete(cfg.eval_stream)
            await client.aclose()

    asyncio.run(go())


def test_missing_bundle_is_a_reported_failed_run(make_eval_harness, bundles) -> None:
    """A bundle_ref that does not exist in MinIO is an unresolvable suite: a failed
    run (0/0) is reported and the entry acked, never a consumer crash."""
    store, _upload = bundles

    async def go() -> None:
        async with make_eval_harness() as (_base_url, _fake, _client):
            token = uuid.uuid4().hex[:8]
            cfg = _cfg(f"test:evals:{token}", f"g-{token}")
            client = AsyncRedis(host=_VH, port=_VP, password=_VPW, decode_responses=True)
            reports: list[dict[str, Any]] = []
            async with httpx.AsyncClient(timeout=30.0) as lf_client:
                consumer = _build_consumer(
                    redis_client=client,
                    cfg=cfg,
                    bundle_store=store,
                    substrate=_UnusedSubstrate(),
                    reports=reports,
                    lf_client=lf_client,
                )
                await consumer.ensure_group()
                sha = f"sha-{token}"
                item = _item(
                    suite="gone",
                    sha=sha,
                    bundle_ref=f"tests/bundles/does-not-exist-{token}.zip",
                    target_url=None,
                )
                await client.xadd(cfg.eval_stream, {"payload": item.model_dump_json()})

                await _drain_one(consumer, reports)

                # A failed run is reported (0/0), distinguishable from a real run,
                # and the entry is acked (a missing bundle never redelivers forever).
                assert reports[0]["sha"] == sha
                assert reports[0]["passed_count"] == 0
                assert reports[0]["total"] == 0
                summary = await client.xpending(cfg.eval_stream, cfg.eval_consumer_group)
                assert summary["pending"] == 0

            await client.delete(cfg.eval_stream)
            await client.aclose()

    asyncio.run(go())


def test_entry_is_acked_after_report_even_when_report_fails(make_eval_harness, bundles) -> None:
    """The report POST 500s on every attempt (terminal failure). The report attempt
    still completes (retried, then logged) and the entry is acked afterward, so a
    down platform API never wedges the stream -- documenting at-least-once with a
    best-effort report."""
    store, upload = bundles

    async def go() -> None:
        async with make_eval_harness() as (base_url, fake, _client):
            fake.responses = {"q": "yes"}
            bundle_ref = upload(
                EvalSuite(
                    name="one",
                    cases=[
                        EvalCase(id="1", input="q", grader=Grader(kind=CONTAINS, expected="yes"))
                    ],
                )
            )
            token = uuid.uuid4().hex[:8]
            cfg = _cfg(f"test:evals:{token}", f"g-{token}")
            client = AsyncRedis(host=_VH, port=_VP, password=_VPW, decode_responses=True)
            reports: list[dict[str, Any]] = []
            async with httpx.AsyncClient(timeout=30.0) as lf_client:
                consumer = _build_consumer(
                    redis_client=client,
                    cfg=cfg,
                    bundle_store=store,
                    substrate=_UnusedSubstrate(),
                    reports=reports,
                    lf_client=lf_client,
                    report_status=500,
                )
                await consumer.ensure_group()
                item = _item(
                    suite="one", sha=f"sha-{token}", bundle_ref=bundle_ref, target_url=base_url
                )
                await client.xadd(cfg.eval_stream, {"payload": item.model_dump_json()})

                task = asyncio.create_task(consumer.run())
                try:
                    await _wait_until(lambda: len(reports) >= 2)  # every attempt retried
                finally:
                    consumer.request_stop()
                    await task

                # Report terminally failed (logged); the entry is acked so it is not
                # redelivered forever.
                summary = await client.xpending(cfg.eval_stream, cfg.eval_consumer_group)
                assert summary["pending"] == 0

            await client.delete(cfg.eval_stream)
            await client.aclose()

    asyncio.run(go())


def test_provisioned_runner_end_to_end(make_eval_harness, bundles) -> None:
    """No target_url: the consumer provisions a runner via the G1 substrate (boot
    env carrying the bundle_ref + budget), evals against it, reports, and tears the
    sandbox down in a finally. The fake runner is the model boundary, so no real
    model is ever called."""
    store, upload = bundles

    async def go() -> None:
        async with make_eval_harness() as (base_url, fake, _client):
            fake.responses = {"ping": "pong"}
            port = int(base_url.rsplit(":", 1)[1])
            bundle_ref = upload(
                EvalSuite(
                    name="prov",
                    cases=[
                        EvalCase(
                            id="1", input="ping", grader=Grader(kind=CONTAINS, expected="pong")
                        )
                    ],
                )
            )
            token = uuid.uuid4().hex[:8]
            cfg = _cfg(f"test:evals:{token}", f"g-{token}")
            sandbox_prefix = f"test:agentos:sandbox:{token}"
            sync_client = redis.Redis(
                host=_VH, port=_VP, password=_VPW or None, decode_responses=False
            )
            fake_k8s = _FakeK8s()
            substrate = SandboxSubstrate(
                fake_k8s,  # type: ignore[arg-type]
                AffinityStore(sync_client, key_prefix=sandbox_prefix),
                SubstrateConfig(
                    namespace="test-ns",
                    warm_pool="test-pool",
                    runner_port=port,
                    route_ttl_seconds=60,
                    claim_timeout_seconds=3.0,
                    poll_interval_seconds=0.005,
                    key_prefix=sandbox_prefix,
                ),
            )
            client = AsyncRedis(host=_VH, port=_VP, password=_VPW, decode_responses=True)
            reports: list[dict[str, Any]] = []
            async with httpx.AsyncClient(timeout=30.0) as lf_client:
                consumer = _build_consumer(
                    redis_client=client,
                    cfg=cfg,
                    bundle_store=store,
                    substrate=substrate,
                    reports=reports,
                    lf_client=lf_client,
                )
                await consumer.ensure_group()
                sha = f"sha-{token}"
                item = _item(suite="prov", sha=sha, bundle_ref=bundle_ref, target_url=None)
                await client.xadd(cfg.eval_stream, {"payload": item.model_dump_json()})

                await _drain_one(consumer, reports)

                # The provisioned runner answered and the suite passed 1/1.
                assert reports[0]["passed_count"] == 1
                assert reports[0]["total"] == 1
                assert reports[0]["target_url"] is None  # provisioned, not a shortcut
                # The boot env carried the bundle ref and a budget (the F2 seam),
                assert fake_k8s.claim_envs, "substrate.claim was never called"
                assert fake_k8s.claim_envs[0][BUNDLE_REF_ENV] == bundle_ref
                assert BUDGET_ENV in fake_k8s.claim_envs[0]
                # and the sandbox was torn down after the eval (finally: release).
                assert fake_k8s.deleted, "provisioned sandbox was never released"
                assert not fake_k8s.claims

                summary = await client.xpending(cfg.eval_stream, cfg.eval_consumer_group)
                assert summary["pending"] == 0
                await _assert_langfuse_traces(lf_client, cfg, sha, expected=1)

            await client.delete(cfg.eval_stream)
            keys = list(sync_client.scan_iter(match=f"{sandbox_prefix}:*"))
            if keys:
                sync_client.delete(*keys)
            sync_client.close()
            await client.aclose()

    asyncio.run(go())


def test_pending_entry_from_a_dead_consumer_is_reclaimed(make_eval_harness, bundles) -> None:
    """An entry a crashed consumer took but never acked (still in the group PEL) is
    reclaimed via XAUTOCLAIM and re-run, so the at-least-once promise holds -- a
    crash before ack never strands the eval."""
    store, upload = bundles

    async def go() -> None:
        async with make_eval_harness() as (base_url, fake, _client):
            fake.responses = {"q": "ok"}
            bundle_ref = upload(
                EvalSuite(
                    name="recl",
                    cases=[
                        EvalCase(id="1", input="q", grader=Grader(kind=CONTAINS, expected="ok"))
                    ],
                )
            )
            token = uuid.uuid4().hex[:8]
            # Reclaim anything pending immediately, and tick the reclaim loop fast.
            cfg = _cfg(
                f"test:evals:{token}",
                f"g-{token}",
                reclaim_min_idle_ms=0,
                reclaim_interval_s=0.05,
            )
            client = AsyncRedis(host=_VH, port=_VP, password=_VPW, decode_responses=True)
            reports: list[dict[str, Any]] = []
            async with httpx.AsyncClient(timeout=30.0) as lf_client:
                consumer = _build_consumer(
                    redis_client=client,
                    cfg=cfg,
                    bundle_store=store,
                    substrate=_UnusedSubstrate(),
                    reports=reports,
                    lf_client=lf_client,
                )
                await consumer.ensure_group()
                sha = f"sha-{token}"
                item = _item(suite="recl", sha=sha, bundle_ref=bundle_ref, target_url=base_url)
                await client.xadd(cfg.eval_stream, {"payload": item.model_dump_json()})

                # A dead consumer takes the entry (moves it into the PEL) and never
                # acks -- the read loop's ">" will never see it again.
                await client.xreadgroup(
                    cfg.eval_consumer_group,
                    "dead-consumer",
                    {cfg.eval_stream: ">"},
                    count=10,
                )
                pending = await client.xpending(cfg.eval_stream, cfg.eval_consumer_group)
                assert pending["pending"] == 1  # stranded under the dead consumer

                await _drain_one(consumer, reports)

                # Reclaimed, re-run against the bundle, reported, and acked.
                assert reports[0]["sha"] == sha
                assert reports[0]["passed_count"] == 1
                summary = await client.xpending(cfg.eval_stream, cfg.eval_consumer_group)
                assert summary["pending"] == 0

            await client.delete(cfg.eval_stream)
            await client.aclose()

    asyncio.run(go())


async def _assert_langfuse_traces(
    lf_client: httpx.AsyncClient, cfg: WorkerConfig, sha: str, *, expected: int
) -> None:
    """Poll the real Langfuse until ``expected`` traces are visible for the version
    tag (v3 ingestion is async: queued, then materialized in ClickHouse)."""
    found = 0
    for _ in range(40):
        resp = await lf_client.get(
            f"{cfg.langfuse_host}/api/public/traces",
            params={"tags": f"version:{sha}"},
            auth=(cfg.langfuse_public_key, cfg.langfuse_secret_key),
        )
        found = len(resp.json().get("data", [])) if resp.status_code == 200 else 0
        if found >= expected:
            break
        await asyncio.sleep(1)
    assert found == expected
