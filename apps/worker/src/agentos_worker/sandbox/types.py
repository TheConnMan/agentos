"""Sandbox substrate types: the claim handle, route record, config, and errors.

The substrate is the G1 seam between the worker kernel (F1) and the
kubernetes-sigs/agent-sandbox runtime. F1 talks in ``thread_key`` (the Slack
``thread_ts``, or any stable per-conversation key) and receives a
``SandboxHandle`` naming the claimed sandbox and its dial target. Everything
Kubernetes-shaped stays behind this module.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum


class RouteState(StrEnum):
    """Lifecycle state of a thread route recorded in the affinity store."""

    LIVE = "live"
    SUSPENDED = "suspended"


@dataclass(frozen=True)
class SandboxHandle:
    """A claimed sandbox bound to a thread: identity plus the ACI dial target.

    ``base_url`` is only resolvable from inside the cluster (the FQDN is a
    headless-Service DNS name); out-of-cluster callers (tests) reach the runner
    via a port-forward instead.
    """

    thread_key: str
    claim_name: str
    sandbox_name: str
    namespace: str
    service_fqdn: str
    port: int
    session_id: str
    history_ref: str | None = None

    @property
    def sandbox_id(self) -> str:
        """The stable sandbox identity (the Sandbox resource name)."""

        return self.sandbox_name

    @property
    def base_url(self) -> str:
        return f"http://{self.service_fqdn}:{self.port}"


@dataclass
class RouteRecord:
    """The affinity-store value for one thread: handle fields plus route state."""

    handle: SandboxHandle
    state: RouteState = RouteState.LIVE

    def to_json(self) -> str:
        payload = asdict(self.handle)
        payload["state"] = self.state.value
        return json.dumps(payload, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> RouteRecord:
        payload = json.loads(raw)
        state = RouteState(payload.pop("state", RouteState.LIVE.value))
        return cls(handle=SandboxHandle(**payload), state=state)


@dataclass(frozen=True)
class SubstrateConfig:
    """Tunables for the substrate; defaults match the dev chart profile."""

    namespace: str
    warm_pool: str
    runner_port: int = 8080
    # How long a live route stays bound with no touch. After expiry the claim
    # is an orphan and reap_orphans() deletes it.
    route_ttl_seconds: int = 3600
    # Suspended routes wait longer: the thread may come back tomorrow.
    suspended_route_ttl_seconds: int = 86400
    claim_timeout_seconds: float = 30.0
    poll_interval_seconds: float = 0.05
    key_prefix: str = "agentos:sandbox"
    claim_prefix: str = "agentos-thread"

    def claim_name_for(self, thread_key: str, nonce: str) -> str:
        """A DNS-safe, per-generation claim name for a thread.

        The thread hash keeps names stable-per-thread for observability; the
        nonce distinguishes generations (a resume creates a new claim for the
        same thread).
        """

        digest = hashlib.sha256(thread_key.encode("utf-8")).hexdigest()[:10]
        return f"{self.claim_prefix}-{digest}-{nonce}"


@dataclass(frozen=True)
class ClaimView:
    """What the substrate needs to know about a SandboxClaim."""

    name: str
    ready: bool
    sandbox_name: str | None
    labels: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SandboxView:
    """What the substrate needs to know about a Sandbox."""

    name: str
    ready: bool
    service_fqdn: str | None
    operating_mode: str


class SandboxError(Exception):
    """Base error for the sandbox substrate."""


class ClaimTimeoutError(SandboxError):
    """The claim did not bind a ready sandbox within the configured timeout."""


class NoRouteError(SandboxError):
    """An operation needed an existing thread route and none was found."""


class SuspendedThreadError(SandboxError):
    """claim() was called on a suspended thread; the kernel must resume()
    explicitly so the stored history is carried into the replacement runner
    instead of silently forking a fresh, history-less session."""
