# Supabase-for-Agents: On-Prem / Self-Hosted Build-vs-Adopt Architecture

Research + synthesis, 2026-07-02. Primary-source-verified (licenses, storage engines, and Helm chart structures fetched directly from LICENSE files, raw `Chart.yaml`/`values.yaml`, and official docs; every load-bearing claim carries a URL). Companion to `claude-design-prompt.md` (the product prototype) and the competitive scan on [[supabase-for-agents]].

**Guiding principle:** minimize what gets built. The team builds at most a nice UI, a thin CLI, an API server, a Slack dispatcher, and the Helm charts that orchestrate everything. Everything else leans on existing open source. No custom telemetry datastore, no custom eval database engine.

## Recommended default stack

**Build only the five things the guiding principle names — web UI, thin CLI, API server, Slack dispatcher, Helm charts — and make Langfuse (MIT core) the single adopted backbone for both agent observability and eval storage.** Langfuse is the linchpin because it is the only license-clean project that unifies traces + datasets + scores + experiments in one system, all in the free MIT core (the Enterprise Edition key gates only enterprise-admin features), with a documented full-CRUD public API strong enough to build our own UI/CLI on. Adopting it collapses two of the hardest "don't build a datastore" problems into one dependency, and it brings its own ClickHouse + Redis/Valkey + MinIO along, which we reuse (ClickHouse as the strongest SQL substrate for agent-run views, Valkey as the dispatcher's queue backend, MinIO for object storage). The Slack layer adopts **Slack Bolt (MIT)** in Socket Mode (correct for firewalled single-node self-host); agent execution runs the **Claude Agent SDK (MIT payload)** in containers, isolated on-prem by the **kubernetes-sigs/agent-sandbox CRD (Apache-2.0, but pre-1.0)** and on the cloud tier by **AWS Bedrock AgentCore (GA, proprietary)**; our own vanilla Postgres holds app state (agents, versions, deployments, users). We do **not** embed Grafana for the core product — generic Grafana panels render agent runs poorly — but we keep an optional `kube-prometheus-stack` for infra/ops dashboards Langfuse doesn't cover. It ships as one umbrella Helm chart with `condition:`-gated subcharts and first-class bring-your-own-DB toggles, copying the exact convention Langfuse's and SigNoz's charts both use.

**Main alternative (when to prefer it):** if you do not want to couple the product's schema/roadmap to Langfuse, or you need generic OTel observability that also covers non-agent infra in one pane, run **SigNoz (MIT core, single ClickHouse store) for observability + plain Postgres tables in your own API for evals**. Cleaner on license-and-ownership, but it costs you the eval store (you build it) and the unified trace+eval UX that is arguably the product's whole point. Prefer it only if unified LLM-trace tooling is explicitly not the differentiator.

## Component map (build vs adopt)

| Component | Build / Adopt | Project | License | Why |
|---|---|---|---|---|
| Web UI (skill.md authoring, run views) | **Build** | — | — | Product differentiator; agent-run-shaped views (conversation / tool-call trees) that no OSS UI renders. Thin: queries our API + Langfuse API. |
| Thin CLI (local emulation, deploy) | **Build** | — | — | Product surface; wraps our API. |
| API server (app state + orchestration) | **Build** | — | — | Owns agents/versions/deployments/users schema + git-flow logic. Recommend TS (shares Bolt-js + BullMQ) or Python (Bolt-python + Celery). |
| Slack dispatcher | **Adopt** | Slack Bolt (bolt-python / bolt-js) + `Assistant` class | MIT | Real self-hostable dispatcher substrate. Slack's agent *surfaces* are hosted, not OSS, but render in the user's client regardless of backend. |
| Agent execution (payload) | **Adopt** | Claude Agent SDK | MIT code (Anthropic Commercial ToS for usage) | Runs headless in a container, bundles Claude Code CLI. The agent runtime itself. |
| Agent isolation — on-prem | **Adopt (cautiously)** | kubernetes-sigs/agent-sandbox `Sandbox` CRD | Apache-2.0 | Declarative sandboxed stateful pod for untrusted agent runs on your K8s. **Pre-1.0, no maturity label** — keep a plain-K8s-Job fallback. |
| Agent isolation — cloud tier | **Adopt** | AWS Bedrock AgentCore Runtime | Proprietary (AWS-managed) | GA (Oct 13 2025), 8h execution windows + session isolation. Cloud-tier alternative to running agent-sandbox yourself. |
| App-state DB | **Adopt** | PostgreSQL (vanilla) | PostgreSQL License | Agents, versions, deployments, users. Can reuse Langfuse's bundled Postgres as a separate logical DB to shrink footprint. |
| Agent observability + trace store | **Adopt** | Langfuse | MIT (core) | Purpose-built LLM/agent tracing; brings ClickHouse. Unifies with eval store. |
| Eval store (datasets, scores, runs) | **Adopt** | Langfuse (same system) | MIT (core) | Datasets/scores/experiments/evals all MIT; strong public read-write API for evals-as-PR-checks. |
| OTel Collector | **Adopt** | OpenTelemetry Collector | Apache-2.0 | Standard OTLP ingestion into Langfuse; or instrument via Langfuse SDK directly. |
| Metrics/logs/traces stores | **Adopt (transitive)** | ClickHouse (via Langfuse) | Apache-2.0 | One columnar store; SQL is the strongest substrate for tool-call-tree reconstruction. |
| Object storage | **Adopt (transitive)** | MinIO (via Langfuse, aliased `s3`) | AGPLv3 (MinIO) | Buffers trace/eval events, media, exports. BYO real S3 in prod. |
| Redis / queue | **Adopt (transitive)** | Valkey (via Langfuse, aliased `redis`) | BSD-3 (Valkey) | Reuse for the dispatcher's BullMQ/Celery queue (forced by Slack's 3s ack + long runs). |
| Grafana | **Adopt, but do NOT embed in core UX** | Grafana OSS / kube-prometheus-stack | AGPLv3 | Embed only as disposable infra/ops dashboards; build custom run views instead. |

