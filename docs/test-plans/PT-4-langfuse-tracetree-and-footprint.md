# PT-4 — Langfuse trace/eval backbone: tool-tree reconstruction + OTLP ingest + single-node footprint

Settles **R3** (Langfuse API cannot reconstruct the tool-call tree) — this **is** the plan's spike **S1** (`detailed-architecture.md:356`, `on-prem-architecture.md:121`) — and captures **R6** (single-node footprint) for free. Lowest-risk, highest-feasibility of the four; needs **no cluster and no Anthropic key**, so it should run first. Companion: `../analysis/agent-os-prototype-derisking-review.md` §3 R3/R6.

**Run status: RUN on the local box via docker compose, 2026-07-04. Verdict: GO — all three claims PASS.** Live evidence in §Live results below.

## Live results (local docker, Langfuse chart stack, 2026-07-04)

Ran the official Langfuse self-host `docker compose` (web + worker + clickhouse + postgres + redis + minio).

- **Claim 1 (OTLP-HTTP ingest → generation mapping): PASS.** Emitted a synthetic trace via the OpenTelemetry Python SDK to `http://localhost:3000/api/public/otel/v1/traces` over **HTTP/protobuf** with Basic auth. A span carrying `model=claude-opus-4-8` was ingested and surfaced as observation **`type=GENERATION`**, `model=claude-opus-4-8`, `usageDetails={input:1200, output:88, total:1288}`. The two `execute_tool` spans surfaced as `type=TOOL`. Async ingestion latency ~8s (worker queue).
- **Claim 2 (tool-tree reconstruction via public API): PASS — THE key S1/R3 question.** `GET /api/public/observations?traceId=<id>` returned every observation with a populated **`parentObservationId`**, and the 3-level tree reconstructed cleanly:
  ```
  SPAN: agent.run
    GENERATION: llm.generation
      TOOL: search_repo
      TOOL: write_file
  ```
  So the Runs-view tool-call tree is buildable directly on the Langfuse public API — no ClickHouse SQL fallback needed. R3 is a GO.
- **Claim 3 (single-node footprint): PASS, and the estimate was way high.** Total backbone RSS at light load = **2.28 GiB** (web 1.28 GiB, worker 445 MiB, clickhouse 453 MiB, minio 63 MiB, postgres 61 MiB, redis 6 MiB). This is ~1/8 of the on-prem doc's ~16-20 GB estimate. **ClickHouse is NOT the memory hog** — a standalone `clickhouse:24.8` idled at 142 MiB and peaked at 391 MiB under a 1M-row insert+aggregation. The doc's footprint fear is the **Helm 3-replica/2xlarge default preset**, not ClickHouse itself; a single-replica ClickHouse is cheap.
- **NEW on-prem finding (CPU baseline): current ClickHouse requires AVX.** The Langfuse compose pins untagged `clickhouse/clickhouse-server` (→ latest), which **SIGILL-crashed (exit 132) on this host because the CPU only has `sse4_2`, no AVX/AVX2.** Pinning `clickhouse/clickhouse-server:24.8` (still ships SSE4.2 builds) fixed it. **Implication for the leave-behind:** an on-prem customer on an older/constrained CPU (or a CPU-limited VM/LXC) needs a pinned ClickHouse ≤24.8 or a build with the SSE4.2 baseline. Add a CPU-feature preflight to the Helm chart. This is exactly the kind of "runs on one commodity node" caveat worth catching before a customer install.

Cleanup done: `docker compose down -v` + image removal; box returned to prior free memory.

## Objective

Prove three things about the adopted observability/eval backbone before the UI (H1) and eval matrix (K1) are built on it:

1. **OTLP ingest works over HTTP** (NOT gRPC — verified 2026-07-04 that Langfuse rejects gRPC OTLP), and a span carrying `gen_ai.*` / a `model` attribute becomes a `generation` observation.
2. **The public API reconstructs a ≥3-level tool-call tree** for a trace via `parentObservationId` linkage — the exact shape the Runs view renders. This is the "is the API strong enough" item the on-prem doc flags as the one worth a spike.
3. **The single-node footprint is real:** capture actual resident memory/CPU of the Langfuse backbone (Postgres + ClickHouse + Valkey + MinIO + web/worker) so the "8-10 vCPU / ~20 GB one node" claim (`:14`) is evidence-backed, and confirm the **ClickHouse 3-replica/2xlarge default trap** is overridden to single-replica.

## Environment

- Local `docker` (present at `/bin/docker`) is sufficient. Use the official Langfuse self-host **docker compose** (v3 stack) for the ingest+API test; use the **helm chart v1.5.37 / app v3.201.1** on `kind`/scratch only if you also want to measure the *k8s* footprint (optional; compose measures the same components).
- No Anthropic key, no scratch cluster required for claims 1-2. Claim 3's *k8s* footprint variant wants a cluster; the compose variant measures container RSS locally.

## Setup

