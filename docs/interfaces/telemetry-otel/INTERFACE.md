# INTERFACE: Telemetry / OTEL

> Part of the AgentOS swappable-seam catalog — see the [seam index](../../interfaces.md).
> **Kind:** SOFT &nbsp;·&nbsp; **Implementations today:** 1 &nbsp;·&nbsp; **Swap-readiness grade:** B+

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

- `build_tracer_provider` (`otel.py:36`) returns a `TracerProvider` wired with an
  argument-free `OTLPSpanExporter()` over a `SimpleSpanProcessor` (`otel.py:62`), or
  `None` when unconfigured (`otel.py:48`, so offline runs neither export nor fail).
- Endpoint/headers come from the standard `OTEL_EXPORTER_OTLP_*` env vars, read by the
  opentelemetry SDK itself (`otel.py:59`); `SessionConfig.otel` is the typed view of
  the same vars.
- `RunTracer.run_span` (`otel.py:80`) opens the root `agent.run` (`SpanKind.SERVER`,
  `otel.py:98`) and a child `llm.generation` span. `_GenerationSpan.record_model`
  stamps `gen_ai.request.model` (`otel.py:141`), `record_usage` stamps
  `gen_ai.usage.input_tokens` / `output_tokens` (`otel.py:153`), and `tool_span` emits
  an `execute_tool` child carrying `gen_ai.tool.name` and `gen_ai.operation.name`
  (`otel.py:159`).

## Implementations today

One: Langfuse, reached through the OTel Collector (which authenticates and forwards,
since Langfuse OTLP ingest is HTTP-only). The runner does not know it is Langfuse — it
only knows OTLP. The read side (trace list, tree reconstruction) is a separate adapter
in the API and out of scope for this write-side seam.

## Known leakage

The write path is clean but for one vendor attribute: the root span carries
`langfuse.trace.name` (`otel.py:99`), a Langfuse-named attribute on an otherwise
neutral OTLP path. `run_span` similarly stamps `langfuse.session.id` (`otel.py:101`)
and `langfuse.user.id` (`otel.py:103`) so Langfuse maps them to its Sessions/Users
features. A clean seam would emit a neutral attribute the collector maps to the
vendor name; today the vendor name is set at the source.

## Cross-links

- **Epic(s):** #47 — extends the observability / telemetry write path.
- **Vision doc:** [architecture-vision.md](../../architecture-vision.md) — Job 2 (Observability / OTel store), grade B+.
- **ADR(s):** [ADR-0004](../../adr/0004-langfuse-observability-and-eval-backbone.md) — Langfuse as the single observability + eval backbone (OTLP over HTTP/protobuf to the collector).