## Q1 — On-prem observability on OTel

### Stack comparison

| Stack | Datastore(s) | License (verbatim) | Single-node footprint |
|---|---|---|---|
| Grafana LGTM (`grafana/otel-lgtm`) | OTel Collector + Prometheus + Loki + Tempo + Pyroscope + Grafana (separate stores, 3 query languages) | Components AGPLv3. All-in-one image: *"intended for development, demo, and testing environments"* | Single dev-only container; not production. |
| SigNoz | **ClickHouse** (metrics+logs+traces in one) + Postgres + CH Keeper | *"Content outside of the above mentioned directories… is available under the 'MIT Expat' license"* with an `ee/` carve-out under a separate license | *"At least 4GB of memory allocated to Docker."* |
| Langfuse v3 | ClickHouse + Postgres + Redis/Valkey + S3/MinIO | *"All core Langfuse features and APIs are available in Langfuse OSS (MIT licensed) without any limits."* | Heaviest: 4 stores + 2 app containers. |

Langfuse v3 internal components, quoted from https://langfuse.com/self-hosting: Postgres *"The main database for transactional workloads"*; ClickHouse *"High-performance OLAP database which stores traces, observations, and scores"*; Redis/Valkey *"Used for queue and cache operations"*; S3/Blob *"Object storage to persist all incoming events, multi-modal inputs, and large exports."* Sources: `github.com/SigNoz/signoz` (LICENSE at `raw.githubusercontent.com/SigNoz/signoz/develop/LICENSE`), `github.com/grafana/docker-otel-lgtm`, `langfuse.com/self-hosting`.

### OTel GenAI semantic conventions: DEVELOPMENT (not Stable) as of July 2026

