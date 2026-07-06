"""OTel tracing for the runner: gen_ai spans exported OTLP-HTTP to the collector.

Productizes the PT-4/PT-E prototype span shape. Each turn is a root ``agent.run``
(SERVER) span carrying a ``langfuse.trace.name``, with a child ``llm.generation``
span holding ``gen_ai.request.model`` and ``gen_ai.usage.*`` token counts, plus a
child ``execute_tool`` span per tool call (``gen_ai.tool.name`` /
``gen_ai.operation.name``). Langfuse maps a model-bearing span to a generation and
nests tool spans as observations, so this reconstructs the tool-call tree (S1).

Traces go to the OTel Collector over OTLP-HTTP, never directly to Langfuse: the
collector is the adapter that authenticates and forwards (Langfuse OTLP ingest is
HTTP-only). Endpoint/headers come from the standard ``OTEL_EXPORTER_OTLP_*`` env
vars via ``SessionConfig.otel``; the exporter is constructed argument-free so the
opentelemetry SDK's own env parsing applies (it appends ``/v1/traces`` to a base
``OTEL_EXPORTER_OTLP_ENDPOINT``). When no endpoint is configured the tracer is a
no-op, so unit tests and offline runs neither export nor fail.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from aci_protocol import OtelConfig
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanKind, Tracer

_SERVICE_NAME = "agentos-runner"


def build_tracer_provider(
    otel: OtelConfig, session_id: str, sandbox_id: str | None = None
) -> TracerProvider | None:
    """Build a TracerProvider exporting to the collector, or None if unconfigured.

    ``session_id`` is attached as a resource attribute so traces are attributable
    to the sandbox session that produced them. ``sandbox_id`` (the ACI
    ``AGENTOS_SANDBOX_ID``) is stamped alongside it when present so a trace is
    attributable to the concrete sandbox that ran it; an absent or empty value is
    omitted rather than stamped as an empty string.
    """

    if not otel.endpoint:
        return None

    attributes: dict[str, str] = {
        "service.name": _SERVICE_NAME,
        "agentos.session_id": session_id,
    }
    if sandbox_id:
        attributes["agentos.sandbox_id"] = sandbox_id
    resource = Resource.create(attributes)
    provider = TracerProvider(resource=resource)
    # The exporter reads OTEL_EXPORTER_OTLP_ENDPOINT / _HEADERS / _PROTOCOL from
    # the environment itself; SessionConfig.otel is the typed view of the same
    # vars, so an argument-free exporter and the config agree by construction.
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter()))
    return provider


class RunTracer:
    """Thin wrapper over an OTel tracer emitting the runner's gen_ai span tree.

    A None provider yields a no-op tracer so callers need no branching.
    """

    def __init__(self, provider: TracerProvider | None) -> None:
        self._provider = provider
        self._tracer: Tracer = (
            provider.get_tracer("agentos-runner")
            if provider is not None
            else trace.get_tracer("agentos-runner")
        )

    @contextmanager
    def run_span(self, trace_name: str, model: str | None) -> Iterator[_GenerationSpan]:
        """Open the root ``agent.run`` span and its child ``llm.generation`` span."""

        with self._tracer.start_as_current_span("agent.run", kind=SpanKind.SERVER) as root:
            root.set_attribute("langfuse.trace.name", trace_name)
            with self._tracer.start_as_current_span("llm.generation") as gen:
                span = _GenerationSpan(self._tracer, gen)
                # Stamp the configured model at span open when AGENTOS_MODEL is
                # set; otherwise the span stays model-less until the SDK reports
                # the actual model on its first assistant message (record_model).
                span.record_model(model)
                yield span

    def shutdown(self) -> None:
        """Flush and shut down the exporter if one was configured."""

        if self._provider is not None:
            self._provider.shutdown()


class _GenerationSpan:
    """Handle for annotating the generation span and emitting tool child spans."""

    def __init__(self, tracer: Tracer, span: Any) -> None:
        self._tracer = tracer
        self._span = span
        self._model_recorded = False

    def record_model(self, model: str | None) -> None:
        """Stamp the generation model attribute once, first non-empty value wins.

        Langfuse only maps ``llm.generation`` to a GENERATION observation (and so
        records the ``gen_ai.usage.*`` token counts) when the span carries a model
        attribute; a model-less span ingests as an untyped SPAN with zero usage.
        The configured ``AGENTOS_MODEL`` is stamped at span open when set; when it
        is unset the runner backfills the actual model the SDK reports on its first
        assistant message, so the generation is typed either way. Only genuinely
        unknown models leave the attribute absent.
        """

        if self._model_recorded or not model:
            return
        self._span.set_attribute("gen_ai.request.model", model)
        self._span.set_attribute("model", model)
        self._model_recorded = True

    def record_usage(self, usage: Mapping[str, Any] | None) -> None:
        """Attach gen_ai token-usage attributes from an SDK usage mapping."""

        if not usage:
            return
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                self._span.set_attribute(f"gen_ai.usage.{key}", value)

    def tool_span(self, tool_name: str) -> None:
        """Emit a short ``execute_tool`` child span for one tool call."""

        with self._tracer.start_as_current_span("execute_tool") as tool:
            tool.set_attribute("gen_ai.tool.name", tool_name)
            tool.set_attribute("gen_ai.operation.name", "execute_tool")
