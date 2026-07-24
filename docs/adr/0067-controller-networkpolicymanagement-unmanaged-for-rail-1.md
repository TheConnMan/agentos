# 67. The runner SandboxTemplate sets networkPolicyManagement: Unmanaged so Rail 1 is not additively defeated

Date: 2026-07-21
Status: Accepted

## Context

Issue #765 (release-blocker, security): a cluster-tier install with a tight
egress allowlist (`--allow-web-egress <github>/32`, `--allow-egress-host
openrouter`) did not actually get one. Packet-level evidence on a live k3s
install of v0.4.2: the chart rendered its own containment correctly --
`curie-runner-default-deny-egress` (egress: none) and
`curie-runner-allow-egress` (TCP/443 scoped to exactly the declared /32s) --
but the vendored agent-sandbox controller (ADR-0023) independently created
`curie-runner-network-policy` with a broad egress rule (`0.0.0.0/0` minus
RFC1918/link-local, both address families). A non-allowlisted host
(example.com) was reachable from a real sandbox pod.

Kubernetes NetworkPolicy semantics are additive, not restrictive-intersecting:
when multiple NetworkPolicy objects select the same pod for the same policy
type, the CNI allows traffic that matches ANY of them. There is no mechanism
for one policy to narrow what a separate, broader policy already permits. So
the chart's own Rail 1 (`templates/security-networkpolicy.yaml`: default-deny
egress + DNS/collector/MinIO/inference carve-outs + the operator allowlist,
`security-networkpolicy.yaml`) cannot, by adding yet another NetworkPolicy,
override or narrow a separately-managed permissive policy selecting the same
pods -- the union of "deny-all + narrow allow" and "allow public internet" is
just "allow public internet". Rail 1 was never broken; it was being silently
unioned away by a second policy the chart does not control.

### The controller's actual behavior (verified, not assumed)

The vendored controller (`charts/curie/files/agent-sandbox/controller.yaml`,
upstream `kubernetes-sigs/agent-sandbox` v0.5.0,
`registry.k8s.io/agent-sandbox/agent-sandbox-controller:v0.5.0`) runs with args
`--leader-elect=true --extensions` -- no flag governs NetworkPolicy generation
or scope (ADR-0023 already established there is no `--namespace` flag either;
same conclusion holds for network-policy behavior: it is not a controller
flag). The lever lives in the CRD instead: the vendored
`sandboxtemplates.extensions.agents.x-k8s.io` CRD
(`charts/curie/crds/crd-sandboxtemplates.yaml`) already carries two spec
fields on `SandboxTemplate`, present in this repo before this change but never
set by the chart:

- `spec.networkPolicyManagement`: `Managed` (default) or `Unmanaged`.
- `spec.networkPolicy.{ingress,egress}`: a restricted subset of
  `NetworkPolicySpec` the controller applies when Managed and the field is
  non-nil.

Upstream's doc comments (`sigs.k8s.io/agent-sandbox/extensions/api/v1alpha1`,
confirmed via `pkg.go.dev` and the project's own docs site,
`agent-sandbox.sigs.k8s.io`) state the exact behavior: a single shared
NetworkPolicy is reconciled per SandboxTemplate (not per pod, not once at
creation -- an ongoing controller-runtime `Owns(&NetworkPolicy{})` watch, per
ADR-0023's citation of `sandboxtemplate_controller.go:253`). When Managed and
`spec.networkPolicy` is nil, the controller applies its own "Secure Default":
egress to the public internet, blocking RFC1918/link-local/metadata -- exactly
what issue #765 observed. Setting `networkPolicyManagement: Unmanaged` makes
the controller skip creating (and reconciling) that policy for the template
entirely.

Because this is a continuous reconcile against a field on the SandboxTemplate
object, not a one-time creation, a chart-side "delete the extra policy after
install" hack would be fought and reverted on the controller's next reconcile
loop. The field is the only lever; there is nothing to patch around it.

### Alternatives weighed

1. **Populate `spec.networkPolicy.egress` with the chart's own allowlist,
   keep `networkPolicyManagement: Managed`.** Would also work (Managed +
   non-nil `networkPolicy` uses the custom rules instead of the Secure
   Default), but requires translating `security.networkPolicy.allowedEgress`
   (chart's `{cidr, ports}` shape, plus the DNS/collector/MinIO/inference
   carve-outs and the metadata `except` logic) into the CRD's restricted
   `NetworkPolicyEgressRule` shape a second time. That is a second
   representation of the same policy that can drift from Rail 1's, for no
   benefit over Unmanaged in this chart, which already renders a complete,
   independently-tested Rail 1. Rejected as unnecessary duplication.
2. **Fork/patch the vendored controller image to add a scoping flag.**
   Rejected outright: the exact anti-pattern ADR-0023 already weighed and
   rejected for the same vendored binary -- a maintenance burden
   (re-patch on every upstream bump) out of proportion to a chart-level
   field that already exists for this purpose.
3. **Leave Managed and rely on periodic deletion of the extra policy.**
   Rejected: the controller reconciles the shared policy continuously
   (`Owns(&NetworkPolicy{})`), so a deleted object is recreated on the next
   reconcile. Fighting a controller loop is not a fix.

## Decision

The runner `SandboxTemplate` (`templates/agent-sandbox.yaml`) sets
`spec.networkPolicyManagement: Unmanaged` whenever
`security.networkPolicy.enabled` is true (the default). This is not narrowed
to "only when an allowlist is configured": the additive-union defeat applies
to Rail 1's default-deny baseline itself, with or without a populated
`allowedEgress`, so every install with Rail 1 on needs the controller kept out
of the way. When `security.networkPolicy.enabled` is false (the operator has
turned Rail 1 off entirely), the field is left unset, so the CRD default
(`Managed`) applies and the controller's own baseline Secure Default policy
still protects the pod rather than nothing at all.

A render-assertion (`ci/render-assertions.sh`, Assertion 10) pins both
branches: the default render carries `networkPolicyManagement: Unmanaged`,
and `--set security.networkPolicy.enabled=false` leaves the field absent. A
live-cluster check (`templates/security-probe.yaml`, Claim 1c) asserts, when
this release deploys the controller, that no
`<release>-runner-network-policy` object exists in the release namespace --
proving Unmanaged actually suppressed it, not just that the chart asked for
it.

## Consequences

- Rail 1 (`security-networkpolicy.yaml`) becomes the sole NetworkPolicy
  selecting runner-sandbox pods whenever it is enabled: no second,
  independently-managed policy can silently widen it via the additive-union
  behavior.
- If a customer cluster runs agent-sandbox from a DIFFERENT release
  (`agentSandbox.controller.deploy: false`, per the existing "one controller
  per cluster" invariant), that external controller must also honor
  `networkPolicyManagement` for this to hold. This is the same class of
  cross-release version-compatibility assumption ADR-0023 already accepts for
  the vendored CRD/controller pairing; it is not a new risk this change
  introduces.
- Re-vendoring a newer upstream release must re-verify `networkPolicyManagement`
  still exists with this exact semantic (alongside the two patches already
  called out in `controller.yaml`'s header comment and ADR-0023's verb split).
- The existing security probe's Claim 1 (a synthetic pod carrying the chart's
  own `runner-sandbox` selector labels) does not, by itself, exercise a real
  Sandbox-controller-created pod; Claim 1c closes the specific gap this issue
  exposed by checking for the controller's shadow NetworkPolicy object
  directly, without requiring a full bound SandboxClaim in the test hook.