The conventions moved to a dedicated `open-telemetry/semantic-conventions-genai` repo. GenAI spans: *"Status: Development"* (`raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-spans.md`); GenAI metrics: *"Status: [Development][DocumentStatus]"* (same repo, `gen-ai-metrics.md`). Spans, metrics, and events are all still Development — none Stable. Coverage has broadened (inference, embeddings, retrievals, memory, tool-execution spans, so tool-call trees are representable via `execute_tool` INTERNAL spans), but you must opt in via `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` and **attribute names can still change**. Implication: insert a thin attribute-mapping layer between raw `gen_ai.*` spans and the durable UI schema.

### Embed Grafana or build custom views? Build custom.

Query-API assessment (all fetched):
- **Tempo HTTP API** (`grafana.com/docs/tempo/latest/api_docs/`): `GET /api/traces/<traceid>` returns OTel JSON; a **V2** endpoint adds an LLM-optimized trace format. **Strong enough** for tool-call-tree UIs.
- **ClickHouse SQL** (SigNoz/Langfuse stores): arbitrary joins, `parent_span_id` trees, conversation ordering in one language. **Strongest substrate.**
- **Prometheus HTTP API** (`prometheus.io/docs/prometheus/latest/querying/api/`): instant/range queries returning time-series matrices. **Flagged too weak** — metric tiles only, cannot build a run/conversation view on it.
- **Grafana embedding** (`grafana.com/blog/how-to-embed-grafana-dashboards-into-web-applications/`): iframe works only with `allow_embedding = true` (*"disables Grafana's default X-Frame-Options: DENY header"*) plus anonymous auth or a service-account token and `frame_origin` allow-listing. Native shared-dashboard links are read-only URL shares, not iframe panels.

**Recommendation:** build the core agent-run UI on **Langfuse's public API** (cleanest, versioned, authenticated), with **raw ClickHouse SQL as an escape hatch** for heavy custom aggregation. Use Grafana embeds only as disposable infra/ops dashboards. Because the store is Langfuse-native/ClickHouse, you get both the simplest single-store operational footprint and the strongest query substrate; gluing three AGPL LGTM stores together is the weaker fit for this product.

## Q2 — Eval storage

| Project | License (core) | Storage | Stores traces too? | API read-write | EE/commercial gating |
|---|---|---|---|---|---|
| **Langfuse** | **MIT (Expat)**; separate commercial license only for `ee/` dirs | Postgres + ClickHouse (scores/observations) + Redis + S3 | **Yes** — unified | **Strong.** Basic-auth public REST, full CRUD dataset-items/datasets/dataset-runs/scores | Datasets/scores/experiments/evals/tracing **all MIT-core**; EE gates only SCIM, audit logs, RBAC, retention, masking |
| **Arize Phoenix** | **Server = Elastic License 2.0**; client SDKs Apache-2.0 | SQLite default, Postgres supported | Yes | Moderate-good (OpenAPI upload/eval endpoints) | ELv2 forbids offering *"the software to third parties as a hosted or managed service"* — **disqualifying for a managed-service product** |
| **promptfoo** | MIT | Local SQLite (Drizzle) | No | **Weak** — CLI/test-runner, single-SQLite self-host, not a multi-client store | **Acquired by OpenAI (announced 2026-03-09)**, stays OSS. Architecturally forces a build. |
| **DeepEval** | Framework Apache-2.0 | Local JSON/CSV; real store = Confident AI cloud | No | **None** without the commercial SaaS | Framework OSS; Confident AI is closed commercial SaaS |

### Langfuse MIT-core-vs-EE split, quoted

