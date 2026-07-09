# charts/agentos

The umbrella Helm chart that installs the whole AgentOS (Relay) stack on a
single node. It installs the backing-store stack (Langfuse + Postgres + Valkey +
ClickHouse + MinIO + OTel Collector, dev profile, BYO toggles, the two
preflights) plus the security rails as chart defaults.

The chart is a direct port of the proven `compose.dev.yaml` dev stack: same
images, same tags, same `:24.8` ClickHouse pin, same headless-bootstrapped
Langfuse dev project. So the compose stack and this chart verify
the identical stack. Rather than vendoring the upstream Langfuse chart and its
Bitnami subcharts, each component is a first-class template here -- this keeps
the single-node footprint controllable and avoids the Bitnami-catalog
(`bitnamilegacy/*`) instability. It still follows the Langfuse chart *idiom*:
every backing store is toggle-gated with a single-block bring-your-own surface.

## Install

The defaults are the flagship path: GHCR images, the runner substrate and its
controller on, a modest single-node footprint, and graceful degradation when the
cluster lacks Slack tokens or runsc. So a fresh install is two commands.

**Step 1 -- bare install.** Nothing to build, no overlays, no `--set`:

```bash
helm install agentos charts/agentos -n agentos --create-namespace
kubectl get pods -n agentos -w
```

This brings up the full stack (Langfuse + stores + OTel), the four app services
from GHCR, and the runner sandbox substrate. Two things degrade gracefully so the
install is green with zero secrets:

- **Slack** is not connected (no tokens), so the dispatcher Deployment is skipped
  rather than crash-looped, and the runner stays in offline fake-model mode.
- **gVisor** kernel isolation is `auto`: if the cluster has the `gvisor`
  RuntimeClass, runner pods use it; if not, they run without it and `NOTES.txt`
  prints a warning. Either way the install does not block.

**Step 2 -- connect Slack + a real model.** When you have Slack tokens and a
model credential, upgrade in place (the exact command is also printed in
`NOTES.txt` after step 1):

```bash
helm upgrade agentos charts/agentos -n agentos --reuse-values \
  --set dispatcher.slack.appToken=xapp-... \
  --set dispatcher.slack.botToken=xoxb-... \
  --set dispatcher.slack.signingSecret=... \
  --set agentSandbox.runner.fakeModel=false \
  --set agentSandbox.runner.credentials=sk-ant-... \
  --set 'security.networkPolicy.allowedEgress[0].cidr=160.79.104.0/23' \
  --set 'security.networkPolicy.allowedEgress[0].ports[0].protocol=TCP' \
  --set 'security.networkPolicy.allowedEgress[0].ports[0].port=443'
```

Setting the two Slack tokens is what makes the dispatcher deploy. The runner
NetworkPolicy is fail-closed (`security.networkPolicy.allowedEgress` is empty by
default), so the `allowedEgress` flags are required to let real model calls reach
the API -- here Anthropic's published range (`160.79.104.0/23`, TCP 443). Add
further entries for any MCP endpoints the runner must reach. Because this upgrade
flips to a real model, under the default `security.gvisor.mode=auto` it now fails
closed on a cluster without the `gvisor` RuntimeClass (runsc) -- install runsc +
the containerd handler on every node first, or add `--set security.gvisor.mode=off`
(or `-f charts/agentos/values-e2e-nogvisor.yaml`) to run real code without kernel
isolation knowingly.

**Cluster variants:**

- **Cluster already runs the agent-sandbox controller** (cluster-scoped, one per
  cluster): add `--set agentSandbox.controller.deploy=false`.
- **No runsc and you want the no-gvisor shape to be explicit/deterministic**
  (skip the RuntimeClass lookup): `-f charts/agentos/values-e2e-nogvisor.yaml`.
  `auto` handles a runsc-less cluster only for the fake-model default; a
  real-model install (`fakeModel=false`) under `auto` now fails closed on a
  runsc-less cluster, so on such a cluster use this overlay (or `--set
  security.gvisor.mode=off`) to run real code without gVisor, or `--set
  security.gvisor.mode=require` to fail-hard.
- **Production sizing:** the default `resources`/persistence blocks are a modest
  single-node footprint (fits an 8-16 GB node). Raise them for real load.

