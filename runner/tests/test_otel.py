"""OTel: the gen_ai span tree is emitted for a turn; exporter wiring is gated."""

import anyio
from aci_protocol import Event, OtelConfig
from agentos_runner import RunTracer, SideEffectClassifier, build_tracer_provider
from agentos_runner.fake import FakeModelSession
from agentos_runner.session import SessionRunner
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


def test_run_emits_agent_generation_and_tool_spans() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    runner = SessionRunner(
        session_factory=FakeModelSession,  # default_turn: text + Bash tool + result usage
        ceiling=0,
        tracer=RunTracer(provider),
        classifier=SideEffectClassifier(),
        trace_name="agentos-run:test",
        model="fake-model",
    )

    async def go() -> None:
        await runner.start()
        async for _ in runner.run_turn(Event(type="message", text="go", user="U", ts="1")):
            pass

    anyio.run(go)

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert {"agent.run", "llm.generation", "execute_tool"} <= set(spans)
    assert spans["agent.run"].attributes["langfuse.trace.name"] == "agentos-run:test"
    gen = spans["llm.generation"]
    assert gen.attributes["gen_ai.request.model"] == "fake-model"
    assert gen.attributes["gen_ai.usage.output_tokens"] == 8
    assert spans["execute_tool"].attributes["gen_ai.tool.name"] == "Bash"


def test_tracer_provider_none_without_endpoint() -> None:
    otel = OtelConfig()
    assert build_tracer_provider(otel, "s1") is None


def test_tracer_provider_built_with_endpoint() -> None:
    otel = OtelConfig(endpoint="http://localhost:4318")
    provider = build_tracer_provider(otel, "s1")
    assert isinstance(provider, TracerProvider)
    provider.shutdown()
