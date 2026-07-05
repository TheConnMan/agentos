"""Fixtures for the sandbox substrate tests.

Affinity tests run against the REAL Valkey from the compose stack (repo test
discipline: never mock Valkey). The Kubernetes control plane is an external
service, so substrate-logic tests use ``FakeSandboxClient`` (an in-memory model
of the agent-sandbox claim/pool behavior observed in PT-1/PT-D); the real
client is exercised by the env-gated k8scratch e2e in ``test_e2e_k8scratch.py``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
import redis
from agentos_worker.sandbox import (
    AffinityStore,
    ClaimView,
    SandboxView,
    SubstrateConfig,
)

_VALKEY_HOST = os.environ.get("TEST_VALKEY_HOST", "localhost")
_VALKEY_PORT = int(os.environ.get("TEST_VALKEY_PORT", "56379"))
_VALKEY_PW = os.environ.get("TEST_VALKEY_PW", "valkeypass")


@pytest.fixture
def redis_client() -> Iterator[redis.Redis]:
    client = redis.Redis(
        host=_VALKEY_HOST,
        port=_VALKEY_PORT,
        password=_VALKEY_PW or None,
        decode_responses=False,
    )
    try:
        client.ping()
    except redis.exceptions.RedisError as exc:
        pytest.skip(f"Valkey not reachable at {_VALKEY_HOST}:{_VALKEY_PORT}: {exc}")
    yield client
    client.close()


@pytest.fixture
def key_prefix(redis_client: redis.Redis) -> Iterator[str]:
    """Per-test-unique key prefix on the shared Valkey, cleaned up after."""

    prefix = f"test:agentos:sandbox:{uuid.uuid4().hex}"
    yield prefix
    keys = list(redis_client.scan_iter(match=f"{prefix}:*"))
    if keys:
        redis_client.delete(*keys)


@pytest.fixture
def affinity(redis_client: redis.Redis, key_prefix: str) -> AffinityStore:
    return AffinityStore(redis_client, key_prefix=key_prefix)


@pytest.fixture
def config(key_prefix: str) -> SubstrateConfig:
    return SubstrateConfig(
        namespace="test-ns",
        warm_pool="test-pool",
        route_ttl_seconds=60,
        suspended_route_ttl_seconds=120,
        claim_timeout_seconds=2.0,
        poll_interval_seconds=0.005,
        key_prefix=key_prefix,
    )


@dataclass
class FakeClaim:
    name: str
    env: dict[str, str]
    labels: dict[str, str]
    sandbox_name: str
    ready: bool = True


@dataclass
class FakeSandbox:
    name: str
    service_fqdn: str
    operating_mode: str = "Running"
    ready: bool = True


@dataclass
class FakeSandboxClient:
    """In-memory model of the agent-sandbox extensions behavior:

    a created claim binds a sandbox immediately (warm pool), the sandbox gets a
    headless-service FQDN, deleting the claim deletes its sandbox, and
    suspending flips operatingMode (the pod deletion itself is a cluster-side
    effect the substrate never reads back).
    """

    namespace: str = "test-ns"
    claims: dict[str, FakeClaim] = field(default_factory=dict)
    sandboxes: dict[str, FakeSandbox] = field(default_factory=dict)
    bind_ready: bool = True
    created: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def create_claim(
        self,
        name: str,
        *,
        pool: str,
        env: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> None:
        sandbox_name = f"sbx-{name}"
        self.claims[name] = FakeClaim(
            name=name,
            env=dict(env or {}),
            labels={"agentos.dev/managed-by": "agentos-sandbox-substrate", **(labels or {})},
            sandbox_name=sandbox_name,
            ready=self.bind_ready,
        )
        self.sandboxes[sandbox_name] = FakeSandbox(
            name=sandbox_name,
            service_fqdn=f"{sandbox_name}.{self.namespace}.svc.cluster.local",
        )
        self.created.append(name)

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
        self.deleted.append(name)

    def list_claims(self, *, label_selector: str) -> list[ClaimView]:
        key, _, value = label_selector.partition("=")
        views = []
        for claim in self.claims.values():
            if claim.labels.get(key) == value:
                view = self.get_claim(claim.name)
                assert view is not None
                views.append(view)
        return views

    def get_sandbox(self, name: str) -> SandboxView | None:
        sandbox = self.sandboxes.get(name)
        if sandbox is None:
            return None
        return SandboxView(
            name=sandbox.name,
            ready=sandbox.ready,
            service_fqdn=sandbox.service_fqdn,
            operating_mode=sandbox.operating_mode,
        )

    def set_sandbox_mode(self, name: str, mode: str) -> None:
        self.sandboxes[name].operating_mode = mode


@pytest.fixture
def fake_k8s() -> FakeSandboxClient:
    return FakeSandboxClient()