```bash
# Langfuse self-host via docker compose (single-node dev profile).
git clone https://github.com/langfuse/langfuse.git /tmp/langfuse-pt4 && cd /tmp/langfuse-pt4
docker compose up -d          # brings up web, worker, postgres, clickhouse, redis(valkey), minio
# wait for web healthy, then create an org/project in the UI (localhost:3000) and mint API keys:
#   LF_PK=pk-lf-...  LF_SK=sk-lf-...
```

## Commands / evidence capture

```bash
# --- Claim 1 & 2: emit a synthetic nested trace via OTLP-HTTP, then read it back ---
# Use a tiny OTel SDK script (python) that builds a root span + child generation span +
# two grandchild execute_tool spans, so the tree is 3 levels deep:
#
#   root: "agent.run"  (SpanKind SERVER)  -> generation: gen_ai.request.model=claude-...  (attr `model`)
#        -> execute_tool: gen_ai.tool.name=search
#        -> execute_tool: gen_ai.tool.name=write_file
#
# Exporter config (the load-bearing gotcha):
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:3000/api/public/otel
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf          # NOT grpc — Langfuse rejects gRPC
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic $(printf '%s:%s' $LF_PK $LF_SK | base64 -w0)"
export OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental
python3 emit_nested_trace.py    # (the ~40-line script above)

# Read it back through the public API and reconstruct the tree:
TRACE=<traceId printed by the script>
curl -s -u $LF_PK:$LF_SK "http://localhost:3000/api/public/traces/$TRACE" | python3 -m json.tool
curl -s -u $LF_PK:$LF_SK "http://localhost:3000/api/public/observations?traceId=$TRACE" \
  | python3 -c 'import sys,json; obs=json.load(sys.stdin)["data"];
import collections; kids=collections.defaultdict(list)
[kids[o.get("parentObservationId")].append(o) for o in obs]
def show(pid,d=0):
  for o in kids[pid]: print("  "*d + f"{o[\"type\"]}: {o.get(\"name\")}"); show(o[\"id\"],d+1)
show(None)'
# EVIDENCE: the printed tree shows generation -> execute_tool x2 nesting via parentObservationId.
# Also confirm the model-bearing span became type=GENERATION.

# --- Claim 3: footprint ---
docker stats --no-stream    # RECORD mem/cpu per container; sum the backbone
docker compose ps
# Confirm ClickHouse is single-replica (compose default is 1; the HELM default is the 3x trap —
# if measuring the helm variant, verify values override clickhouse.replicaCount=1, clusterEnabled=false).
```

## Expected evidence

- **GO (expected):** OTLP-HTTP ingest works; the model span is a `generation`; the observations endpoint returns `parentObservationId` on children and the script prints a clean 3-level tree; backbone RSS is captured (expect ClickHouse to dominate). This confirms the whole product surface can be built on the Langfuse public API.
- **PARTIAL:** ingest works but parent linkage is flattened (children have null `parentObservationId`) → the Runs-view tree must be reconstructed from ClickHouse SQL instead (the on-prem doc's named fallback, `on-prem-architecture.md:57`); record GO-with-fallback.
- **NO-GO (unlikely):** OTLP-HTTP rejected or no generation mapping → re-check the endpoint path/protocol/auth before concluding; this is almost always a config error, not a Langfuse limitation.

## Failure signals

- Trace never appears → wrong protocol (gRPC instead of http/protobuf), missing root span (Langfuse "requires a root span or the trace is malformed"), or bad Basic auth header. Fix in that order.
- ClickHouse container OOMs / eats the box → the 3-replica/2xlarge trap; override to single-replica. This is itself the R6 evidence.
- `parentObservationId` present in ingest but absent in API read → a Langfuse version behavior; note the app version (v3.201.1 target) since the S1 GO/NO-GO is version-sensitive.

## Cleanup

```bash
cd /tmp/langfuse-pt4 && docker compose down -v   # -v drops the volumes (throwaway)
rm -rf /tmp/langfuse-pt4
```

## Timebox

**0.5 day.** Mostly compose bring-up + the 40-line emitter. If compose is heavy on this box, the ingest+API claims can be tested against Langfuse Cloud's free tier at `/api/public/otel` (same API), sacrificing only the footprint measurement.

## Blocker

**None on capability; one resource caveat.** Runnable with local `docker` (present) OR on the `k8scratch` k3s cluster (confirmed available). Caveat checked 2026-07-04: this host has ~8 GB RAM free with other bg jobs live, and the full Langfuse backbone (ClickHouse-anchored) estimates ~16 GB — running the full compose here risks OOMing a shared box, so it was **deliberately not run in this pass** to avoid thrashing the machine. Run it when the box is quiet, or point OTLP ingest + API claims (1-2, the actually-uncertain half) at Langfuse Cloud's free tier (`/api/public/otel`, same API) and skip only the local footprint measurement. This should still be an early run — it needs no Anthropic key and de-risks the entire read-side product surface. Note the OTLP protocol gotcha (HTTP/protobuf, not gRPC) verified 2026-07-04 is already baked into the commands above.
