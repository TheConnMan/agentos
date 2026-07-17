# 54. Local Docker runner containers are hardened and network-isolated

Date: 2026-07-17

Status: Accepted

Implements [#631](https://github.com/curie-eng/agentos/issues/631). Applies, to
the local Docker substrate, the container-level isolation that
[ADR-0006](0006-security-rails-as-chart-defaults.md) ships as Kubernetes chart
defaults and that [#493] hardens for rendered Kubernetes sandbox containers. It
does not change, and is not, the Kubernetes security boundary.

## Context

AgentOS runs a bundle as arbitrary code; the supported security boundary is the
Kubernetes sandbox (gVisor + a default-deny egress NetworkPolicy + per-agent
RBAC), documented in [`SECURITY.md`](../../SECURITY.md) and ADR-0006.

The local developer loop is a second substrate. `agentos local up` runs the
compose stack and the worker spawns runner containers on the host Docker daemon
([`apps/worker/src/agentos_worker/sandbox/docker.py`](../../apps/worker/src/agentos_worker/sandbox/docker.py)),
and `agentos skill up` boots a runner directly
([`cli/src/docker.rs`](../../cli/src/docker.rs)). Before this ADR those runner
containers ran with no container hardening and joined the shared compose network
(`agentos_default`), where a bundle could reach the data tier
(Postgres/Valkey/MinIO) by service name even though the runner never uses those
stores directly. The worker itself is host-root-equivalent by design (it holds
the Docker socket); that is unchanged. The gap was the *runner* containers: they
had avoidable host, daemon-adjacent, and data-tier reach for no functional
reason.

Local mode is scoped to **trusted** bundles, so this is not a boundary for
untrusted code. But "trusted" is not "infallible": a buggy-but-trusted bundle
should not be able to casually escalate on the host or read a store's embedded
credentials off the shared network. That reachability is pure downside.

## Decision

**Every runner container the local substrate spawns is hardened at the container
level and joined to a dedicated, data-tier-free network.** The controls mirror
the K8s runner `securityContext` + `resources` so local and cluster tell the same
isolation story:

- **Read-only root filesystem** with writable `tmpfs` for `/tmp` and the
  non-root runner's `$HOME` only (the emptyDir-equivalent writable paths).
- **All Linux capabilities dropped** (`--cap-drop ALL`).
- **No privilege escalation** (`--security-opt no-new-privileges`), with Docker's
  **default seccomp profile** left active (never `unconfined`).
- **Bounded pids/memory/cpu** on the worker substrate (the untrusted product
  loop): memory and cpu mirror the chart runner limits. The interactive
  `skill up` dev loop applies the filesystem/capability/privilege controls but
  leaves resource caps off, so a developer's heavy local run is not throttled.
- **No Docker socket** in any runner (only the worker mounts it) and **no data
  tier**: runners join a dedicated `agentos_runner` network onto which only the
  runner's documented dependencies are multi-homed -- the OTel collector
  (telemetry), Ollama (`--local-model`), and the API (the `state` endpoint) --
  never Postgres/Valkey/MinIO. A real-model run still gets external egress
  (`agentos_runner` is a normal, non-`internal` bridge).

The worker substrate's controls are expressed as a `RunnerHardening` value
overridable from `AGENTOS_RUNNER_*` env, so an operator can loosen a limit a
heavy trusted bundle needs, or disable the set for debugging, without editing
code.

## Consequences

- A local runner can no longer reach the data tier or the Docker daemon, and
  cannot write outside its tmpfs mounts, drop-in capabilities, or gain privilege
  on exec. Blast radius from a trusted-but-buggy bundle shrinks accordingly.
- Local and Kubernetes now apply the same *shape* of container isolation, so the
  local loop is a more honest rehearsal of the deployed one.
- This remains **defense-in-depth for trusted bundles, not a security boundary
  for untrusted code.** The only supported boundary for untrusted bundles is the
  Kubernetes path. `SECURITY.md` states this explicitly.
- The dedicated network requires the runner's dependencies to be multi-homed onto
  it in `compose.dev.yaml`; a future dependency the runner must reach has to be
  added to `agentos_runner` (the same fail-closed discipline as the chart's
  `allowedEgress`).

[#493]: https://github.com/curie-eng/agentos/issues/493
