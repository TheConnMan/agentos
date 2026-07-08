# Operating the `cluster` target

This doc is the runbook for the **`cluster`** target: the AgentOS platform
running on a Kubernetes cluster (a Helm release). The same `agentos` binary
installs and runs it, wrapping the umbrella Helm chart the way `linkerd` or
`cilium` wrap theirs. Every verb takes `--dry-run` to print the exact
`helm`/`kubectl` command line (secrets masked) without executing.

`cluster` is the heaviest of three CLI targets. Reach for a lighter one when it
answers your question:

## Which target do I want?

| Target | What runs | Slack | Kubernetes | Verbs | Reach for it to |
|---|---|---|---|---|---|
| `skill` | Just the runner container on the host Docker daemon. No platform, no queue, no API, no Slack. Fully offline. | none | none | `up` `down` `status` `message` `eval` | Iterate a plugin/skill against a local runner, the fastest loop. |
| `local` | The full platform via docker compose (Postgres + Valkey + Langfuse + API + worker). | none | none | `up` `down` `status` `message` `deploy` | Exercise the real queue -> worker -> sandbox -> reply product loop with zero Slack and zero Kubernetes. Its API is published on host port `28000`. |
| `cluster` | The platform on Kubernetes (a Helm release). | optional | yes | `up` `down` `status` `message` `deploy` | Operate and drive a deployed cluster release (this doc). |

The universal quartet `up`/`down`/`status`/`message` is on all three targets;
`skill` adds `eval`, and `local`/`cluster` add `deploy`. The `skill` target is
the runner-only loop; `local` and `cluster` add the full platform in front of
the identical runner and ACI. For the `skill` and `local` targets see
[`cli/README.md`](../cli/README.md) and the
[README](../README.md#which-target-do-i-want); the rest of this doc is `cluster`.

Prerequisites: `kubectl` and `helm` on PATH, pointed at a reachable cluster
(the `agents.x-k8s.io` Agent Sandbox CRDs and a NetworkPolicy-enforcing CNI are
installed by the chart's preflights; see `charts/agentos/README.md`).

## Install and inspect

- `agentos cluster up` runs `helm upgrade --install` using the chart resolved
  from the version-pinned release asset by default, so a downloaded release
  binary needs no repo checkout. Pass `--chart <path-or-tgz>` to override with a
  local chart for chart development. For local development, override resolved
  artifacts with `-f <compose>`, `--chart <path-or-tgz>`, and `--image <ref>`.
  It installs into the `agentos` namespace, exposing the UI and Langfuse on node
  ports (pass `--no-expose` to keep them ClusterIP-only). It reads
  `AGENTOS_MODEL_CREDENTIALS`: when the env var is set it switches the runner
  off the fake model, forwards the credential through the masked `--set`
  machinery (so `--dry-run` never prints it), and opens the runner's
  fail-closed egress to the model provider; when it is absent the release
  installs sealed (canned replies) and `up` warns that replies stay canned
  until the env var is set and `up` is re-run. Pass `--fake-model` to force the
  sealed install even when the credential is present (a dev/CI escape hatch).
  Pass `--allow-web-egress <CIDR>` (repeatable) to open runner egress on TCP 443
  to each declared CIDR for skill/tool web access -- appended additively after
  the model carve-out at index `[0]`, so it never weakens the model rule; omit it
  and egress stays sealed. This is the platform enablement the weather example
  (#36) needs, whose skill answers via a live web search: `agentos cluster up
  --allow-web-egress 0.0.0.0/0` opens the open internet (still minus the
  `169.254.169.254` metadata endpoint the chart carves out of `0.0.0.0/0`), or
  narrow the CIDR to a specific web-search provider for a tighter posture. The
  raw helm equivalent is
  `--set 'security.networkPolicy.allowedEgress[1].cidr=0.0.0.0/0'` plus
  `...[1].ports[0].protocol=TCP` and `...[1].ports[0].port=443` (index `[1]`
  because the model entry is `[0]`; use index `[0]` instead when installing
  sealed with no model credential, so the array has no gap).
- `agentos cluster status` reports release health, pod readiness, and the access URLs;
  the UI URL carries `?api=1`, so it opens wired to the in-cluster API (the
  deployed UI proxies `/api/` there).
- `agentos cluster down` uninstalls the release and sweeps its runtime namespaces; the
  `agents.x-k8s.io` CRDs are left in place. It prompts before deleting unless
  `--yes` is passed.

## Connecting Slack

Connecting a real Slack workspace is a raw `helm upgrade --reuse-values` that
sets the dispatcher's app and bot tokens and clears `worker.slackApiBaseUrl=`
(un-wiring any `agentos cluster message` stub routing). It is intentionally not a CLI
verb; the chart's `NOTES.txt` prints the exact command after `up`.

## Deploy a bundle to the cluster

Before `agentos cluster message` can drive an agent, a bundle must be deployed to
the in-cluster platform API with `agentos cluster deploy`. The `agentos-api`
service is ClusterIP, but the UI is exposed on a NodePort (`:30080`) and its
nginx reverse-proxies `/api/` to the in-cluster API, so a `/api`-suffixed node
URL reaches the API with no port-forward. Take the UI URL from `agentos cluster
status` and give `cluster deploy` its `/api` path:

```bash
agentos cluster deploy --plugin-dir <bundle-dir> \
  --api-url http://<node>:30080/api --api-key agentos-dev-key
```

If you installed with `--no-expose` (no NodePort) or otherwise can't reach the
node port, fall back to a manual port-forward of the ClusterIP service. `cluster
deploy` does not self-manage one, so its default `--api-url
http://localhost:8000` is unreachable until you forward the API yourself. Run the
port-forward first, deploy, then release it:

```bash
kubectl port-forward svc/agentos-api 8000:8000 -n agentos &
agentos cluster deploy --plugin-dir <bundle-dir> \
  --api-url http://localhost:8000 --api-key agentos-dev-key
kill %1   # cluster message plumbs its own forwards, so this one is no longer needed
```

Without a deploy, `agentos cluster message` fails with `no agents are deployed on
the platform API`.

## Driving a deployed cluster with zero Slack

`agentos cluster message "..."` exercises a deployed release end to end with no Slack
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

`agentos local up|down|status` wraps the local compose stack. A no checkout
release binary uses the pinned `compose.release.yaml` release asset, while repo
development still uses `compose.dev.yaml`, so the inner loop and the cluster
share one CLI. `local up` brings up the full product stack (API + worker
alongside the backing stores, plus the console UI at
`http://localhost:28080/?api=1`), so
`agentos local deploy --api-url http://localhost:28000` then `agentos local message
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
