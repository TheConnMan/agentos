# charts/agentos

The umbrella Helm chart that installs the whole AgentOS (Relay) stack on a
single node. Owning tasks: **A1** (this: Langfuse + Postgres + Valkey +
ClickHouse + MinIO + OTel Collector, dev profile, BYO toggles, the two
preflights) and **A2** (security rails as chart defaults, added later).

The chart is a direct port of the proven `compose.dev.yaml` (V1 dev stack): same
images, same tags, same `:24.8` ClickHouse pin, same headless-bootstrapped
Langfuse dev project. So V1 (compose) and V3 (this chart on k8scratch) verify
the identical stack. Rather than vendoring the upstream Langfuse chart and its
Bitnami subcharts, each component is a first-class template here -- this keeps
the single-node footprint controllable and avoids the Bitnami-catalog
(`bitnamilegacy/*`) instability. It still follows the Langfuse chart *idiom*:
every backing store is toggle-gated with a single-block bring-your-own surface.

## Install

Dev profile (single node, ~4 GB scratch cluster such as k8scratch):

```bash
helm install agentos-dev charts/agentos -n agentos-dev --create-namespace \
  -f charts/agentos/values-dev.yaml
kubectl get pods -n agentos-dev -w
```

Default profile (larger requests; supply real secrets for anything real):

```bash
helm install agentos charts/agentos -n agentos --create-namespace
```

Reach the Langfuse UI:

```bash
kubectl port-forward -n agentos-dev svc/agentos-dev-langfuse-web 3000:3000
# http://localhost:3000  -- dev keys: pk-lf-agentos-dev / sk-lf-agentos-dev
```

App services emit OTLP to the **collector**, never straight to Langfuse
(Langfuse OTLP ingest is HTTP-only): `agentos-dev-otel-collector:4317` (gRPC) /
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
false-pass: A2's isolation policies would render but enforce nothing. The probe
does a before/after egress check -- reach an external target with no policy
(expect reachable), apply a default-deny-egress policy to itself (RFC1918
private ranges stay allowed so the control path survives; the public target is
denied), retry (expect blocked). It reports `enforcement=true` only if the after
egress is actually blocked, and `enforcement=false` (fails loudly) otherwise.

## Single-node footprint (measured on k8scratch, 4 GB / 4 core, k3s)

The dev profile fits the whole stack on one 4 GB node, but **tightly**: steady
state is ~3.3 GB / ~82% node memory once Langfuse migrations settle. Langfuse
web is the anchor (~950 MB resident with the heap cap raised to 1 GB; its Node
default heap of ~512 MB OOM-crashes under a tight container limit, so the dev
profile sets `NODE_OPTIONS=--max-old-space-size` and a 1536 MB web limit).
ClickHouse settles around ~255 MB single-replica with cluster mode off. This
matches the build plan's anticipated resize: everything runs in 4 GB for
chart/security verification, and a resize to >=8 vCPU / 16-20 GB gives
comfortable headroom for the walking-skeleton and soak gates.

## What G1 (agent-sandbox subchart) needs to know

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
- **NetworkPolicy is enforced** on the k3s target (probe proves it), so the A2
  runner-egress policies will actually bite -- design the sandbox egress allow
  (model API + declared MCP endpoints) accordingly. RFC1918 vs public is a clean
  split point, as the probe's own deny policy demonstrates.
- **Resource headroom:** on the current 4 GB node the backbone leaves little
  room for bursty runner pods; G1's warm-pool sizing should assume the resize,
  or run against the plan's `kind` fallback for pure lifecycle tests.

## Agent Sandbox substrate (G1)

`agentSandbox.deploy: true` adds the runner `SandboxTemplate`
(`<release>-runner`) and `SandboxWarmPool` (`<release>-runner-pool`) that the
worker's sandbox substrate (`agentos_worker.sandbox`) claims from. Default off.

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
- **Runner image**: the pool runs `agentos-runner` built locally
  (`docker build -f runner/Dockerfile -t agentos-runner .` from the repo root)
  and imported into the cluster runtime
  (`docker save agentos-runner:<tag> | ssh <node> 'sudo k3s ctr images import -'`),
  hence `imagePullPolicy: Never` by default. Fake-model mode
  (`agentSandbox.runner.fakeModel`, default true) round-trips ACI events with
  no credential.
- **Per-claim env**: the template sets `envVarsInjectionPolicy: Overrides` so
  the substrate's resume path can inject `AGENTOS_HISTORY_REF` /
  `AGENTOS_SESSION_ID` per claim. Claims carrying env bind a fresh sandbox
  rather than a pre-warmed one; the fast path (no env) binds warm.
- Traces flow to `<release>-otel-collector:4318` (HTTP), per the collector
  rule above; the env block is omitted when `otelCollector.deploy: false`.
