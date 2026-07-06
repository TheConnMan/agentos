# 2. Kubernetes Agent Sandbox as the interactive runtime substrate

Date: 2026-07-04
Status: Accepted

## Context

Interactive Slack threads need a runtime that gives each thread a stable identity, an isolated place to run untrusted plugin code, a way for the worker to reach the running agent, and fast allocation. The options were: build our own long-lived runner pool, use a managed cloud runtime (Bedrock AgentCore / Vertex / Foundry — all cloud-locked), or adopt `kubernetes-sigs/agent-sandbox`. Only a portable K8s substrate supports the on-prem / leave-behind tier, which is the product's differentiator.

`agent-sandbox` is pre-1.0 (v0.5.0, no maturity label), so adopting it was a risk that had to be tested, not assumed.

## Decision

Adopt Kubernetes Agent Sandbox as the interactive runtime substrate. The worker claims a `Sandbox` from a `SandboxWarmPool`, records `thread_ts → sandbox_id`, and dials the runner at the sandbox's `.status.serviceFQDN`. Batch/eval runs use plain K8s Jobs. Keep the ACI abstraction (ADR 0005) and a plain-K8s-Job fallback so the substrate is replaceable if the project breaks API.

## Evidence (live, scratch k3s cluster, 2026-07-04)

- `Sandbox` + `spec.service: true` populates `.status.serviceFQDN` and auto-creates a headless Service; a probe pod reached the runner through it.
- `SandboxWarmPool` (replicas:2) pre-warmed sandboxes; a `SandboxClaim` bound to a pre-warmed one in **0.199s** (claim Ready timestamp precedes claim creation), pool self-replenished to readyReplicas:2.
- The controller installed cleanly (1/1, no cert-manager/webhook errors) on stock k3s. The `SandboxTemplate`/`SandboxWarmPool`/`SandboxClaim` extensions are a separate `extensions.yaml` install (not in the core manifest).

## Consequences

- The interactive path depends on a pre-1.0 dependency; the ACI abstraction + Job fallback bound that risk.
- Warm-pool is the one extension surface to keep watching across upgrades.
- Cloud-managed runtimes are explicitly not used for the interactive tier (they cannot leave behind on-prem). They remain an option for a hosted-only tier later.
- See ADR 0003 for the hibernation consequence that this substrate forced.
