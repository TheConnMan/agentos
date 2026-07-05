"""SandboxSubstrate lifecycle logic against real Valkey + the in-memory
cluster fake (the K8s control plane is the one external service faked here;
the real cluster path is the e2e in test_e2e_k8scratch.py)."""

from __future__ import annotations

import pytest
from agentos_worker.sandbox import (
    HISTORY_ENV,
    SESSION_ENV,
    AffinityStore,
    ClaimTimeoutError,
    NoRouteError,
    RouteRecord,
    RouteState,
    SandboxHandle,
    SandboxSubstrate,
    SubstrateConfig,
)

from .conftest import FakeSandboxClient


@pytest.fixture
def substrate(
    fake_k8s: FakeSandboxClient, affinity: AffinityStore, config: SubstrateConfig
) -> SandboxSubstrate:
    return SandboxSubstrate(fake_k8s, affinity, config)


def test_claim_binds_and_routes_thread(
    substrate: SandboxSubstrate, fake_k8s: FakeSandboxClient
) -> None:
    handle = substrate.claim("1700000000.000100")

    assert handle.sandbox_name.startswith("sbx-agentos-thread-")
    assert handle.service_fqdn.endswith(".svc.cluster.local")
    assert handle.base_url == f"http://{handle.service_fqdn}:8080"
    assert fake_k8s.claims[handle.claim_name].env == {}

    # Same thread claims again -> same binding, no second claim created.
    again = substrate.claim("1700000000.000100")
    assert again == handle
    assert len(fake_k8s.created) == 1

    # A different thread gets a different sandbox (no cross-talk).
    other = substrate.claim("1700000000.000999")
    assert other.sandbox_name != handle.sandbox_name


def test_lookup_returns_none_when_sandbox_gone(
    substrate: SandboxSubstrate, fake_k8s: FakeSandboxClient
) -> None:
    handle = substrate.claim("T1")
    assert substrate.lookup("T1") == handle

    # Cluster-side deletion out from under the route (node loss, manual kill).
    fake_k8s.sandboxes.pop(handle.sandbox_name)
    assert substrate.lookup("T1") is None


def test_claim_timeout_cleans_up_claim(
    fake_k8s: FakeSandboxClient, affinity: AffinityStore, config: SubstrateConfig
) -> None:
    fake_k8s.bind_ready = False
    substrate = SandboxSubstrate(fake_k8s, affinity, config)

    with pytest.raises(ClaimTimeoutError):
        substrate.claim("T1")
    # The unbound claim is not leaked and no route was recorded.
    assert fake_k8s.deleted == fake_k8s.created
    assert affinity.get("T1") is None


def test_lost_race_adopts_winner_and_retires_loser(
    substrate: SandboxSubstrate,
    fake_k8s: FakeSandboxClient,
    affinity: AffinityStore,
) -> None:
    # A competing worker recorded a route for T1 between our create and put.
    winner_handle = SandboxHandle(
        thread_key="T1",
        claim_name="claim-winner",
        sandbox_name="sbx-claim-winner",
        namespace="test-ns",
        service_fqdn="sbx-claim-winner.test-ns.svc.cluster.local",
        port=8080,
        session_id="sess-w",
    )
    original_create = fake_k8s.create_claim

    def create_then_lose(name: str, **kwargs: object) -> None:
        original_create(name, **kwargs)  # type: ignore[arg-type]
        affinity.put_if_absent("T1", RouteRecord(handle=winner_handle), ttl_seconds=60)

    fake_k8s.create_claim = create_then_lose  # type: ignore[method-assign]

    handle = substrate.claim("T1")
    assert handle == winner_handle
    # The loser's claim was deleted, not leaked.
    assert len(fake_k8s.deleted) == 1


def test_suspend_resume_rehydrates_from_history(
    substrate: SandboxSubstrate, fake_k8s: FakeSandboxClient, affinity: AffinityStore
) -> None:
    first = substrate.claim("T1")
    substrate.suspend("T1", history_ref="sdk-session-abc")

    # Suspended: mode flipped, route no longer live.
    assert fake_k8s.sandboxes[first.sandbox_name].operating_mode == "Suspended"
    record = affinity.get("T1")
    assert record is not None and record.state is RouteState.SUSPENDED
    assert substrate.lookup("T1") is None
    # A claim() while suspended must not silently fork a second live session
    # for the thread without the history; the kernel resumes explicitly.

    resumed = substrate.resume("T1")
    assert resumed.claim_name != first.claim_name
    assert resumed.session_id == first.session_id
    assert resumed.history_ref == "sdk-session-abc"
    # The new claim injects the rehydrate env for the replacement runner.
    env = fake_k8s.claims[resumed.claim_name].env
    assert env[HISTORY_ENV] == "sdk-session-abc"
    assert env[SESSION_ENV] == first.session_id
    # Old claim retired; route is live again on the new claim.
    assert first.claim_name in fake_k8s.deleted
    assert substrate.lookup("T1") == resumed


def test_suspend_and_resume_require_route(substrate: SandboxSubstrate) -> None:
    with pytest.raises(NoRouteError):
        substrate.suspend("nope", history_ref=None)
    with pytest.raises(NoRouteError):
        substrate.resume("nope")


def test_release_deletes_claim_and_route(
    substrate: SandboxSubstrate, fake_k8s: FakeSandboxClient, affinity: AffinityStore
) -> None:
    handle = substrate.claim("T1")
    assert substrate.release("T1")
    assert handle.claim_name not in fake_k8s.claims
    assert handle.sandbox_name not in fake_k8s.sandboxes
    assert affinity.get("T1") is None
    assert not substrate.release("T1")


def test_reap_orphans_deletes_unrouted_claims(
    substrate: SandboxSubstrate, fake_k8s: FakeSandboxClient, affinity: AffinityStore
) -> None:
    live = substrate.claim("T-live")
    orphan = substrate.claim("T-orphan")
    # The orphan's route expires (simulated by guarded delete), its claim stays.
    affinity.delete_if_claim("T-orphan", orphan.claim_name)

    reaped = substrate.reap_orphans()
    assert reaped == [orphan.claim_name]
    assert orphan.claim_name not in fake_k8s.claims
    assert live.claim_name in fake_k8s.claims
    # Reap is idempotent.
    assert substrate.reap_orphans() == []
