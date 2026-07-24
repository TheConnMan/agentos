# 23. Controller NetworkPolicy RBAC: cluster-scope read, namespace-scope mutate

Date: 2026-07-13
Status: Accepted

## Context

The umbrella chart vendors the upstream `kubernetes-sigs/agent-sandbox` v0.5.0
controller (`registry.k8s.io/agent-sandbox/agent-sandbox-controller:v0.5.0`) as
release manifests under `charts/curie/files/agent-sandbox/controller.yaml`.
The controller reconciles a shared `NetworkPolicy` per `SandboxTemplate`
(`extensions/controllers/sandboxtemplate_controller.go:253`,
`Owns(&networkingv1.NetworkPolicy{})`).

[ADR-less PR #189 / issue #66] hardened this controller: it **removed the
cluster-wide `networkpolicies` grant** from the vendored
`agent-sandbox-controller-extensions` ClusterRole and re-granted it as a
**namespaced** `Role`/`RoleBinding` (`agent-sandbox-controller-networkpolicies`,
`namespace: {{ .Release.Namespace }}`). The security intent is load-bearing: a
compromised controller SA (or leaked token) must not be able to delete the
fail-closed egress `NetworkPolicy` (Rail 1) in any *other* namespace and then
exfiltrate. #66's live check proved `kubectl auth can-i delete networkpolicies`
was `no` cluster-wide and in every namespace but the release namespace.

That hardening broke the controller's ability to start. A `controller-runtime`
manager backs `Owns(&NetworkPolicy{})` with an **informer that LISTs/WATCHes at
cluster scope** (the vendored manager sets no cache namespace scoping;
`cmd/agent-sandbox-controller/main.go:262` calls `ctrl.NewManager` with no
`Cache.DefaultNamespaces`). A namespaced Role cannot satisfy a cluster-scope
LIST, so the NetworkPolicy informer cache never syncs and the manager aborts ->
CrashLoopBackOff -> **no `SandboxClaim` ever binds -> not a single turn runs at
the cluster tier**. Field evidence: cold-start parity-ladder run 2 (v0.3.0
release binary, k3s v1.35). This is invisible at the skill and local tiers, which
use the docker substrate, not this controller (issue #350).

### Alternatives weighed

1. **Namespace-scope the controller's cache/informer** (the "cleanest" fix: a
   `--namespace` flag or `cache.Options.DefaultNamespaces`, so its NetworkPolicy
   LIST is namespaced and the existing namespaced Role suffices). **Rejected as
   infeasible without recompiling upstream.** v0.5.0's binary exposes no
   namespace-scoping flag (`main.go` defines `--leader-elect`,
   `--leader-election-namespace`, `--webhook-namespace`, `--extensions`,
   concurrency/pprof knobs -- and no `--namespace`), and the manager hardcodes a
   cluster-wide cache. Achieving this would require patching and rebuilding the
   vendored public image, a maintenance burden (re-patch on every upstream bump)
   far out of proportion to a chart RBAC fix.

2. **Re-widen the ClusterRole to full `networkpolicies`** (grant cluster-wide
   create/delete/get/list/patch/update/watch back). **Rejected: regresses #66.**
   It re-opens the exact cross-namespace egress-policy-deletion hole the
   fail-closed-egress invariant exists to prevent.

## Decision

Split the controller's `networkpolicies` RBAC by verb along the read/mutate line:

- **Cluster-scope, read-only.** A new `ClusterRole` +
  `ClusterRoleBinding` grants **only `get`, `list`, `watch`** on
  `networkpolicies` to the `agent-sandbox-controller` SA. This is the minimum the
  cluster-wide informer needs to sync, and it contains no ability to change
  anything.
- **Namespace-scope, mutating.** The existing namespaced `Role`/`RoleBinding`
  (`agent-sandbox-controller-networkpolicies`, in `.Release.Namespace`) keeps the
  **mutating verbs `create`, `delete`, `patch`, `update`** (plus `get`), so the
  controller can only create/update/delete NetworkPolicies in the release
  namespace -- where this chart creates all Sandboxes.

The security guarantee #66 established is preserved in its load-bearing form: the
controller SA has **no cluster-wide mutate** on networkpolicies, so a compromised
SA still cannot delete or alter the fail-closed egress policy in any other
namespace. The only new grant is read visibility, which cannot be used to defeat
containment. When a cluster-scope watch is genuinely required -- and here it is,
because the upstream binary offers no namespaced alternative -- least privilege
means read-only cluster-wide, mutate namespaced.

Additionally, add an **install-time gate** so a future RBAC regression fails the
install, not the first turn: a `helm test` / hook Job asserts the controller
Deployment becomes Available and its log shows the manager reached
`Starting workers` (which is emitted only after all informer caches, including
NetworkPolicy, sync). Gated on `agentSandbox.controller.deploy`.

## Consequences

- A real-model `cluster up` on stock k8s (>= 1.31) brings the controller to
  Running and binds `SandboxClaim`s with no manual RBAC patch.
- The controller SA gains cluster-wide **read** on networkpolicies. This is a
  deliberate, documented widening from #66's zero cluster-wide access, justified
  by the informer's structural requirement; it grants no mutate power.
- Re-vendoring a newer upstream release must re-apply this verb split alongside
  #66's securityContext patch (noted in `controller.yaml`'s header comment). If a
  future upstream release adds namespace-scoped caching, Alternative 1 becomes
  viable and should supersede this ADR.
- The install-time gate adds one hook Job to the controller-deploy path; it is
  skipped when `agentSandbox.controller.deploy` is false.
