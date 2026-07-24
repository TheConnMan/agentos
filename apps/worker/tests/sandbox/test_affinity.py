"""AffinityStore behavior against the real compose-stack Valkey."""

from __future__ import annotations

import redis
from curie_worker.sandbox import AffinityStore, RouteRecord, RouteState, SandboxHandle


def _handle(thread: str = "T1", claim: str = "claim-a") -> SandboxHandle:
    return SandboxHandle(
        thread_key=thread,
        claim_name=claim,
        sandbox_name=f"sbx-{claim}",
        namespace="ns",
        service_fqdn=f"sbx-{claim}.ns.svc.cluster.local",
        port=8080,
        session_id="sess-1",
    )


def test_round_trip_and_ttl(affinity: AffinityStore, redis_client: redis.Redis) -> None:
    record = RouteRecord(handle=_handle())
    assert affinity.put_if_absent("T1", record, ttl_seconds=60)

    loaded = affinity.get("T1")
    assert loaded is not None
    assert loaded.handle == record.handle
    assert loaded.state is RouteState.LIVE
    assert affinity.touch("T1", ttl_seconds=90)
    assert affinity.get("missing") is None
    assert not affinity.touch("missing", ttl_seconds=90)


def test_put_if_absent_loses_race(affinity: AffinityStore) -> None:
    first = RouteRecord(handle=_handle(claim="claim-a"))
    second = RouteRecord(handle=_handle(claim="claim-b"))
    assert affinity.put_if_absent("T1", first, ttl_seconds=60)
    assert not affinity.put_if_absent("T1", second, ttl_seconds=60)

    loaded = affinity.get("T1")
    assert loaded is not None
    assert loaded.handle.claim_name == "claim-a"


def test_delete_if_claim_guards_against_stale_releaser(affinity: AffinityStore) -> None:
    affinity.put_if_absent("T1", RouteRecord(handle=_handle(claim="claim-a")), ttl_seconds=60)

    # A stale releaser holding the wrong claim name must not delete the route.
    assert not affinity.delete_if_claim("T1", "claim-stale")
    assert affinity.get("T1") is not None

    assert affinity.delete_if_claim("T1", "claim-a")
    assert affinity.get("T1") is None
    # Second delete is a no-op, not an error.
    assert not affinity.delete_if_claim("T1", "claim-a")


def test_mark_suspended_records_history_ref(affinity: AffinityStore) -> None:
    affinity.put_if_absent("T1", RouteRecord(handle=_handle()), ttl_seconds=60)

    updated = affinity.mark_suspended("T1", "sdk-session-123", ttl_seconds=120)
    assert updated.state is RouteState.SUSPENDED
    assert updated.handle.history_ref == "sdk-session-123"

    loaded = affinity.get("T1")
    assert loaded is not None
    assert loaded.state is RouteState.SUSPENDED
    assert loaded.handle.history_ref == "sdk-session-123"


def test_live_claim_names_skips_expired_routes(affinity: AffinityStore) -> None:
    affinity.put_if_absent("T1", RouteRecord(handle=_handle("T1", "claim-a")), ttl_seconds=60)
    affinity.put_if_absent("T2", RouteRecord(handle=_handle("T2", "claim-b")), ttl_seconds=60)
    assert affinity.live_claim_names() == {"claim-a", "claim-b"}

    affinity.delete_if_claim("T2", "claim-b")
    assert affinity.live_claim_names() == {"claim-a"}
