"""The sandbox lifecycle substrate: claim, route, suspend/resume, reap.

The one-live-session-per-thread routing rule (detailed-architecture section 2)
is implemented here as: the affinity store maps ``thread_key`` to exactly one
claim; ``claim()`` returns the existing live binding or creates one from the
warm pool; a lost creation race is resolved by deleting the loser's claim and
adopting the winner's. F1 (the worker kernel) composes these primitives; it
never touches Kubernetes directly.

Cold-restart rehydrate design (ADR-0003, PT-1 finding: suspend/resume is a cold
pod restart, the live process never survives): ``suspend()`` records the
caller-supplied history ref on the route; ``resume()`` retires the suspended
claim and creates a NEW claim whose per-claim env injects
``AGENTOS_HISTORY_REF`` (and the original ``AGENTOS_SESSION_ID``), so the
replacement runner boots rehydrating from stored history rather than assuming
process or cache warmth. The runner accepts the ref as an SDK resume id
(runner/config.py); producing the ref (an SDK session id) is the caller's job.
"""

from __future__ import annotations

import time
import uuid

from .affinity import AffinityStore
from .k8s import (
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    THREAD_HASH_LABEL,
    SandboxClient,
)
from .types import (
    ClaimTimeoutError,
    NoRouteError,
    RouteRecord,
    RouteState,
    SandboxHandle,
    SubstrateConfig,
)

HISTORY_ENV = "AGENTOS_HISTORY_REF"
SESSION_ENV = "AGENTOS_SESSION_ID"


