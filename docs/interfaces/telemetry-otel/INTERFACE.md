---
seam: Telemetry / OTEL
kind: SOFT
impls: 1
grade: B+
epics:
  - "#47"
order: 7
---
# INTERFACE: Telemetry / OTEL

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
<!-- BEGIN GENERATED: header (agentos dev docs-lint) -->
> **Kind:** SOFT &nbsp;·&nbsp; **Implementations today:** 1 &nbsp;·&nbsp; **Swap-readiness grade:** B+
<!-- END GENERATED: header -->

**Kind legend:** CLEAN = a real `Protocol`/typed port class · SOFT = swap via env/URL/prefix/wire, no code interface · NONE = not built yet.

## The black line

On the write side, observability is swapped at the OTLP wire, not in code: the runner
exports `gen_ai.*` spans over OTLP-HTTP to a collector, and the collector is the only
component that authenticates and forwards to a backend. Services never speak the
backend directly. Swapping the trace store means repointing one collector exporter
block, not touching the runner. The opinionated core is the span tree shape
(`agent.run` → `llm.generation` → `execute_tool`) and the `gen_ai` semantic-convention
attributes on it.

## Current contract

A second backend must ingest the OTLP-HTTP export produced in
`runner/src/agentos_runner/otel.py`:

- `build_tracer_provider` (`runner/src/agentos_runner/otel.py::build_tracer_provider`) returns a `TracerProvider` wired with an
  argument-free `OTLPSpanExporter()` over a `SimpleSpanProcessor`, or
  `None` when unconfigured (so offline runs neither export nor fail).
- Endpoint/headers come from the standard `OTEL_EXPORTER_OTLP_*` env vars, read by the
  opentelemetry SDK itself inside `build_tracer_provider`; `SessionConfig.otel` is the typed view of
  the same vars.
- `RunTracer.run_span` (`runner/src/agentos_runner/otel.py::RunTracer.run_span`) opens the root `agent.run` (`SpanKind.SERVER`)
  and a child `llm.generation` span. `_GenerationSpan.record_model`
  stamps `gen_ai.request.model` (`runner/src/agentos_runner/otel.py::_GenerationSpan.record_model`), `record_usage` stamps
  `gen_ai.usage.input_tokens` / `output_tokens` (`runner/src/agentos_runner/otel.py::_GenerationSpan.record_usage`), and `tool_span` emits
  an `execute_tool` child carrying `gen_ai.tool.name` and `gen_ai.operation.name`
  (`runner/src/agentos_runner/otel.py::_GenerationSpan.tool_span`).

## Implementations today

One: Langfuse, reached through the OTel Collector (which authenticates and forwards,
since Langfuse OTLP ingest is HTTP-only). The runner does not know it is Langfuse — it
only knows OTLP. The read side (trace list, tree reconstruction) is a separate concern in
the API — Langfuse's query model spans several API modules plus routers, not one isolated
module — and is out of scope for this write-side seam.

## Known leakage

The write path is clean but for three vendor-named attributes, all set at the source on
the root span rather than mapped in the collector. `RunTracer.run_span` stamps
`langfuse.trace.name`, `langfuse.session.id`, and `langfuse.user.id`
(`runner/src/agentos_runner/otel.py::RunTracer.run_span`) so Langfuse maps them to its
name/Sessions/Users features. A clean seam would emit neutral attributes the collector
maps to the vendor names; today all three vendor names are set at the source.

## Cross-links

- **Epic(s):** #47 — extends the observability / telemetry write path.
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — Job 2 (Observability / OTel store), grade B+.
- **ADR(s):** [ADR-0004](../../adr/0004-langfuse-observability-and-eval-backbone.md) — Langfuse as the single observability + eval backbone (OTLP over HTTP/protobuf to the collector).