The top-level `LICENSE` (`raw.githubusercontent.com/langfuse/langfuse/main/LICENSE`) makes everything outside `ee/`, `web/src/ee/`, `worker/src/ee/` **MIT**; the `ee/LICENSE` is a commercial key-gated license (*"may only be used, if you… have agreed to… a valid Langfuse Enterprise License"*). The eval features fall on the **free MIT side**: *"All product capabilities — tracing, evaluations, prompt management, experiments, annotation, the playground, and more — are MIT licensed without any usage limits"* (`langfuse.com/docs/open-source`). EE-gated list (verbatim): Project-level RBAC, Protected Prompt Labels, Data Retention Policies, Audit Logs, Server-Side Data Masking, UI Customization, Org Creators, Org Management API + SCIM, Instance Management API. Public API confirmed at `api.reference.langfuse.com`: *"All Langfuse data and features are available via the API"* — `POST /api/public/dataset-items` (upsert), datasets, dataset-runs, scores, `GET /api/public/v2/scores` filterable by `datasetRunId`.

### Recommendation

Phoenix (ELv2 hosting restriction) and DeepEval (no open self-hosted store) are structurally disqualified for a managed-service product; promptfoo is MIT but a local test-runner, not a store. The real decision is **Langfuse (adopt) vs plain Postgres in our API**. Adopt Langfuse if you want a unified, MIT, batteries-included trace+eval store with a strong CRUD API and can run its 4-datastore footprint. Build plain Postgres tables if traces are out of scope and you value schema ownership — but note Langfuse itself moved traces+scores to ClickHouse precisely because **Postgres is a poor engine for trace-scale volume**, so if high-volume tracing is in scope, plain Postgres alone is the wrong tool and Langfuse's case strengthens sharply. Given the product explicitly wants observability *and* evals-as-PR-checks, **adopt Langfuse**.

## Q3 — Reference Helm compositions

- **Langfuse (`langfuse/langfuse-k8s`, chart v1.5.37 / app v3.201.1):** single umbrella + 4 **Bitnami OCI subcharts** — `postgresql` (16.4.9), `clickhouse` (8.0.5), `valkey` (2.2.4, `alias: redis`), `minio` (14.10.5, `alias: s3`), each gated by `condition: <dep>.deploy`. BYO idiom: set `<dep>.deploy: false` then fill `host`/`port`/`auth`/`existingSecret` on the *same* block (verbatim comment: *"If you want to use an external Postgres server (or a managed one), set this to false"*). Documented minimum (app containers only): *"we recommend to use at least 2 CPUs and 4 GB of RAM for all containers"* (`langfuse.com/self-hosting/deployment/infrastructure/containers`). **Trap:** ClickHouse defaults to `replicaCount: 3` + `resourcesPreset: 2xlarge` (a 3-node cluster) — must override to `replicaCount: 1`, `clusterEnabled: false` for single-node. Source: `raw.githubusercontent.com/langfuse/langfuse-k8s/main/charts/langfuse/{Chart,values}.yaml`.
- **SigNoz (`SigNoz/charts`, v0.131.0):** single umbrella + own-repo subcharts (`clickhouse` 24.1.18, `postgresql` 0.0.2, optional `redpanda` for Kafka, optional `signoz-otel-gateway`); ZooKeeper is nested *inside* the ClickHouse subchart (`clickhouse.zookeeper.enabled`). BYO idiom differs: `clickhouse.enabled: false` + a dedicated `externalClickhouse:` block (splits DBs per signal). Published capacity table (scale guidance, not a floor): ClickHouse 16c/32Gi per shard, Core 4c/8Gi, Postgres 2c/8Gi, ZooKeeper 2c/8Gi. Small/local install ~4 vCPU / 8–16 GB. Source: `raw.githubusercontent.com/SigNoz/charts/main/charts/signoz/{Chart,values}.yaml`, `signoz.io/docs/setup/capacity-planning/community/resources-planning/`.
- **Grafana LGTM:** **no official umbrella.** `grafana/otel-lgtm` bundles *"the OpenTelemetry Collector, Prometheus (metrics), Tempo (traces), Loki (logs), Pyroscope (profiles), and Grafana into a single container"* — Prometheus not Mimir — and is *"intended for development, demo, and testing environments"* only. `kube-prometheus-stack` (v87.5.1) is the metrics-only umbrella (subcharts: grafana, kube-state-metrics, prometheus-node-exporter, all `.enabled`-gated). Full LGTM = compose loki + tempo + mimir-distributed + grafana as separate releases. Source: `github.com/grafana/docker-otel-lgtm`, `raw.githubusercontent.com/prometheus-community/helm-charts/main/charts/kube-prometheus-stack/Chart.yaml`.

