# 59. The sandbox is a bounded resource envelope; capacity is a tenant boundary

Date: 2026-07-21

Status: Accepted

Extends [ADR-0006](0006-security-rails-as-chart-defaults.md) (security rails are
chart defaults) and [ADR-0008](0008-multi-tenancy.md) (namespace-per-tenant
compute) with the dimension neither of them covers: **availability**. Those ADRs
establish what a sandbox may *reach*; this one establishes what a sandbox may
*consume*. It does not change the confidentiality or integrity boundary.

## Context

Curie runs a bundle as arbitrary code inside a Kubernetes sandbox. ADR-0006
ships the isolation rails as chart defaults (gVisor, default-deny egress,
non-root, read-only rootfs), and ADR-0008 decides that compute stays hard-siloed
at namespace-per-tenant because "executing untrusted, prompt-injectable code that
holds customer credentials is a different risk class than pooling rows."

Both of those are *reachability* boundaries. Neither bounds consumption, and the
shipped chart bounds consumption only partially:

- The runner declares `cpu` and `memory` requests/limits
  ([`charts/curie/values.yaml`](../../charts/curie/values.yaml), `runner.resources`)
  and **no `ephemeral-storage` request or limit**. No `resources` block anywhere
  in the chart sets one, on any container.
- Every writable volume in the sandbox pod is an unbounded `emptyDir` with **no
  `sizeLimit`**: the shared `bundles` volume, the init-only `mc-config` volume,
  and one volume per `hardening.writablePaths` entry (`/tmp` and the runner's
  `$HOME` by default), in
  [`charts/curie/templates/agent-sandbox.yaml`](../../charts/curie/templates/agent-sandbox.yaml).
  The `SandboxTemplate` CRD supports `sizeLimit`; the chart never sets it.
- Bundle ingestion is unbounded on both ends. The upload endpoint reads the whole
  body into memory with no size gate
  ([`apps/api/src/curie_api/routers/bundles.py`](../../apps/api/src/curie_api/routers/bundles.py)),
  and `safe_extract`
  ([`packages/plugin-format/src/plugin_format/archive.py`](../../packages/plugin-format/src/plugin_format/archive.py))
  rejects traversing, link, and special-file entries but enforces **no size and
  no compression-ratio limit**. The only size cap in the system bounds the GitHub
  webhook body, not this path and not the extracted footprint.
- There is **no `ResourceQuota`, no `LimitRange`, and no `PriorityClass`** in the
  chart. ADR-0008's tenant namespace is therefore a reachability boundary with no
  capacity boundary inside it.

The consequence is that disk is the one resource dimension with no ceiling at
all. Because there is no per-pod `ephemeral-storage` limit, the kubelet cannot
cap or evict the offending pod on its own account; its only backstop is
**node-level `DiskPressure` eviction, which ranks victims across the whole node
by whether usage exceeds requests, then by pod priority, then by usage relative
to requests.** (QoS class is not the ranking criterion for disk, and does not
apply to `ephemeral-storage` at all.) Since no pod in the chart declares an
`ephemeral-storage` request, every request is zero and every priority is equal,
so that ranking degenerates to raw usage.

