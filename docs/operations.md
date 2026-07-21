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
| `local` | The full platform via docker compose (Postgres + Valkey + Langfuse + API + worker). | stub by default, optional real Slack with `--slack` | none | `up` `down` `status` `comms` `message` `deploy` | Exercise the real queue -> worker -> sandbox -> reply product loop with zero Slack and zero Kubernetes. Its API is published on host port `28000`. |
| `cluster` | The platform on Kubernetes (a Helm release). | optional | yes | `up` `down` `status` `comms` `message` `deploy` | Operate and drive a deployed cluster release (this doc). |

The universal quartet `up`/`down`/`status`/`message` is on all three targets;
`skill` adds `eval`, while `local` and `cluster` add `comms` plus `deploy`. The `skill`
target is the runner-only loop; `local` and `cluster` add the full platform in
front of the identical runner and ACI. For the `skill` and `local` targets see
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
  `AGENTOS_CREDENTIALS` (deprecated alias `AGENTOS_MODEL_CREDENTIALS`): when the env var is set it switches the runner
  off the fake model and forwards the credential through the masked `--set`
  machinery (so `--dry-run` never prints it); when it is absent the release
  installs sealed (canned replies) and `up` warns that replies stay canned
  until the env var is set and `up` is re-run. Pass `--fake-model` to force the
  sealed install even when the credential is present (a dev/CI escape hatch).
  A model credential alone opens **no** egress: the sandbox stays fail-closed, so
  the model host is unreachable until you open its provider egress explicitly.
  When a credential is present but no egress was opened, `up` warns the sandbox is
  sealed and the model is unreachable, naming both flags below.
  Pass `--allow-egress-host <provider>` (repeatable) to open runner egress on TCP
  443 to a named model provider -- one of `anthropic` or `openrouter`. Each maps
  to that provider's API hostname (`anthropic` -> `api.anthropic.com`,
  `openrouter` -> `openrouter.ai`), which the
  CLI resolves to narrow host routes (`/32` + `/128`) at install time, from the
  machine running `cluster up` (so the resolved IPs can differ from the runner's
  in-cluster view under GeoDNS or split-horizon DNS). The set is
  intentionally limited to the two providers the runner can drive today
  (`anthropic` via `sk-ant-`, `openrouter` via `sk-or-`); others (OpenAI, Gemini,
  the base-URL-override providers) are layered in only once the runner supports
  them. No provider
  IPs are baked into the binary, only hostnames, because provider/CDN IPs rotate;
  if calls start failing after a rotation, re-run `up` to re-resolve. An unknown
  `--allow-egress-host` value is a usage error listing the accepted providers and
  pointing at `--allow-web-egress` for arbitrary destinations.
  Pass `--allow-web-egress <CIDR>` (repeatable) to open runner egress on TCP 443
  to each declared CIDR for skill/tool web access or for a destination no named
  provider covers; omit both flags and egress stays sealed. This is the platform
  enablement the weather example (#36) needs, whose skill answers via a live web
  search: `agentos cluster up --allow-web-egress 0.0.0.0/0` opens the open
  internet (still minus the `169.254.169.254` metadata endpoint the chart carves
  out of `0.0.0.0/0`), or narrow the CIDR to a specific web-search provider for a
  tighter posture. When a declared value is a default route (`0.0.0.0/0`, `::/0`,
  or any `/0` prefix), `up` prints a distinct rail-removal warning -- opening
  egress to the whole internet removes the default-deny rail for a
  prompt-injectable sandbox, so prefer a narrow CIDR unless you genuinely need the
  open internet. The declared egress entries occupy the `allowedEgress` array in
  order: provider host routes from `--allow-egress-host` take the leading indices
  only when that flag is passed, followed by any `--allow-web-egress` CIDRs. The
  raw helm equivalent of a single web-egress rule (with no provider egress) is
  `--set 'security.networkPolicy.allowedEgress[0].cidr=0.0.0.0/0'` plus
  `...[0].ports[0].protocol=TCP` and `...[0].ports[0].port=443`; shift the index
  up by one for each preceding `--allow-egress-host` entry so the array has no gap.
- `agentos cluster status` reports release health, pod readiness, and the access URLs;
  the UI URL carries `?api=1`, so it opens wired to the in-cluster API (the
  deployed UI proxies `/api/` there).
- `agentos cluster down` uninstalls the release and deletes only the namespaces
  this release created, identified by the ownership label `up` stamped on them
  (`agentos.dev/created-by=<release>`); pre-existing (unlabeled) namespaces and
  the `agents.x-k8s.io` CRDs are left untouched. It prompts before deleting
  unless `--yes` is passed. If `helm uninstall` fails (for example a transient
  API-server blip), teardown does not abort: the ownership-scoped namespace
  sweep still runs so compute is not left orphaned. If the sweep's label
  selector matches nothing (for example a pre-existing namespace, which is
  never stamped with the ownership label), the error message says so plainly
  rather than claiming namespaces were removed. If teardown still cannot
  complete, the command exits nonzero (exit 3 for a transient/retryable
  failure, exit 1 otherwise) and prints an exact resumable cleanup command,
  also carried in the `--json` `{error, fix}` payload, to run once the API
  server is reachable; when both the uninstall and the sweep are still
  outstanding, that resumable command aggregates both steps' exit statuses so
  re-running it verbatim cannot misreport success while the release record is
  still stale. See ADR-0064 (`docs/adr/0064-fail-forward-cluster-teardown.md`).

## Connecting Slack

Use `agentos cluster comms --slack` to wire a real Slack workspace onto the
release. It is a thin `helm upgrade --reuse-values` wrapper that sets the
dispatcher's app and bot tokens and, on connect, clears
`worker.slackApiBaseUrl=` to un-wire any `agentos cluster message` stub routing.
After the upgrade it also restarts and waits for the worker (and, on connect,
the dispatcher) so the running pods pick up the changed tokens, since a
Secret change alone does not roll pods whose token comes from a
`secretKeyRef` env var.

Connect:

```bash
SLACK_APP_TOKEN=xapp-... \
SLACK_BOT_TOKEN=xoxb-... \
agentos cluster comms --slack
```

Disconnect:

```bash
agentos cluster comms --slack --disconnect
```

Dry run:

```bash
SLACK_APP_TOKEN=xapp-... \
SLACK_BOT_TOKEN=xoxb-... \
agentos cluster comms --slack --dry-run
```

The env-backed token values are masked in dry-run output and are never printed
in full.

### Local compose comms

Use `agentos local comms --slack` to wire the compose stack to a real Slack
workspace. It reads `SLACK_APP_TOKEN` and `SLACK_BOT_TOKEN`, masks both values
in printed commands, starts the dispatcher, and points the worker at real
Slack.

Disconnect:

```bash
agentos local comms --slack --disconnect
```

Disconnect stops the dispatcher and restores the local Slack stub so
`agentos local message` keeps working.

Dry run:

```bash
SLACK_APP_TOKEN=xapp-... \
SLACK_BOT_TOKEN=xoxb-... \
agentos local comms --slack --dry-run
```

Dry run prints the compose command with masked token values and does not change
the stack.

## Deploy a bundle to the cluster

Before `agentos cluster message` can drive an agent, a bundle must be deployed to
the in-cluster platform API with `agentos cluster deploy`. Per ADR-0057, with no
`--api-url`, `cluster deploy` self-plumbs a `kubectl port-forward` to
`svc/<release>-api` (a loopback tunnel) and dials `http://localhost:<port>` --
no manual port-forward and no UI NodePort proxy involved:

```bash
agentos cluster deploy --plugin-dir <bundle-dir>
```

With no `--api-key`/`AGENTOS_API_KEY` either, the key is auto-discovered by
reading `api.apiKey` out of the release's `<release>-secrets` Secret (decoded
server-side, so the plaintext never lands in argv); the discovered key travels
only in the `X-API-Key` header over the loopback tunnel, never over the
cleartext UI `/api` NodePort proxy that ADR-0024 used for this path. Pass
`--api-key` explicitly (or set `AGENTOS_API_KEY`) to override discovery with
your own key.

An explicit `--api-url` (e.g. `http://<node>:30080/api`, ADR-0024's UI proxy
still available as the escape hatch) or `AGENTOS_API_URL` direct-dials the given
URL exactly as given, with no tunnel. If the auto-discovered key would then
travel over plain `http://`, `cluster deploy` refuses rather than leak it on the
wire -- pass `--api-key` explicitly to acknowledge, use an `https://` URL, or
omit `--api-url` to go back over the loopback tunnel.

Key discovery fails with a usage error telling you to pass `--api-key` when the
release's `<release>-secrets` Secret cannot be read. The port-forward itself
fails with a hint to check `agentos cluster status` if the release is not
healthy.

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
