"""Harness for the kernel tests.

Real Valkey (compose stack) for stream, locks, and markers; the real G1
``SandboxSubstrate`` with a fake Kubernetes client whose sandboxes resolve to
``127.0.0.1`` so the real ``RunnerClient`` dials an in-process aiohttp fake
runner. The only mocked collaborators are the Slack sink and the model behind the
runner (the fake runner scripts NDJSON frames directly). Async setup is done
inside each test's ``asyncio.run`` body (the repo runs async tests without a
pytest-asyncio plugin), so the reusable pieces here are plain classes plus a few
sync fixtures.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field

import pytest
import redis
from aci_protocol import Final, OutboundEvent, SessionStatus
from agentos_worker.config import WorkerConfig
from agentos_worker.kernel import Kernel
from agentos_worker.markers import Markers
from agentos_worker.runner_client import RunnerClient
from agentos_worker.sandbox import AffinityStore, SandboxSubstrate, SubstrateConfig
from agentos_worker.sandbox.types import ClaimView, SandboxView
from agentos_worker.slack_sink import SlackSink
from agentos_worker.threadlock import ThreadLock
from aiohttp import web
from aiohttp.test_utils import TestServer
from redis.asyncio import Redis as AsyncRedis

_VALKEY_HOST = os.environ.get("TEST_VALKEY_HOST", "localhost")
_VALKEY_PORT = int(os.environ.get("TEST_VALKEY_PORT", "56379"))
_VALKEY_PW = os.environ.get("TEST_VALKEY_PW", "valkeypass")


@pytest.fixture
def sync_redis() -> Iterator[redis.Redis]:
    client = redis.Redis(
        host=_VALKEY_HOST, port=_VALKEY_PORT, password=_VALKEY_PW or None, decode_responses=True
    )
    try:
        client.ping()
    except redis.exceptions.RedisError as exc:
        pytest.skip(f"Valkey not reachable: {exc}")
    yield client
    client.close()


@pytest.fixture
def names(sync_redis: redis.Redis) -> Iterator[dict[str, str]]:
    """Per-test-unique stream / group / key prefixes on the shared Valkey."""
    token = uuid.uuid4().hex
    ns = {
        "stream": f"test:agentos:runs:{token}",
        "group": f"g-{token}",
        "prefix": f"test:agentos:worker:{token}",
        "sandbox_prefix": f"test:agentos:sandbox:{token}",
    }
    yield ns
    for pat in (f"{ns['prefix']}*", f"{ns['sandbox_prefix']}*", ns["stream"]):
        keys = list(sync_redis.scan_iter(match=pat))
        if keys:
            sync_redis.delete(*keys)


def make_config(names: dict[str, str], **overrides: object) -> WorkerConfig:
    base: dict[str, object] = {
        "valkey_host": _VALKEY_HOST,
        "valkey_port": _VALKEY_PORT,
        "valkey_password": _VALKEY_PW,
        "stream": names["stream"],
        "consumer_group": names["group"],
        "consumer_name": "test-consumer",
        "key_prefix": names["prefix"],
        "slack_edit_min_interval_s": 0.0,
        "max_attempts": 3,
        "retry_backoff_base_s": 0.001,
        "retry_backoff_max_s": 0.01,
        "lock_ttl_ms": 5000,
        "lock_acquire_timeout_s": 5.0,
        "reclaim_min_idle_ms": 50,
        "reclaim_interval_s": 0.05,
        "read_block_ms": 100,
    }
    base.update(overrides)
    return WorkerConfig(**base)


# --- Fake Slack sink ----------------------------------------------------------


class FakeSink(SlackSink):
    """Records every chat.update the kernel makes."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, str, str]] = []

    async def update(self, *, channel: str, ts: str, text: str) -> None:
        self.updates.append((channel, ts, text))

    @property
    def last_text(self) -> str | None:
        return self.updates[-1][2] if self.updates else None


# --- Fake Kubernetes client (sandboxes resolve to 127.0.0.1) ------------------


@dataclass
class _FakeClaim:
    name: str
    sandbox_name: str
    labels: dict[str, str]
    ready: bool = True


@dataclass
class _FakeSandbox:
    name: str
    operating_mode: str = "Running"