**Local dev profile (offline, locally-built images).** `values-dev.yaml` repoints
every image at a locally-built, cluster-imported tag with `imagePullPolicy: Never`
for a fully disconnected cluster, so you MUST build and import the images first
(see "Publishing and pulling images" below) or the pods die `ErrImageNeverPull`:

```bash
# Prereq: build + import each first-party image into the cluster runtime first.
helm install agentos-dev charts/agentos -n agentos-dev --create-namespace \
  -f charts/agentos/values-dev.yaml
kubectl get pods -n agentos-dev -w
```

Reach the Langfuse UI:

```bash
kubectl port-forward -n agentos svc/agentos-langfuse-web 3000:3000
# http://localhost:3000  -- dev keys: pk-lf-agentos-dev / sk-lf-agentos-dev
```

App services emit OTLP to the **collector**, never straight to Langfuse
(Langfuse OTLP ingest is HTTP-only): `agentos-otel-collector:4317` (gRPC) /
`:4318` (HTTP). The collector forwards to Langfuse over HTTP.

## Components

| Component | Image | Notes |
|---|---|---|
| Langfuse web + worker | `langfuse/langfuse:3`, `langfuse/langfuse-worker:3` | Observability + eval backbone. Headless-bootstrapped dev org/project. |
| Postgres | `postgres:16-alpine` | Langfuse transactional store + app state. StatefulSet. |
| Valkey | `valkey/valkey:8-alpine` | Langfuse cache/queue + dispatcher Streams queue. |
| ClickHouse | `clickhouse/clickhouse-server:24.8` | Langfuse OLAP store. Tag pinned SSE4.2-safe (see preflight). |
| MinIO | `minio/minio` (+ `minio/mc` init) | Langfuse object storage; BYO real S3 in prod. |
| OTel Collector | `otel/opentelemetry-collector-contrib:0.119.0` | OTLP (gRPC+HTTP) -> Langfuse over HTTP. |

## Publishing and pulling images

First-party service images are published to GHCR by the `Release images`
workflow (`.github/workflows/release.yaml`) on every push to `main`, as
`ghcr.io/curie-eng/agentos-<service>` tagged with the commit SHA and `latest`.
All five first-party services build in the matrix: `agentos-api`,
`agentos-dispatcher`, `agentos-worker`, `agentos-ui`, and `agentos-runner`. The
chart defaults every first-party image at its `ghcr.io/curie-eng/agentos-*`
`:latest`, so the bare install (above) pulls from GHCR with no image overrides.
The four Deployment-managed services (api, dispatcher, worker, ui) use
`imagePullPolicy: Always` -- they pull once per rollout, so `Always` just keeps a
fresh install from serving a stale `latest` a node cached earlier. The **runner**
image is the exception: it uses `imagePullPolicy: IfNotPresent` because a sandbox
pod is cold-created per Slack thread, and an `Always` (re-)pull inside that boot
window blew past the worker's claim timeout and killed runs. Its freshness comes
instead from the `runner-prewarm` DaemonSet (`agentSandbox.runner.prewarm`,
default on with the sandbox substrate), which pulls the runner image `Always` and
keeps it pinned on every node; a Release-revision annotation rolls those pods on
every `helm upgrade` so the pin refreshes a churned `latest`. Pin an immutable tag
for reproducible deploys, where the pull policies are a cheap no-op.
A GHCR package inherits its repo's visibility, so on a **private** repo the image
is not anonymously pullable and the node needs credentials. Two supported paths:

- **Private + pull Secret (default posture).** Create a docker-registry Secret
  in the release namespace and reference it:
  ```bash
  kubectl create secret docker-registry ghcr-pull -n <ns> \
    --docker-server=ghcr.io --docker-username=<gh-user> \
    --docker-password=<a PAT or token with read:packages>
  helm install ... --set 'agentSandbox.runner.imagePullSecrets[0].name=ghcr-pull'
  ```
  The chart wires `imagePullSecrets` onto the runner SandboxTemplate pod.
- **Public package.** In the GHCR package settings make the package public; then
  no pull Secret is needed and `imagePullSecrets` stays empty.

For offline dev/e2e, `-f values-dev.yaml` overrides all five first-party images
back to locally-built, cluster-imported tags with `imagePullPolicy: Never`, so a
disconnected cluster never attempts a GHCR pull. That path requires building and
importing each image first:

