# 28. The sandbox substrate is a resilience-only fallback, not a product swap axis

Date: 2026-07-13

Status: Accepted

Records the decision epic [#285](https://github.com/curie-eng/curie/issues/285)
asks for (part of the substrate-seam vision epic
[#86](https://github.com/curie-eng/curie/issues/86), cross-referenced from the
multi-dev epic [#44](https://github.com/curie-eng/curie/issues/44)). This is a
**decision, not code**: it settles whether substrate portability is ever surfaced
to users as a product feature.

## Context

The sandbox substrate is where a conversation thread claims, dials, suspends, and
reaps its isolated runner runtime. The worker talks to a clean `SandboxClient`
`Protocol` (`apps/worker/src/curie_worker/sandbox/k8s.py:50`) with **two real
implementations already proving the port**:

- `KubernetesSandboxClient` (`sandbox/k8s.py:101`) — the production path, driving
  the pre-1.0 `kubernetes-sigs/agent-sandbox` CRDs.
- `DockerSandboxClient` (`sandbox/docker.py:94`) — local runner containers
  ("middle mode": a laptop, no cluster).

The implementation is selected by a SOFT boot switch (`CURIE_SANDBOX_SUBSTRATE`,
`apps/worker/src/curie_worker/run.py:81`). This is the one infra seam whose
interface is already *taught* by a genuine second implementation, so its
`INTERFACE.md` grades it CLEAN
(`docs/interfaces/substrate/INTERFACE.md`).

That cleanliness creates a standing temptation: because the port is real, it looks
like a natural place to advertise "bring your own substrate" — Nomad, Fly
Machines, Firecracker, a managed cloud container runtime (Bedrock AgentCore,
Vertex, Foundry). The question this ADR closes is whether substrate choice ever
becomes a **marketed product axis**, or stays an **internal resilience fallback**.

Prior decisions frame, but do not by themselves answer, this:

- **ADR-0002** adopted Agent Sandbox *and deliberately kept a plain-K8s-Job /
  portable-container fallback* to bound the risk of depending on a pre-1.0 CRD.
  It also states cloud-managed runtimes are explicitly out for the interactive
  tier because they cannot leave behind an on-prem deployment (the product's
  differentiator).
- **ADR-0007** (adopt-not-build): the interactive runner substrate is adopted,
  not built; hand-rolling substrates is a design error. "The single leave-behind
  path is the portable container; managed cloud runtimes are cloud-locked and out
  for the interactive tier."
- **ADR-0016** (swappable jobs around an opinionated core): a seam is promoted
  from convention to a marketed, frozen contract **only when a real swap demand
  arrives**, not speculatively. The second implementation teaches the interface;
  building more ahead of demand is negative work.
- **#86's deliberate stance**: keep the `SandboxClient` port and the Docker peer
  genuinely maintained so the platform never hard-requires the beta CRD path, but
  do not build additional substrates speculatively — a third impl lands only on
  real demand.

## Decision

**The sandbox substrate stays a resilience-only fallback and an internal
portability seam. It is NOT surfaced to users as a product "choose your
substrate" axis.** Substrate choice is an operational/deployment detail
(Kubernetes in production, Docker for local middle mode), not a marketed feature,
a pricing lever, or a documented "supported substrates" matrix customers pick
from.

Concretely:

1. **Two implementations, maintained as core resilience — not a marketed matrix.**
   Both `KubernetesSandboxClient` and `DockerSandboxClient` stay genuinely
   maintained and tested. The Docker peer exists so the platform never
   *hard-requires* the pre-1.0 agent-sandbox CRD (the ADR-0002 bound), and so
   local development needs no cluster. That is the whole point of the second
   impl; it is not a promise of an open substrate ecosystem.

2. **No third substrate is built speculatively.** Nomad, Fly Machines,
   Firecracker, and cloud-managed runtimes are **not** built ahead of a concrete,
   funded customer requirement. Per ADR-0016, a third `SandboxClient` lands only
   when a real swap demand arrives — at which point the existing clean port is
   what makes it cheap. Cloud-managed runtimes additionally remain out for the
   *interactive* tier specifically because they foreclose the on-prem
   leave-behind story (ADR-0002/0007); they stay a possibility only for a future
   hosted-only tier, which would be its own decision.

3. **The seam is not frozen into a public contract.** `SandboxClient` stays a
   convention-plus-review boundary (the ADR-0016 discipline), documented by its
   `INTERFACE.md` black line. Only `aci-protocol` and `plugin-format` are the
   CI-frozen contracts; the substrate port is not promoted to that tier by this
   decision. Freezing it is a *future* step gated on a real second-vendor demand,
   not on the port merely being clean.

4. **`CURIE_SANDBOX_SUBSTRATE` stays an internal/operator switch, not a
   user-facing product setting.** It selects k8s-vs-docker for a deployment; it is
   not documented or billed as "pick your runtime."

### Recommendation (the decision, stated plainly)

**Substrate portability is resilience, not a product.** Keep the port clean and
both peers alive because that is cheap insurance against a beta CRD and the cost
of local dev — but market Curie's differentiator as the *verification/eval
layer and the leave-behind portable deployment*, not as a bring-your-own-substrate
platform. If and when a customer genuinely needs a third substrate, the clean
`SandboxClient` port makes adding it a bounded piece of work; do it then, freeze
the contract then, and revisit this ADR with a superseding one.

## Alternatives considered

- **Market substrate choice as a product swap axis now** (publish a supported-
  substrates matrix; build a Nomad or Firecracker impl to prove it). *Rejected:*
  premature abstraction (ADR-0016) — the interface would encode guesses about a
  second vendor that does not exist, and the team would spend its budget
  maintaining speculative substrates instead of the eval/verification layer that
  is the actual differentiator (ADR-0007). It also invites cloud-locked
  interactive runtimes that break the on-prem leave-behind story (ADR-0002).

- **Collapse to a single substrate** (drop the Docker peer, hard-require the
  agent-sandbox CRD). *Rejected:* re-couples the platform to a pre-1.0 dependency
  with no fallback (undoing the ADR-0002 risk bound) and destroys the
  zero-cluster local-dev path. The second impl earns its keep as resilience even
  though it is not a marketed feature.

- **Freeze `SandboxClient` into a public, CI-gated contract now** so third
  parties can implement substrates. *Rejected for now:* per ADR-0016 a seam is
  frozen when a real swap demand arrives, not because it looks clean. Freezing it
  speculatively adds a maintenance obligation (drift gates, compat guarantees) for
  a consumer that does not exist. Left as an explicitly gated future step.

## Consequences

- The substrate `INTERFACE.md` keeps its "core-with-fallback, not a marketed
  swap" framing; this ADR is the decision that framing was waiting on. A pointer
  to this ADR is added to that INTERFACE's cross-links.
- Epic #86 is answered: the sandbox seam is **resilience-only**, consistent with
  the meta-rule in ADR-0016. #86 can close (or narrow to "watch agent-sandbox
  toward 1.0") rather than spawning speculative-substrate work.
- The multi-dev epic #44 (and its design pass) inherits a settled substrate
  stance: multi-dev isolation and targeting are solved *within* the existing
  Kubernetes substrate (namespaces, RBAC, release/context targeting), not by
  introducing new substrates. This ADR is cross-referenced from #44 per #285's
  acceptance.
- A PR that adds a third substrate, or markets substrate choice as a product
  feature, ahead of a concrete customer requirement is violating this ADR — the
  same way ADR-0016 flags a speculative abstraction layer.
- Revisiting is expected, not forbidden: a genuine second-vendor demand triggers
  a superseding ADR that promotes the port to a frozen contract and adds the
  third impl. The clean port is what keeps that future cheap.