@dataclass
class FakeK8s:
    """In-memory agent-sandbox model whose sandboxes dial the local fake runner."""

    namespace: str = "test-ns"
    claims: dict[str, _FakeClaim] = field(default_factory=dict)
    sandboxes: dict[str, _FakeSandbox] = field(default_factory=dict)

    def create_claim(
        self,
        name: str,
        *,
        pool: str,
        env: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> None:
        sandbox_name = f"sbx-{name}"
        self.claims[name] = _FakeClaim(
            name=name,
            sandbox_name=sandbox_name,
            labels={"agentos.dev/managed-by": "agentos-sandbox-substrate", **(labels or {})},
        )
        self.sandboxes[sandbox_name] = _FakeSandbox(name=sandbox_name)

    def get_claim(self, name: str) -> ClaimView | None:
        claim = self.claims.get(name)
        if claim is None:
            return None
        return ClaimView(
            name=claim.name,
            ready=claim.ready,
            sandbox_name=claim.sandbox_name if claim.ready else None,
            labels=dict(claim.labels),
        )

    def delete_claim(self, name: str) -> None:
        claim = self.claims.pop(name, None)
        if claim is not None:
            self.sandboxes.pop(claim.sandbox_name, None)

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
        sandbox = self.sandboxes.get(name)
        if sandbox is None:
            return None
        # 127.0.0.1 so RunnerClient reaches the in-process fake runner.
        return SandboxView(
            name=sandbox.name,
            ready=True,
            service_fqdn="127.0.0.1",
            operating_mode=sandbox.operating_mode,
        )

    def set_sandbox_mode(self, name: str, mode: str) -> None:
        self.sandboxes[name].operating_mode = mode


# --- Fake runner (aiohttp) ----------------------------------------------------


class FakeRunner:
    """A scriptable ACI runner over HTTP.

    ``turn_scripts`` is a FIFO of frame lists, one consumed per ``/v1/event``;
    when exhausted it falls back to ``default_script``. A script that omits a
    ``final`` and ends the stream models a mid-run drop (no terminal). ``hold``
    (an asyncio.Event set by the test or by ``/v1/interrupt``) makes a turn hang
    active after its prefix so steer/interrupt can be exercised against a live
    turn; ``tail`` frames flush on release.
    """

    def __init__(self) -> None:
        self.app = web.Application()
        self.app.add_routes(
            [
                web.get("/status", self._status),
                web.post("/v1/event", self._event),
                web.post("/v1/steer", self._steer),
                web.post("/v1/interrupt", self._interrupt),
            ]
        )
        self.turn_active = False
        self.turn_scripts: list[list[OutboundEvent]] = []
        self.default_script: list[OutboundEvent] = [Final(text="ok", status=SessionStatus.DONE)]
        self.opened: list[str] = []
        self.steers: list[str] = []
        self.interrupts: int = 0
        self.hold: object | None = None  # asyncio.Event when a turn should hang
        self.tail: list[OutboundEvent] = []
        self.event_fail_times: int = 0  # return 500 on the next N /v1/event calls

    async def _status(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "idle-awaiting-input", "turn_active": self.turn_active})

    async def _event(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.opened.append(body["text"])
        if self.event_fail_times > 0:
            self.event_fail_times -= 1
            return web.json_response({"error": "transient runner failure"}, status=500)
        script = self.turn_scripts.pop(0) if self.turn_scripts else list(self.default_script)
        resp = web.StreamResponse(status=200, headers={"Content-Type": "application/x-ndjson"})
        await resp.prepare(request)
        self.turn_active = True
        for frame in script:
            await resp.write((frame.model_dump_json() + "\n").encode("utf-8"))
        if self.hold is not None:
            await self.hold.wait()  # type: ignore[attr-defined]
            for frame in self.tail:
                await resp.write((frame.model_dump_json() + "\n").encode("utf-8"))
        self.turn_active = False
        await resp.write_eof()
        return resp

    async def _steer(self, request: web.Request) -> web.Response:
        body = await request.json()
        if not self.turn_active:
            return web.json_response({"error": "no active turn"}, status=409)
        self.steers.append(body["text"])
        return web.json_response({"ok": True})

    async def _interrupt(self, request: web.Request) -> web.Response:
        self.interrupts += 1
        if self.hold is not None:
            self.hold.set()  # type: ignore[attr-defined]
        return web.json_response({"ok": True})


@dataclass
class Harness:
    substrate: SandboxSubstrate
    kernel: Kernel
    sink: FakeSink
    runner: FakeRunner
    config: WorkerConfig
    async_redis: AsyncRedis


@pytest.fixture
def make_harness(
    names: dict[str, str], sync_redis: redis.Redis
) -> Callable[..., contextlib.AbstractAsyncContextManager[Harness]]:
    """A factory the tests call inside their asyncio.run body: ``make_harness()``.

    Closes over the per-test names and the sync Valkey client so tests need no
    conftest import (which importlib mode makes fragile)."""

    def factory(**overrides: object) -> contextlib.AbstractAsyncContextManager[Harness]:
        return kernel_harness(names, sync_redis, **overrides)

    return factory


@contextlib.asynccontextmanager
async def kernel_harness(
    names: dict[str, str], sync_redis: redis.Redis, **config_overrides: object
) -> AsyncIterator[Harness]:
    """Assemble a live kernel wired to a fake runner and real Valkey."""
    config = make_config(names, **config_overrides)
    fake_runner = FakeRunner()
    server = TestServer(fake_runner.app)
    await server.start_server()
    port = server.port
    assert port is not None

    fake_k8s = FakeK8s()
    substrate = SandboxSubstrate(
        fake_k8s,  # type: ignore[arg-type]
        AffinityStore(sync_redis, key_prefix=names["sandbox_prefix"]),
        SubstrateConfig(
            namespace="test-ns",
            warm_pool="test-pool",
            runner_port=port,
            route_ttl_seconds=60,
            claim_timeout_seconds=3.0,
            poll_interval_seconds=0.005,
            key_prefix=names["sandbox_prefix"],
        ),
    )
    async_redis: AsyncRedis = AsyncRedis(
        host=_VALKEY_HOST, port=_VALKEY_PORT, password=_VALKEY_PW or None, decode_responses=True
    )
    sink = FakeSink()
    runner_client = RunnerClient(total_timeout_s=30.0)
    kernel = Kernel(
        substrate=substrate,
        runner=runner_client,
        sink=sink,
        lock=ThreadLock(
            async_redis,
            ttl_ms=config.lock_ttl_ms,
            acquire_timeout_s=config.lock_acquire_timeout_s,
            poll_interval_s=config.lock_poll_interval_s,
        ),
        markers=Markers(async_redis, config),
        config=config,
    )
    try:
        yield Harness(substrate, kernel, sink, fake_runner, config, async_redis)
    finally:
        with contextlib.suppress(Exception):
            await runner_client.close()
        with contextlib.suppress(Exception):
            await async_redis.aclose()
        with contextlib.suppress(Exception):
            await server.close()