```bash
for svc in api dispatcher worker ui; do
  docker build -f apps/$svc/Dockerfile -t agentos-$svc:local .
done
docker build -f runner/Dockerfile -t agentos-runner:latest .
# import each into the cluster runtime, e.g. for k3s:
for img in agentos-api:local agentos-dispatcher:local agentos-worker:local \
           agentos-ui:local agentos-runner:latest; do
  docker save "$img" | ssh <node> 'sudo k3s ctr images import -'
done
```

Skip the build+import and the `Never` pull policy leaves the pods stuck at
`ErrImageNeverPull`. For a from-GHCR install with no local build, just use the
bare `helm install` (the default) -- no overlay needed.

## Values surface and the BYO idiom

Keys are **camelCase** (Go templates cannot dot-index hyphenated keys). Every
backing store is condition-gated by `<store>.deploy` and carries its BYO fields
on the same block. To use an external instance, flip `deploy: false` and fill
`host` / `port` / `auth` (or `existingSecret`):

```yaml
# Use a managed Postgres instead of the in-cluster one
postgres:
  deploy: false
  host: my-rds.example.com
  port: 5432
  auth: { username: agentos, database: agentos }
  existingSecret: my-pg-secret   # must carry key: postgresPassword
```

Toggles (all default `true`): `langfuse.deploy`, `postgres.deploy`,
`valkey.deploy`, `clickhouse.deploy`, `minio.deploy`, `otelCollector.deploy`.
Flipping any to `false` removes its resources from the render; consumers
(Langfuse env, the collector config) repoint at the BYO host automatically.

