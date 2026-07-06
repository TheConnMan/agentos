# CLAUDE.md — charts/agentos

The umbrella Helm chart: Langfuse + Postgres + Valkey + ClickHouse + MinIO +
OTel Collector, plus the Agent Sandbox substrate and its security rails.
Owning tasks: A1 (backbone + preflights), A2 (security rails). Full component
and rail detail in `charts/agentos/README.md`.

## Load-bearing invariants

- **Values, not templates, for anything that varies by environment.** Per
  the platform-level rule this repo also follows: resource limits, replica
  counts, and probe settings belong in `values.yaml` / a values overlay, not
  hardcoded in `templates/`. A value safe on a 4 GB scratch cluster can OOMKill
  on a smaller install.
- **Every backing store follows the same toggle + BYO idiom.** `<store>.deploy`
  (default `true`) gates whether the in-chart resource renders; flipping it
  to `false` repoints consumers (Langfuse env, the collector config) at the
  BYO `host`/`port`/`auth`/`existingSecret` fields on the same block. A new
  backing store must follow this exact pattern -- do not add a store with a
  different enable/disable shape.
- **Values keys are camelCase, not hyphenated.** Go templates cannot
  dot-index a hyphenated key. Keep this consistent across any new values
  additions.
- **Fail-closed egress, always.** `security.networkPolicy.allowedEgress` is
  empty by default; an unset allowlist must never mean allow-all. If you add
  a new egress destination the runner needs, it goes into this allowlist
  explicitly -- never widen the default-deny baseline itself.
- **Both preflights are mandatory Helm hooks, not advisory scripts.** The
  CPU-AVX/ClickHouse-pin check (`preflights.avxCheck`) and the
  NetworkPolicy-enforcement probe (`preflights.networkPolicyProbe`) block a
  broken install. Do not make either skippable by default, and do not add a
  new cluster-dependent assumption (a CNI feature, a kernel feature) without
  a matching preflight -- an assumption that silently fails on a customer
  cluster is exactly the failure mode these exist to prevent.
- **gVisor needs `runsc` on the node; the chart cannot install it.** On a
  cluster without it, use the ready-made overlay
  `-f charts/agentos/values-e2e-nogvisor.yaml` (sets `runtimeClassName=""` and
  disables the gVisor preflight, leaves every other rail on) rather than
  hand-editing `security.gvisor.*` -- the overlay is the supported opt-out
  path for e2e/scratch clusters.
- **CRDs in `crds/` are vendored, never templated.** Helm does not
  upgrade or delete `crds/` content; a teardown needs a manual
  `kubectl delete crd <name>`. Do not move CRD definitions into `templates/`
  to make them "manageable" -- that changes install ordering guarantees
  Helm's `crds/` convention provides.
- **The controller (`agentSandbox.controller.deploy`) is cluster-scoped.**
  Install it from exactly one release per cluster; leave it `false` on any
  cluster that already runs `agent-sandbox`. Do not default this to `true`
  in a values file intended for a shared/multi-release cluster.
- **The runner image is `IfNotPresent` + prewarm, NOT `Always`.** Images pull
  from GHCR; the four Deployment-managed services default to `Always` (fresh
  `:latest` on every rollout), but the runner must not -- a sandbox pod is
  created per Slack thread, and an in-boot image download can blow the
  worker's claim timeout (live incident 2026-07-06). The runner-prewarm
  DaemonSet (`agentSandbox.runner.prewarm`) pulls the runner image at
  install/upgrade instead, and every `helm upgrade` rolls it to refresh the
  cache. Do not flip the runner to `Always` and do not disable the prewarm
  on `:latest`-tag clusters without accepting stale-image risk.

## Verify

```bash
helm lint charts/agentos
helm template charts/agentos -f charts/agentos/values-dev.yaml   # chart-authoring check, no cluster contact
```

Cluster verification (k8scratch or `kind`; see the root `CLAUDE.md`'s
k8scratch section):
```bash
helm install agentos-dev charts/agentos -n agentos-dev --create-namespace \
  -f charts/agentos/values-dev.yaml
kubectl get pods -n agentos-dev -w
helm test agentos-dev -n agentos-dev                              # re-runs both preflights + the A2 security probe suite
kubectl logs -n agentos-dev job/agentos-dev-preflight-avx
kubectl logs -n agentos-dev job/agentos-dev-security-probe        # rails 1, 2, 4
kubectl logs -n agentos-dev agentos-dev-security-probe-hardening  # rail 3
```
