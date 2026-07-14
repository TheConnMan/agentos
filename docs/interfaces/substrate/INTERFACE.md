# INTERFACE: Substrate / SandboxClient

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
> **Kind:** CLEAN &nbsp;·&nbsp; **Implementations today:** 2 (Kubernetes, Docker) &nbsp;·&nbsp; **Swap-readiness grade:** not separately graded (core substrate)

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

The substrate is where a conversation thread claims, dials, suspends, and reaps an isolated runner runtime. `SandboxSubstrate` composes the port; everything Kubernetes-shaped (or Docker-shaped) lives behind the `SandboxClient` `Protocol`. The kernel talks in `thread_key` and receives a `SandboxHandle` with a dial target — it never touches a cluster or a container runtime directly. The swap axis is which runtime backs a claim (k8s CRDs vs local Docker containers); the routing, affinity, and rehydrate logic above the line stay opinionated core.

## Current contract

A second implementation must satisfy the `SandboxClient` `Protocol` at `apps/worker/src/agentos_worker/sandbox/k8s.py:50`, six methods:

- `create_claim(name, *, pool, env=None, labels=None) -> None` (`k8s.py:53`)
- `get_claim(name) -> ClaimView | None` (`k8s.py:62`)
- `delete_claim(name) -> None` (`k8s.py:64`)
- `list_claims(*, label_selector) -> list[ClaimView]` (`k8s.py:66`)
- `get_sandbox(name) -> SandboxView | None` (`k8s.py:68`)
- `set_sandbox_mode(name, mode: OperatingMode) -> None` (`k8s.py:70`)

The exchanged value types are `ClaimView` (`sandbox/types.py:110`: `name`, `ready`, `sandbox_name`, `labels`) and `SandboxView` (`sandbox/types.py:120`: `name`, `ready`, `service_fqdn`, `operating_mode`, `port`). `operating_mode` must report `"Running"` for a claim to be handed back (`substrate.py:75`), and `OperatingMode` is `Literal["Running", "Suspended"]` (`k8s.py:30`). The selector reads `AGENTOS_SANDBOX_SUBSTRATE` (default `"kubernetes"`, else `"docker"`) in `_sandbox_client()` at `apps/worker/src/agentos_worker/run.py:81` (branch at `run.py:98`).

## Implementations today

Two, both under `apps/worker/src/agentos_worker/sandbox/`:

- `KubernetesSandboxClient` (`k8s.py:101`) — drives agent-sandbox CRDs (`Sandbox` in `agents.x-k8s.io`, `SandboxClaim` in `extensions.agents.x-k8s.io`) via `CustomObjectsApi`.
- `DockerSandboxClient` (`docker.py:94`) — boots runner containers on the local Docker daemon for middle mode (a laptop, no cluster).

## Known leakage

The `SandboxView.port` field (`types.py:134`) exists only because the Docker path publishes each runner on its own loopback host port, while the Kubernetes path uses one fleet-wide `runner_port`; `None` means "fall back to `SubstrateConfig.runner_port`". Credential handling also differs across the line: the k8s client strips `AGENTOS_CREDENTIALS` from per-claim env so it is never persisted in plaintext on the claim (`k8s.py:47`, `k8s.py:139`), relying on the chart Secret's `secretKeyRef`; the Docker client has no Secret and forwards exactly one model credential by name -- an explicit AGENTOS_CREDENTIALS alone (never an ambient CLAUDE_CODE_OAUTH_TOKEN/ANTHROPIC_API_KEY that would shadow it), and none at all for a fake-model or local base-URL-override run that resolves none. These are runtime-shaped asymmetries the `Protocol` does not fully hide.

## Cross-links

- **Epic(s):** #86 — substrate vision (pluggable runtimes beyond agent-sandbox); #44 — substrate hardening
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — core substrate, not separately graded
- **ADR(s):** [ADR-0002](../../adr/0002-kubernetes-agent-sandbox-as-runtime-substrate.md) — Kubernetes Agent Sandbox as the interactive runtime substrate; [ADR-0008](../../adr/0008-multi-tenancy.md) — multi-tenancy: hard-siloed compute (namespace-per-tenant) rides this seam; [ADR-0028](../../adr/0028-substrate-is-resilience-fallback-not-product-swap-axis.md) — the "core-with-fallback, not a marketed swap" stance above is now a recorded decision: substrate portability stays a resilience-only fallback, not a product swap axis (settles #86)