Secrets: dev credentials live in `values.yaml` and are written to one
`<release>-secrets` Secret. For production, override the values or point
`langfuse.existingSecret` (and each store's `existingSecret`) at your own
Secrets. `langfuse.encryptionKey` must be 64 hex chars (`openssl rand -hex 32`).

## The two preflights

Both run as Helm hooks (blocking a broken install) and are re-runnable via
`helm test <release> -n <ns>`.

**(a) CPU-AVX / ClickHouse-pin check** (`preflights.avxCheck`). A pre-install /
pre-upgrade hook Job. ClickHouse >= 25.x is compiled for AVX and SIGILLs with
exit 132 on SSE4.2-only CPUs -- a crash-looping pod is a confusing way to learn
that. The Job reads the node's `/proc/cpuinfo`; if the node lacks AVX it FAILS
the install unless the configured ClickHouse tag is in
`clickhouse.sse42SafeTags` (`24.8`, `24.3`, `23.8`). Skipped when
`clickhouse.deploy: false`. Test knob `preflights.avxCheck.forceNoAvx: true`
exercises the SSE4.2 branch on an AVX-capable node. Read the verdict:
`kubectl logs -n <ns> job/<release>-preflight-avx`.

**(b) NetworkPolicy-enforcement probe** (`preflights.networkPolicyProbe`). A
`helm test` Job. A CNI that silently ignores NetworkPolicy is a security
false-pass: the security rails' isolation policies would render but enforce nothing. The probe
does a before/after egress check -- reach an external target with no policy
(expect reachable), apply a default-deny-egress policy to itself (RFC1918
private ranges stay allowed so the control path survives; the public target is
denied), retry (expect blocked). It reports `enforcement=true` only if the after
egress is actually blocked, and `enforcement=false` (fails loudly) otherwise.

## Single-node footprint (measured on a disposable single-node k3s cluster, 4 GB / 4 core)

The dev profile fits the whole stack on one 4 GB node, but **tightly**: steady
state is ~3.3 GB / ~82% node memory once Langfuse migrations settle. Langfuse
web is the anchor (~950 MB resident with the heap cap raised to 1 GB; its Node
default heap of ~512 MB OOM-crashes under a tight container limit, so the dev
profile sets `NODE_OPTIONS=--max-old-space-size` and a 1536 MB web limit).
ClickHouse settles around ~255 MB single-replica with cluster mode off. This
matches the planned resize: everything runs in 4 GB for
chart/security verification, and a resize to >=8 vCPU / 16-20 GB gives
comfortable headroom for integration and soak testing.

## Security rails

The four security-boundary rails ship **on by default** (ADR-0006). They attach to the
agent-sandbox runner surface, so their NetworkPolicy / RBAC / probe resources
render only when `agentSandbox.deploy: true` (there are no runner pods to protect
otherwise). With the sandbox off, the rendered manifests are byte-identical to a
chart without the security rails.

| Rail | What ships | Values |
|---|---|---|
| 1. Default-deny egress + metadata block | NetworkPolicies selecting `component: runner-sandbox`: default-deny egress, allow-DNS, an operator-declared egress allowlist, and (optional) ingress lock. Arbitrary internet AND `169.254.169.254` are denied by construction. | `security.networkPolicy.*` |
| 2. Per-agent secret isolation | Least-privilege runner ServiceAccount (no secret get/list, token not mounted). The per-agent `resourceNames`-scoped Role is bound by the control plane per agent. | `agentSandbox.runner.serviceAccount.*` |
| 3. Non-root / read-only rootfs | Pod + container securityContext on the runner: `runAsNonRoot`, uid 1000, `readOnlyRootFilesystem`, drop ALL caps, no privilege escalation, RuntimeDefault seccomp, plus writable emptyDir scratch (`/tmp`, `/home/runner`) and `HOME`. | `agentSandbox.runner.hardening.*` |
| 4. gVisor kernel isolation | `runtimeClassName` on runner pods, driven by the `security.gvisor.mode` tri-state (`auto`/`require`/`off`) + a preflight that fails the install if the RuntimeClass is missing or downgraded, firing in `require` (always) and in `auto` for real-model runs + an optional RuntimeClass object. | `security.gvisor.*`, `security.gvisorPreflight.*` |

**Fail-closed egress.** `security.networkPolicy.allowedEgress` is EMPTY by
default: a fresh install denies all egress except DNS until the operator declares
where the model API and MCP endpoints live (`{cidr, ports}` entries). An unset
allowlist never means allow-all.

**Skill/tool web access.** The same allowlist also carries outbound web access a
skill or tool needs (e.g. a web-search provider). `agentos cluster up --allow-web-egress
<CIDR>` (repeatable) appends one entry per CIDR on TCP 443, additive to the model
rule at index `[0]` and without weakening it; the raw helm equivalent is `--set
'security.networkPolicy.allowedEgress[1].cidr=<CIDR>'` plus
`...[1].ports[0].protocol=TCP` and `...[1].ports[0].port=443` (index `[1]`
because the model entry is `[0]`; use index `[0]` instead when installing sealed
with no model credential, so the array has no gap). This is the platform enablement the weather
example (#36) depends on -- its skill answers via a live web search, which the
sealed default denies. `--allow-web-egress 0.0.0.0/0` opens the open internet
(still minus the `169.254.169.254` metadata endpoint the chart carves out of
`0.0.0.0/0`); narrow the CIDR to a specific provider for a tighter posture. Omit
the flag and the install stays fully sealed.

**gVisor needs runsc on the node**, and `security.gvisor.mode` is a tri-state
(default `auto`):

- **`auto`** -- at install/upgrade time the chart looks up the `gvisor`
  RuntimeClass. Present -> runner pods use it. Absent -> pods run without it and
  `NOTES.txt` warns. Never blocks the install, so a bare install works on any
  cluster. (Helm's `lookup` returns empty under `helm template`/--dry-run, so a
  templated render always shows the no-gvisor shape.) This never-blocks behavior
  applies to the fake-model default only; enabling a real model
  (`fakeModel=false` or `inference.deploy`) under `auto` renders the blocking
  `preflight-gvisor` hook, so a runsc-less real-model install fails closed
  instead of silently running on the host kernel.
- **`require`** -- always stamp the RuntimeClass AND run the `preflight-gvisor`
  hook, which blocks the install with a clear remediation if the runtimeclass is
  missing or downgraded to runc. The fail-hard production posture.
- **`off`** -- never stamp a RuntimeClass; kernel isolation disabled knowingly.
  `-f charts/agentos/values-e2e-nogvisor.yaml` selects this deterministically
  (skipping the lookup); every other rail stays on.

The class name and handler live on `security.gvisor.runtimeClassName` / `.handler`;
set `security.gvisor.installRuntimeClass=true` to have the chart create the
RuntimeClass object (the node must still provide the runtime).

**Verifying the rails.** The security-boundary probe suite re-runs as a `helm test`:

```bash
helm test <release> -n <ns>
kubectl logs -n <ns> job/<release>-security-probe            # claims 1, 2, 4
kubectl logs -n <ns> <release>-security-probe-hardening      # claim 3
```

Claim 1 does a before/after egress control (reachable under a temporary allow-all
-> blocked under the chart default-deny) so a non-enforcing CNI is caught as a
false-pass rather than trusted. Claim 4 reports honestly: if the gvisor
runtimeclass is absent it is marked NOT-TESTABLE (per the security-boundary test plan, never faked), with
enforcement asserted separately by the preflight and proven live in the security-boundary test plan
(`uname` = `4.19.0-gvisor`).

## What the agent-sandbox subchart needs to know

- **Fullname/labels:** resources are `<release>-<component>` and carry
  `app.kubernetes.io/{name,instance,component,managed-by}` plus `helm.sh/chart`.
  Reuse `agentos.selectorLabels` / `agentos.fullname` from `_helpers.tpl`.
- **Where to plug in:** add a `charts/agentos/agent-sandbox/` (or an
  `agentSandbox.*` values block + templates) gated by `agentSandbox.deploy`,
  same condition+BYO idiom. The runner image pre-pull, `SandboxWarmPool`, and
  the control-channel Service belong there.
- **Backing services to target:** the runner queue is Valkey at
  `<release>-valkey:6379` (password in secret key `valkeyPassword`); traces go
  to the collector at `<release>-otel-collector:4317/4318`, NOT to Langfuse
  directly.
- **NetworkPolicy is enforced** on the k3s target (probe proves it), so the security rails'
  runner-egress policies will actually bite -- design the sandbox egress allow
  (model API + declared MCP endpoints) accordingly. RFC1918 vs public is a clean
  split point, as the probe's own deny policy demonstrates.
- **Resource headroom:** on the current 4 GB node the backbone leaves little
  room for bursty runner pods; the sandbox substrate's warm-pool sizing should assume the resize,
  or run against the planned `kind` fallback for pure lifecycle tests.

## Agent Sandbox substrate

`agentSandbox.deploy: true` (the default) adds the runner `SandboxTemplate`
(`<release>-runner`) and `SandboxWarmPool` (`<release>-runner-pool`) that the
worker's sandbox substrate (`agentos_worker.sandbox`) claims from. Set it false
to install only the control plane + backing stores without the runner substrate.

- **CRDs** (`sandboxes.agents.x-k8s.io` + the three
  `*.extensions.agents.x-k8s.io`) are vendored from the upstream v0.5.0
  release into this chart's `crds/` directory, so Helm installs them before
  any template renders. Helm never upgrades or deletes `crds/` content:
  removing them after a teardown is a manual
  `kubectl delete crd <name>`.
- **Controller**: `agentSandbox.controller.deploy: true` installs the vendored
  upstream controller bundle (`files/agent-sandbox/controller.yaml`: namespace
  `agent-sandbox-system`, RBAC, webhook Service, and the Deployment running
  with `--extensions`). It is cluster-scoped; install it from exactly one
  release per cluster, or leave it false on clusters that already run
  agent-sandbox.
- **Runner image**: the pool runs `agentos-runner`, defaulting to
  `ghcr.io/curie-eng/agentos-runner:latest` with `imagePullPolicy: IfNotPresent`
  (per-thread cold boots must not contain a pull; the `runner-prewarm` DaemonSet
  keeps the image fresh on every node -- see "Publishing and pulling images").
  For offline
  dev/e2e, `-f values-dev.yaml` overrides it to a locally-built, cluster-imported
  tag with `imagePullPolicy: Never` (`docker build -f runner/Dockerfile -t
  agentos-runner .` from the repo root, then
  `docker save agentos-runner:<tag> | ssh <node> 'sudo k3s ctr images import -'`).
  Fake-model mode (`agentSandbox.runner.fakeModel`, default true) round-trips ACI
  events with no credential.
- **Per-claim env**: the template sets `envVarsInjectionPolicy: Overrides` so
  the substrate's resume path can inject `AGENTOS_HISTORY_REF` /
  `AGENTOS_SESSION_ID` per claim. Claims carrying env bind a fresh sandbox
  rather than a pre-warmed one; the fast path (no env) binds warm.
- Traces flow to `<release>-otel-collector:4318` (HTTP), per the collector
  rule above; the env block is omitted when `otelCollector.deploy: false`.
