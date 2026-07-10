# AgentOS Interface Catalog (Swappable Seams)

These files document the "strong black lines" that already exist in the code — the
seams where one curated default component can be swapped without rewriting the system.
The governing restraint is
[**"the second implementation teaches the interface"**](architecture-vision.md): we do not
write speculative adapter layers ahead of a real second implementation. Each `INTERFACE.md`
is documentation of where the code already draws the line, not a new abstraction.

## The seams

| Seam | Kind | Impls | Grade | Epic(s) | INTERFACE.md |
|---|---|---|---|---|---|
| Substrate / SandboxClient | CLEAN | 2 (k8s, docker) | not separately graded | #86, #44 | [interfaces/substrate/INTERFACE.md](interfaces/substrate/INTERFACE.md) |
| Harness in-proc / ModelSession | CLEAN | 1 + fake | A- | (folds into #25) | [interfaces/harness-modelsession/INTERFACE.md](interfaces/harness-modelsession/INTERFACE.md) |
| ACI producer (frozen protocol) | CLEAN, frozen | 1 + reference | A- | #25, #47 | [interfaces/aci-producer/INTERFACE.md](interfaces/aci-producer/INTERFACE.md) |
| Channel / ingress (Slack) | SOFT | 1 | C | #7, #19, #27, #38 | [interfaces/channel-ingress/INTERFACE.md](interfaces/channel-ingress/INTERFACE.md) |
| Model provider / credentials | SOFT | 1 (Anthropic) | not separately graded | #24, #46 | [interfaces/model-provider/INTERFACE.md](interfaces/model-provider/INTERFACE.md) |
| Telemetry / OTEL | SOFT | 1 | B+ | #47 | [interfaces/telemetry-otel/INTERFACE.md](interfaces/telemetry-otel/INTERFACE.md) |
| Evals (case + scorer) | SOFT | 1 grader family | B | #8, #26 | [interfaces/evals/INTERFACE.md](interfaces/evals/INTERFACE.md) |
| Blob storage (S3/MinIO) | SOFT | 2 concretes | B+ | #83 | [interfaces/blob-storage/INTERFACE.md](interfaces/blob-storage/INTERFACE.md) |
| Relational DB (Postgres) | SOFT | 1 | A- | #84 | [interfaces/relational-db/INTERFACE.md](interfaces/relational-db/INTERFACE.md) |
| Queue / stream (Valkey) | SOFT | 1 (redis-py) | not separately graded | #85, #7 | [interfaces/queue-stream/INTERFACE.md](interfaces/queue-stream/INTERFACE.md) |
| Bundle format | CLEAN, frozen | 1 | not separately graded | #30 | [interfaces/bundle-format/INTERFACE.md](interfaces/bundle-format/INTERFACE.md) |
| Approval / authorizer | NONE | 0 | not separately graded | #22 | [interfaces/approval/INTERFACE.md](interfaces/approval/INTERFACE.md) |
| Workflow state store | NONE | 0 (concrete AffinityStore) | not separately graded | #23 | [interfaces/workflow-state/INTERFACE.md](interfaces/workflow-state/INTERFACE.md) |
| Memory | NONE (field only) | 0 loaders | not separately graded | #28 | [interfaces/memory/INTERFACE.md](interfaces/memory/INTERFACE.md) |
| Triggers | SOFT | 2 hardcoded (Slack, GH push) | not separately graded | #29 | [interfaces/triggers/INTERFACE.md](interfaces/triggers/INTERFACE.md) |

## Kind legend

- **CLEAN** — a real `Protocol` / typed port class draws the line in code.
- **SOFT** — the swap happens through env vars, a URL/endpoint, a key prefix, or a wire
  payload; there is no code interface, and that is a deliberate decision, not an omission.
- **NONE** — the seam is not built yet; the file records the intended line and the placement
  constraint a future implementation must honor.

Seams not in the six-job Swap-readiness grade table below (substrate, model provider, queue,
bundle format, approval, workflow-state, memory, triggers) are **not separately graded**.

## Swap-readiness grade

Reproduced verbatim from the "Swap readiness" section of
[architecture-vision.md](architecture-vision.md). Only the six production-platform jobs are
graded here.

| Job | Port contract | Current adapter | Grade | Cheapest next step |
|---|---|---|---|---|
| Harness / runtime | Frozen ACI protocol (`packages/aci-protocol`), tri-language, CI-guarded | claude-agent-sdk runner | A-: strongest seam in the system; docked for the plugin-format entanglement and SDK-shaped resume | Write the "implement an ACI server" guide from the conformance suite so the port is documented, not just enforced |
| Observability | OTLP to collector (write), API DTOs (read) | Langfuse behind `langfuse.py` | B+: write side clean but for one vendor span attribute; read side isolated in one module | Rename `langfuse.trace.name` to a neutral attribute mapped in the collector; rename the `/langfuse/*` API routes |
| Evals | Our stream schema + `EvalMatrix` DTO; store behind recorder | Langfuse traces + `eval_pass` scores | B: schema is ours, but the case format is duplicated and the tag convention is unfrozen | Converge the two `cases.json` definitions into one frozen schema ([issue #8](https://github.com/curie-eng/agentos/issues/8)) |
| Blob storage | S3 protocol (boto3 + mc, path-style, endpoint-configurable) | MinIO | B+: config-only within S3-compatible stores; three hand-aligned client sites; no interface for non-S3 | None needed until a non-S3 demand exists; document the three client sites as one seam |
| Relational DB | SQLAlchemy 2.0 + alembic | Postgres | A-: managed-Postgres swap is a DSN change; two Postgres-isms in models | Leave as is; note the `postgresql.UUID` and schema-scoped enum as the two things a non-Postgres target would touch |
| Communication | `QueuedSlackEvent` + `SlackSink` | Slack (Bolt + chat.update) | C+: payload promoted into `aci-protocol` (ADR-0020) so the shape is now the generated cross-language contract; names still Slack-shaped and edit-in-place semantics remain in the core's contract | Land the PR-B channel-neutral field rename (`conversation_id`/`reply_handle`) on the promoted model |
