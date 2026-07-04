import base64, os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.trace import SpanKind

auth = base64.b64encode(b"pk-lf-pt4-public:sk-lf-pt4-secret").decode()
exp = OTLPSpanExporter(
    endpoint="http://localhost:3000/api/public/otel/v1/traces",
    headers={"Authorization": f"Basic {auth}"},
)
prov = TracerProvider(resource=Resource.create({"service.name": "pt4-agent"}))
prov.add_span_processor(SimpleSpanProcessor(exp))
trace.set_tracer_provider(prov)
tr = trace.get_tracer("pt4")

# 3-level tree: agent.run (root) -> generation (model span) -> 2x execute_tool
with tr.start_as_current_span("agent.run", kind=SpanKind.SERVER) as root:
    root.set_attribute("langfuse.trace.name", "pt4-tooltree-demo")
    with tr.start_as_current_span("llm.generation") as gen:
        gen.set_attribute("gen_ai.request.model", "claude-opus-4-8")
        gen.set_attribute("model", "claude-opus-4-8")   # Langfuse maps model-bearing span -> generation
        gen.set_attribute("gen_ai.usage.input_tokens", 1200)
        gen.set_attribute("gen_ai.usage.output_tokens", 88)
        with tr.start_as_current_span("execute_tool") as t1:
            t1.set_attribute("gen_ai.tool.name", "search_repo")
            t1.set_attribute("gen_ai.operation.name", "execute_tool")
        with tr.start_as_current_span("execute_tool") as t2:
            t2.set_attribute("gen_ai.tool.name", "write_file")
            t2.set_attribute("gen_ai.operation.name", "execute_tool")
    print("emitted trace_id:", format(root.get_span_context().trace_id, "032x"))
prov.shutdown()
