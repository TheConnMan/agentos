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


def test_generation_model_backfilled_from_sdk_when_unconfigured() -> None:
    # AGENTOS_MODEL unset (model=None) must NOT leave the generation span
    # model-less: Langfuse would then ingest it as an untyped span and drop token
    # usage to zero. The runner backfills the model the SDK reports on its first
    # assistant message (the fake scripts model="fake-model"), so the span stays a
    # typed generation with usage intact.
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    runner = SessionRunner(
        session_factory=FakeModelSession,
        ceiling=0,
        tracer=RunTracer(provider),
        classifier=SideEffectClassifier(),
        trace_name="agentos-run:test",
        model=None,
    )

    async def go() -> None:
        await runner.start()
        async for _ in runner.run_turn(Event(type="message", text="go", user="U", ts="1")):
            pass

    anyio.run(go)

    gen = {s.name: s for s in exporter.get_finished_spans()}["llm.generation"]
    assert gen.attributes["gen_ai.request.model"] == "fake-model"
    # The usage counts only land on a model-bearing generation, so their presence
    # is the end-to-end proof the span was typed as a generation, not a bare span.
    assert gen.attributes["gen_ai.usage.output_tokens"] == 8


def test_tracer_provider_none_without_endpoint() -> None:
    otel = OtelConfig()
    assert build_tracer_provider(otel, "s1") is None


def test_tracer_provider_built_with_endpoint() -> None:
    otel = OtelConfig(endpoint="http://localhost:4318")
    provider = build_tracer_provider(otel, "s1")
    assert isinstance(provider, TracerProvider)
    provider.shutdown()


def test_resource_stamps_sandbox_id_when_present() -> None:
    # The sandbox id (ACI AGENTOS_SANDBOX_ID) lets a trace be attributed to the
    # concrete sandbox that produced it, not just the session.
    otel = OtelConfig(endpoint="http://localhost:4318")
    provider = build_tracer_provider(otel, "s1", "sandbox-abc")
    assert provider is not None
    attrs = provider.resource.attributes
    assert attrs["agentos.session_id"] == "s1"
    assert attrs["agentos.sandbox_id"] == "sandbox-abc"
    provider.shutdown()


def test_resource_omits_sandbox_id_when_absent_or_empty() -> None:
    # Absent (default) and empty-string sandbox ids are both omitted rather than
    # stamped as an empty attribute value.
    otel = OtelConfig(endpoint="http://localhost:4318")
    for sandbox_id in (None, ""):
        provider = build_tracer_provider(otel, "s1", sandbox_id)
        assert provider is not None
        assert "agentos.sandbox_id" not in provider.resource.attributes
        provider.shutdown()