class SandboxSubstrate:
    """Provision, route, and reap runner sandboxes for conversation threads."""

    def __init__(
        self,
        k8s: SandboxClient,
        affinity: AffinityStore,
        config: SubstrateConfig,
    ) -> None:
        self._k8s = k8s
        self._affinity = affinity
        self._config = config

    # -- claim / lookup -------------------------------------------------------

    def claim(self, thread_key: str, *, env: dict[str, str] | None = None) -> SandboxHandle:
        """Return the thread's live sandbox, claiming a warm one if needed.

        ``env`` is per-claim env injection (the resume path uses it for the
        history ref); the fast path passes none so the claim binds a pre-warmed
        generic sandbox.
        """

        existing = self.lookup(thread_key)
        if existing is not None:
            self._affinity.touch(thread_key, self._config.route_ttl_seconds)
            return existing

        return self._claim_fresh(thread_key, env=env, state=RouteState.LIVE)

    def lookup(self, thread_key: str) -> SandboxHandle | None:
        """The thread's live handle, or None (no route, suspended, or the
        cluster-side sandbox is gone/not ready)."""

        record = self._affinity.get(thread_key)
        if record is None or record.state is not RouteState.LIVE:
            return None
        sandbox = self._k8s.get_sandbox(record.handle.sandbox_name)
        if sandbox is None or sandbox.operating_mode != "Running":
            return None
        return record.handle

    # -- suspend / resume -------------------------------------------------------

    def suspend(self, thread_key: str, *, history_ref: str | None) -> None:
        """Suspend the thread's sandbox and record the rehydrate ref.

        The pod is deleted by the controller (PT-1: suspend is pod deletion);
        the route flips to SUSPENDED with the longer TTL so ``resume()`` can
        rebuild session state later.
        """

        record = self._affinity.get(thread_key)
        if record is None:
            raise NoRouteError(thread_key)
        self._k8s.set_sandbox_mode(record.handle.sandbox_name, "Suspended")
        self._affinity.mark_suspended(
            thread_key, history_ref, self._config.suspended_route_ttl_seconds
        )

    def resume(self, thread_key: str) -> SandboxHandle:
        """Rehydrate a suspended thread into a fresh claim.

        The suspended claim is retired (its process and cache are gone either
        way) and a new claim is created with ``AGENTOS_HISTORY_REF`` injected,
        so the replacement runner boots resuming from stored history.
        """

        record = self._affinity.get(thread_key)
        if record is None:
            raise NoRouteError(thread_key)
        old = record.handle
        env = {SESSION_ENV: old.session_id}
        if old.history_ref is not None:
            env[HISTORY_ENV] = old.history_ref

        self._k8s.delete_claim(old.claim_name)
        self._affinity.delete_if_claim(thread_key, old.claim_name)
        return self._claim_fresh(
            thread_key,
            env=env,
            state=RouteState.LIVE,
            session_id=old.session_id,
            history_ref=old.history_ref,
        )

    # -- release / reap -------------------------------------------------------

    def release(self, thread_key: str) -> bool:
        """End the thread's session: delete the claim (the claim's lifecycle
        deletes its sandbox and pod) and drop the route. True if a route
        existed."""

        record = self._affinity.get(thread_key)
        if record is None:
            return False
        self._k8s.delete_claim(record.handle.claim_name)
        self._affinity.delete_if_claim(thread_key, record.handle.claim_name)
        return True

    def reap_orphans(self) -> list[str]:
        """Delete substrate-managed claims that no live route references.

        Routes expire from Valkey by TTL (idle threads); the corresponding
        claims are then orphans on the cluster. Runs from a periodic worker
        tick. Returns the deleted claim names.
        """

        live = self._affinity.live_claim_names()
        deleted: list[str] = []
        selector = f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE}"
        for claim in self._k8s.list_claims(label_selector=selector):
            if claim.name not in live:
                self._k8s.delete_claim(claim.name)
                deleted.append(claim.name)
        return deleted

    # -- internals --------------------------------------------------------------

    def _claim_fresh(
        self,
        thread_key: str,
        *,
        env: dict[str, str] | None,
        state: RouteState,
        session_id: str | None = None,
        history_ref: str | None = None,
    ) -> SandboxHandle:
        config = self._config
        nonce = uuid.uuid4().hex[:6]
        name = config.claim_name_for(thread_key, nonce)
        thread_hash = name.rsplit("-", 1)[0].rsplit("-", 1)[-1]

        self._k8s.create_claim(
            name,
            pool=config.warm_pool,
            env=env,
            labels={THREAD_HASH_LABEL: thread_hash},
        )
        try:
            sandbox_name = self._await_bound(name)
            fqdn = self._await_service_fqdn(sandbox_name)
        except Exception:
            self._k8s.delete_claim(name)
            raise

        handle = SandboxHandle(
            thread_key=thread_key,
            claim_name=name,
            sandbox_name=sandbox_name,
            namespace=config.namespace,
            service_fqdn=fqdn,
            port=config.runner_port,
            session_id=session_id or f"thread-{thread_hash}",
            history_ref=history_ref,
        )
        record = RouteRecord(handle=handle, state=state)
        if not self._affinity.put_if_absent(thread_key, record, config.route_ttl_seconds):
            # Lost the race: another worker bound this thread first. Adopt the
            # winner and retire our claim.
            self._k8s.delete_claim(name)
            winner = self._affinity.get(thread_key)
            if winner is not None and winner.state is RouteState.LIVE:
                return winner.handle
            raise NoRouteError(f"route for {thread_key} vanished during claim race")
        return handle

    def _await_bound(self, claim_name: str) -> str:
        deadline = time.monotonic() + self._config.claim_timeout_seconds
        while time.monotonic() < deadline:
            claim = self._k8s.get_claim(claim_name)
            if claim is not None and claim.ready and claim.sandbox_name:
                return claim.sandbox_name
            time.sleep(self._config.poll_interval_seconds)
        raise ClaimTimeoutError(
            f"claim {claim_name} not bound within {self._config.claim_timeout_seconds}s"
        )

    def _await_service_fqdn(self, sandbox_name: str) -> str:
        deadline = time.monotonic() + self._config.claim_timeout_seconds
        while time.monotonic() < deadline:
            sandbox = self._k8s.get_sandbox(sandbox_name)
            if sandbox is not None and sandbox.service_fqdn:
                return sandbox.service_fqdn
            time.sleep(self._config.poll_interval_seconds)
        raise ClaimTimeoutError(
            f"sandbox {sandbox_name} has no serviceFQDN within "
            f"{self._config.claim_timeout_seconds}s (is spec.service true in the template?)"
        )