Design takeaways: (1) both mature examples (Langfuse, SigNoz) use single-umbrella + `condition:`-per-dependency with first-class BYO toggles — copy this verbatim; (2) prefer Langfuse's single-block BYO idiom (`deploy` boolean + `existingSecret` everywhere) over SigNoz's split-block; (3) ClickHouse is the resource anchor — default single-replica or the footprint balloons; (4) **Bitnami caveat (July 2026):** Langfuse now pins `bitnamilegacy/*` image repos because Bitnami moved its free-image catalog — pin image repos explicitly and plan for that repo's instability if you depend on `oci://registry-1.docker.io/bitnamicharts`.

## Q4 — Slack dispatcher + agent runtime

**Slack dispatcher:** Slack Bolt (`slackapi/bolt-python` v1.29.0, `bolt-js`) is **MIT** (*"The MIT License (MIT) / Copyright (c) 2020- Slack Technologies, LLC"*) and is the real self-hostable substrate (Events API, Socket Mode, app-mention handling, the `Assistant` side-panel class). Slack does **not** ship an OSS "Agent Kit" — "AI in Slack" / Agents & Assistants is a **Slack-hosted platform feature** (split-view container, agent tab, text streaming) that renders in the user's client; only the thin `Assistant` glue class is OSS-in-Bolt. For a firewalled single-node self-host, **Socket Mode is the intended path** (*"without exposing a public HTTP Request URL"*), but Slack's own guidance is *"To have the highest possible reliability for application connectivity, we recommend using HTTP for production applications"* — so default Socket Mode with robust reconnect supervision on-prem, and offer Events API (HTTP) on the cloud tier. Sources: `github.com/slackapi/bolt-python` + LICENSE, `docs.slack.dev/ai/`, `docs.slack.dev/apis/events-api/comparing-http-socket-mode/`.

**Agent execution:** Claude Agent SDK (`anthropics/claude-agent-sdk-python` v0.2.110) is **MIT code** (usage under Anthropic Commercial ToS), bundles the Claude Code CLI, runs **headless in a container** — the payload. It needs an isolation host: on-prem = **kubernetes-sigs/agent-sandbox** `Sandbox` CRD (Apache-2.0; *"isolated, stateful, singleton workloads, ideal for… AI agent runtimes with untrusted code execution"*) — but the README carries **no alpha/beta/GA label at v0.5.0**, so architect it as pre-1.0 with a plain-K8s-Job fallback. Cloud tier = **AWS Bedrock AgentCore** (GA *"Posted on: Oct 13, 2025"*, *"eight-hour execution windows and complete session isolation"*, AWS-proprietary). Flag: Unit 42 disclosed (Apr 2026) a sandbox DNS/S3 egress issue in AgentCore Code Interpreter (AWS remediated) — relevant if isolation guarantees are load-bearing.

**Concurrency/queue:** a Redis-backed queue is **effectively forced**, not optional. Slack requires event ack within ~3s while agent runs take seconds-to-minutes, so the dispatcher must ack immediately and hand off. Bolt handles the fast in-process ack; put every agent run on **BullMQ (Redis, if TS/bolt-js)** or **Celery (Redis, if Python/bolt-python)** for retries, priority, and independent scaling. Worker concurrency caps pair naturally with agent-sandbox's `SandboxWarmPool`. This reuses the Valkey that Langfuse already bundles.

## Proposed umbrella Helm chart layout

