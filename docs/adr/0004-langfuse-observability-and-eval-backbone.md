# 4. Langfuse as the single observability + eval backbone

Date: 2026-07-04
Status: Accepted

## Context

Curie needs traces (the Runs view), an eval store (evals-as-CI, the eval matrix), and per-run version reproducibility — the production-discipline layer that is the product's moat. The candidates were Langfuse (MIT core), SigNoz (MIT, generic OTel), Arize Phoenix (Elastic License 2.0 — disqualified for a managed-service product), promptfoo/DeepEval (no multi-client self-hosted store). The open risk with Langfuse was whether its public API is strong enough to build our own Runs view on, and whether its 4-datastore footprint fits one node.

## Decision

Adopt Langfuse (self-hosted, v3) as the single backbone for both agent traces and eval storage. Build the Runs view on its public API, with raw ClickHouse SQL as an escape hatch for heavy aggregation. Emit spans via OTLP over **HTTP/protobuf** (not gRPC). Insert a thin mapping layer between raw `gen_ai.*` OTel attributes and our durable UI schema (the GenAI semconv is still Development status and can change).

## Evidence (live, docker compose, 2026-07-04)

- OTLP-HTTP ingest works; a `model`-bearing span maps to a `GENERATION` observation; the public `observations` API returns populated `parentObservationId` linkage that reconstructs a 3-level `agent.run → generation → tool` tree.
- Proven again from inside a sandbox: the runner exported nested traces (`service.name=curie-runner-sandbox`) that reconstructed correctly.
- Footprint measured at **~2.3 GB** for the whole backbone at light load (web 1.28 GB, ClickHouse 453 MiB, rest tiny) — far below the 16-20 GB estimate. ClickHouse is not the memory anchor; the Helm 3-replica/2xlarge default is (override to 1).
- gRPC OTLP is silently unsupported by Langfuse — HTTP only.

## Consequences

- **CPU baseline gotcha:** current ClickHouse requires AVX and SIGILL-crashes on SSE4.2-only hosts. The chart must pin a SSE4.2-compatible ClickHouse (≤24.8) or add a CPU-feature preflight for constrained on-prem hardware.
- The ACI's `OTEL_EXPORTER_OTLP_*` must be configured for HTTP/protobuf; a gRPC misconfig silently drops all traces.
- Eval-grade observability needs per-run version reproducibility (stamp a config hash per run) — a schema decision that lands with the eval lane.
