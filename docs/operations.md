# Operating a cluster install

The same `agentos` binary installs and runs the platform on a Kubernetes
cluster, wrapping the umbrella Helm chart the way `linkerd` or `cilium` wrap
theirs. Every verb takes `--dry-run` to print the exact `helm`/`kubectl`
command line (secrets masked) without executing.

Prerequisites: `kubectl` and `helm` on PATH, pointed at a reachable cluster
(the `agents.x-k8s.io` Agent Sandbox CRDs and a NetworkPolicy-enforcing CNI are
installed by the chart's preflights; see `charts/agentos/README.md`).

## Install and inspect

- `agentos up` runs `helm upgrade --install` of `charts/agentos` into the
  `agentos` namespace, exposing the UI and Langfuse on node ports (pass
  `--no-expose` to keep them ClusterIP-only). It reads
  `AGENTOS_MODEL_CREDENTIALS`: when the env var is set it switches the runner
  off the fake model, forwards the credential through the masked `--set`
  machinery (so `--dry-run` never prints it), and opens the runner's
  fail-closed egress to the model provider; when it is absent the release
  installs sealed (canned replies) and `up` warns that replies stay canned
  until the env var is set and `up` is re-run. Pass `--fake-model` to force the
  sealed install even when the credential is present (a dev/CI escape hatch).
- `agentos status` reports release health, pod readiness, and the access URLs;
  the UI URL carries `?api=1`, so it opens wired to the in-cluster API (the
  deployed UI proxies `/api/` there).
- `agentos down` uninstalls the release and sweeps its runtime namespaces; the
  `agents.x-k8s.io` CRDs are left in place. It prompts before deleting unless
  `--yes` is passed.

## Connecting Slack

Connecting a real Slack workspace is a raw `helm upgrade --reuse-values` that
sets the dispatcher's app and bot tokens and clears `worker.slackApiBaseUrl=`
(un-wiring any `agentos message` stub routing). It is intentionally not a CLI
verb; the chart's `NOTES.txt` prints the exact command after `up`.

## Driving a deployed cluster with zero Slack

`agentos message "..."` exercises a deployed release end to end with no Slack
at all: it stands up a local Slack API stub, self-manages the kubectl
port-forwards, resolves the target agent's channel from the API, points the
deployed worker at the stub (`helm upgrade --reuse-values`), enqueues the exact
event a Slack mention would produce, boots the real Kubernetes sandbox, and
prints the reply. This lets a developer iterate on an agent built for someone
else's workspace with no Slack access. It refuses to hijack a release that is
already connected to a real workspace unless `--force-wire`. Full flag
reference and the multi-turn `--thread` flow are in
[`cli/README.md`](../cli/README.md).

## Bridging to the local dev stack

`agentos local up|down|status` wraps the `compose.dev.yaml` dev stack, so the
inner loop and the cluster share one CLI. `local up` brings up the full product
stack (API + worker alongside the backing stores), so
`agentos deploy --api-url http://localhost:28000` then `agentos message --local
"..."` drives a real queue -> worker -> sandboxed runner -> reply roundtrip with
no Slack and no Kubernetes. See the middle-mode runbook in the
[README](../README.md#quickstart).

## First-install findings

Notes from the first installs of the chart on fresh clusters, kept for the next
operator.

- **The agent-sandbox controller is opt-in.** The chart ships the agent-sandbox
  CRDs, but the vendored controller is gated behind
  `agentSandbox.controller.deploy`. A cluster that has the CRDs but no
  controller silently never binds claims, so a first install must set
  `agentSandbox.controller.deploy=true` unless the cluster already runs the
  controller.
- **gVisor stays off without runsc on the node.** Use the `values-e2e-nogvisor`
  overlay on nodes without `runsc`. All other security rails were verified ON in
  the first fresh-cluster install: default-deny egress, metadata-endpoint block,
  read-only rootfs, non-root, and per-agent secret isolation.
- **langfuse-web restarts ~2x during first boot** while ClickHouse and Postgres
  come up, then stabilizes. This is startup ordering, not a crashloop; do not
  treat the early restarts as a failure.
- **Exactly one Slack Socket Mode owner at a time.** Stop a local dispatcher
  before enabling `dispatcher.deploy=true` in the chart, and stop the in-cluster
  dispatcher before switching back to a local one for dev.
- **kube-router applies NetworkPolicy a few seconds after pod start.** A
  brand-new pod can see open egress for the first seconds before the policy
  lands. This is functionally irrelevant for runners (the first model call comes
  later) but worth knowing when reading probe output from the first seconds of a
  pod's life.