```
agent-platform/                         # umbrella chart
├── Chart.yaml                          # deps, each with condition: <name>.deploy
├── values.yaml                         # global toggles + BYO blocks (Langfuse-style single-block idiom)
├── templates/
│   ├── api-server/                     # BUILD: Deployment + Service + HPA (owns app-state schema, git-flow)
│   ├── web-ui/                         # BUILD: Deployment + Service + Ingress
│   ├── slack-dispatcher/               # BUILD: Deployment (Socket Mode -> no Ingress); Bolt app + queue producer
│   ├── agent-runner/                   # BUILD: SandboxTemplate (agent-sandbox) OR Job controller (fallback)
│   ├── worker/                         # BUILD: BullMQ/Celery consumers -> spawn agent runs
│   └── otel-collector/                 # optional OTLP collector config -> Langfuse
└── charts/  (dependencies)
    ├── langfuse            # condition: langfuse.deploy  (adopt; transitively brings clickhouse, valkey, minio, postgres)
    ├── postgresql          # condition: postgresql.deploy  (app state; BYO or reuse Langfuse's instance, separate DB)
    ├── valkey              # condition: redis.deploy  (dispatcher queue; BYO or reuse Langfuse's)
    └── kube-prometheus-stack  # condition: infraMetrics.enabled  (OPTIONAL: infra/ops dashboards only, off by default)
```

BYO everything via `<dep>.deploy: false` + `host`/`auth`/`existingSecret`. A `docker-compose` dev profile mirrors this: Langfuse compose + our api/ui/dispatcher/worker + a shared Redis.

**Minimal single-node resource estimate:** ClickHouse (via Langfuse) is the floor. With single-replica ClickHouse, Langfuse web+worker, single-node Postgres/Valkey/MinIO: **~6–8 vCPU / ~16 GB** just for the Langfuse backbone. Add our services (api+ui+dispatcher+worker ~1.5 vCPU / ~2 GB) and bursty agent-runner pods (~1–2 vCPU / ~2–4 GB per concurrent run). Realistic single-node dev/demo target: **~8–10 vCPU / ~20 GB RAM**. Cheapest shrink: BYO managed Postgres + real S3 (drop MinIO), run only ClickHouse + Langfuse app + our services in-cluster. A true docker-compose "laptop dev mode" is feasible if you cap ClickHouse to single-replica and gate agent concurrency low.

## Open questions / risks

1. **OTel GenAI semconv is still Development** — `gen_ai.*` attribute names can change. Do not hard-code them into a durable UI schema; build a thin mapping layer. Affects whichever store you pick.
2. **agent-sandbox is pre-1.0** (no maturity label at v0.5.0). Don't represent it as production-hardened; ship a plain-K8s-Job + gVisor/Kata fallback for isolation, and gate the CRD path behind a values toggle.
3. **Bitnami catalog disruption** — Langfuse pins `bitnamilegacy/*` images. Any dependency on `oci://registry-1.docker.io/bitnamicharts` inherits this instability; pin image repos explicitly and monitor. Consider vendoring our own subcharts long-term (SigNoz's approach).
4. **Langfuse coupling** — verify the features we use (datasets/scores/experiments/tracing) stay MIT-core on Langfuse's roadmap; today they are, EE gates only admin/security. Also confirm Langfuse's observation model (parent-observation linkage) cleanly reconstructs the tool-call-tree UI shape before committing the UI to its API — the one "is the API strong enough" item worth a spike.
5. **Langfuse is LLM-trace-focused, not infra observability.** If host/pod metrics and logs are in scope, we still need the optional `kube-prometheus-stack` (or Loki) leg; Langfuse won't cover it. Embed those Grafana dashboards; don't build them.
6. **Socket Mode reliability** — Slack recommends HTTP for production. On-prem Socket Mode needs supervised auto-reconnect; the cloud tier should default to Events API.
7. **MinIO is AGPLv3** — fine for a bundled default, but flag it for any customer sensitive to AGPL in a redistributed product; the BYO-S3 toggle sidesteps it.

## Related

[[supabase-for-agents]], [[curietech-agent-os]], [[agent-deployment-model]], [[agent-leave-behind-platform]], `claude-design-prompt.md`