A single sandbox that fills a node's disk therefore degrades every co-scheduled
pod before the kubelet reacts: writes fail node-wide, the node is tainted
`node.kubernetes.io/disk-pressure` so nothing new schedules on it (warm-pool
refill and other tenants' claims included), and eviction, when it comes, is late,
ranked by usage and priority rather than by fault, and can land on a pod that did
nothing wrong. Since sandbox pods carry no `priorityClassName`, platform pods on
that node are ranked on equal footing with the sandboxes they are supposed to be
supervising.

This is not a scale problem that arrives at some user count. It arrives at **one
disk-heavy agent**: an agent that clones a large repository, installs a
dependency tree, downloads a model, or generates a large artifact is doing
nothing abusive and nothing the product discourages, and can exhaust a modest
node disk within a single turn. Nothing reclaims that space mid-run either:
`emptyDir` is freed only when the pod is deleted, and a bound sandbox is reused
for its thread for up to an hour of idleness. Usage is monotonic for the life of
the pod.

Under ADR-0008's hosted, multi-tenant future this stops being a noisy-neighbor
annoyance and becomes a **cross-tenant availability failure**: tenant A's bundle,
staying entirely inside its own namespace and its own egress policy, degrades or
evicts tenant B's sandboxes scheduled on the same node. Namespace isolation does
not prevent it, because nodes are shared beneath the namespace boundary.

## Decision

**Every sandbox declares a bounded envelope on every resource dimension, and the
tenant namespace carries a capacity ceiling as well as a reachability boundary.**
Capacity isolation is treated as a security property of the multi-tenant
boundary, not as a performance-tuning concern deferred to operators.

Decisions 1, 2, 4, and 5 bind the Kubernetes substrate, which is the only
supported boundary for untrusted code. Decision 3 is API-side and therefore
substrate-neutral. The local Docker substrate's disk story is deferred, not
decided: ADR-0054 already bounds worker-spawned runners on memory/cpu/pids and
deliberately leaves resource caps off the interactive `skill up` loop, and its
writable paths are RAM-backed `tmpfs` rather than node disk, so the failure mode
differs. Whether that is sufficient is tracked separately.

1. **No unbounded resource dimension on a sandbox container.** Every container in
   the sandbox pod (the runner, the bundle init containers, and any sidecar)
   declares `ephemeral-storage` requests and limits alongside `cpu` and `memory`.
   A resource dimension left undeclared is a defect, not a default.

2. **Every writable volume declares a `sizeLimit`.** The `bundles`, `mc-config`,
   and `writablePaths` volumes each carry an explicit ceiling, so a pod that
   overruns is evicted on its own account rather than through node
   `DiskPressure`. Enforcement is by periodic kubelet measurement, not a
   write-time cap, so this is a fast backstop that a burst can briefly overshoot;
   the scheduling-time `ephemeral-storage` request from decision 1 is what keeps
   a node from being overcommitted in the first place. The two are complementary
   and both are set.

3. **Bundle ingestion is bounded end to end and fails closed.** The upload path
   enforces a maximum body size before buffering, and extraction enforces both a
   maximum uncompressed size and a maximum compression ratio, refusing the
   archive rather than partially unpacking it. An archive that cannot be bounded
   is rejected; nothing unbounded is written to a node. Bundles already stored
   under the previous unbounded rules are revalidated against the new caps at
   deploy time, so an oversized legacy bundle fails as a clear deploy-time
   rejection rather than an opaque init-container failure or a mid-extract
   eviction on the node.

4. **The tenant namespace is the capacity boundary.** Each tenant namespace
   carries a `ResourceQuota` (aggregate cpu/memory/ephemeral-storage and a
   sandbox pod count) and a `LimitRange` supplying defaults, so a tenant cannot
   exceed its allocation regardless of how many sandboxes it claims, and a
   sandbox created outside the chart's own templates still inherits a ceiling.
   This completes ADR-0008's decision 3: namespace-per-tenant isolates
   reachability *and* capacity. In the self-host N=1 topology the sandboxes share
   the release namespace with the platform pods, so the quota is scoped by
   decision 5's priority class rather than applied namespace-wide; otherwise it
   would bind the control plane and data tier too.

5. **Platform pods outrank sandbox pods.** A `PriorityClass` places the control
   plane (worker, api, dispatcher, data tier) above sandbox pods, so the
   components required to supervise, drain, and reclaim a sandbox are never
   preferred for eviction over the sandboxes themselves. Priority is a ranking
   and preemption lever, not immunity: a node fully out of disk can still take
   anything down.

6. **Every ceiling is operator-overridable, and defaults are generous enough for
   real work.** Following the `RunnerHardening` precedent in
   [ADR-0054](0054-local-docker-runner-hardening.md), each limit is a chart value
   an operator can raise for a heavy trusted bundle without editing code. The
   defaults are chosen to bound a runaway, not to constrain a legitimate builder
   agent; a limit tight enough to break ordinary work would be routinely disabled
   and would protect nothing.

**The capacity invariant** (what we test and review to): no sandbox can consume
an unbounded quantity of any node resource, and no tenant can consume more than
its namespace allocation, regardless of bundle content, archive shape, or turn
duration.

### Out of scope

Two findings from the same review are real but are different decisions, and are
deliberately not settled here:

- **Throughput.** The cluster-wide interactive ceiling is
  `worker.replicas x max_concurrency`, where the concurrency semaphore is
  hardcoded rather than exposed as configuration, and there is no HPA or node
  autoscaler wiring. That is a scaling decision, not an isolation one.
- **Data-tier availability.** Postgres, Valkey, ClickHouse, and MinIO each ship
  as a single unreplicated pod with `PodDisruptionBudget` disabled by default.
  That is an HA decision, and Valkey in particular (locks, routes, and the queue)
  deserves its own record.

Both are recorded here so the reasoning is not lost. Each is tracked as its own
issue linking back to this ADR, and each likely warrants its own decision record;
neither is decided by this one.

## Consequences

- A runaway or malicious bundle is bounded to its own pod's envelope. Disk
  exhaustion evicts the offending sandbox rather than pushing the node into
  `DiskPressure` and degrading innocent neighbors, which makes the failure both
  contained and attributable. Because the ceilings are enforced by periodic
  measurement, a sufficiently fast writer can still overshoot briefly; the
  guarantee is a bounded blast radius, not an instantaneous cap.
- ADR-0008's hosted multi-tenant path gains the availability leg it was missing.
  Cross-tenant blast radius via shared node capacity closes, so namespace
  isolation is no longer undermined by the nodes beneath it.
- Sandbox pods become schedulable on their declared `ephemeral-storage` request,
  which means **node disk becomes a real scheduling dimension**. Operators must
  size node disk for the sandbox density they intend, and an undersized node pool
  will now surface as unschedulable pods rather than as a silent disk fill. This
  is a deliberate trade: a visible scheduling failure beats an invisible
  node-wide one.
- A legitimately disk-heavy agent can now fail on a limit it previously exceeded
  silently. That is the intended behavior, and the override in decision 6 is the
  supported remedy. Choosing defaults that are too tight is the main way this
  decision can go wrong in practice.
- Bundle rejection becomes a user-visible failure mode with its own error path,
  and the size and ratio ceilings become part of the bundle contract that
  publishers build against.
- None of this is a substitute for the ADR-0006 rails. A bounded envelope limits
  what a compromised bundle can *consume*; it does not change what it can
  *reach*. The supported boundary for untrusted code remains the Kubernetes path.
