"""Prompt-cache smoke test (issue #255): a warm thread must hit cache.

Two layers:

* Unit — :mod:`agentos_runner.cache` classifies a turn's usage as a cache hit
  (``cache_read_input_tokens > 0``) or a cold/broken turn.
* End-to-end smoke — drive a two-turn *warm thread* through the real
  ``SessionRunner`` over the fake harness and assert the second turn's
  ``llm.generation`` span carries cache-read tokens. This is the load-bearing
  assertion: a translating gateway that silently breaks caching (OpenRouter BYOK
  no-cache, a LiteLLM ``cache_control`` mis-map) surfaces here as a warm turn with
  zero cache reads and fails the test loudly, instead of a silent ~10x cost blowup
  in production.
"""

import anyio
from aci_protocol import Event
from agentos_runner import RunTracer, SideEffectClassifier
from agentos_runner.cache import (
    cache_creation_tokens,
    cache_read_tokens,
    is_cache_hit,
)
from agentos_runner.fake import FakeModelSession
from agentos_runner.session import SessionRunner
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# A cold first turn writes the prefix into the cache (creation > 0, read == 0);
# the warm second turn against the same session reads it back (read > 0). This is
# the Anthropic wire usage shape, preserved even through OpenRouter.
_COLD_USAGE = {
    "input_tokens": 40,
    "output_tokens": 8,
    "cache_creation_input_tokens": 1200,
    "cache_read_input_tokens": 0,
}
_WARM_USAGE = {
    "input_tokens": 12,
    "output_tokens": 8,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 1200,
}


def _turn(usage: dict) -> list:
    return [
        AssistantMessage(content=[TextBlock(text="ok")], model="fake-model", usage=usage),
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="fake-session",
            result="ok",
            usage=usage,
        ),
    ]


# --- unit: the cache-hit classifier ---------------------------------------


def test_cold_turn_is_not_a_cache_hit() -> None:
    assert cache_creation_tokens(_COLD_USAGE) == 1200
    assert cache_read_tokens(_COLD_USAGE) == 0
    assert is_cache_hit(_COLD_USAGE) is False


def test_warm_turn_is_a_cache_hit() -> None:
    assert cache_read_tokens(_WARM_USAGE) == 1200
    assert is_cache_hit(_WARM_USAGE) is True


def test_missing_or_malformed_usage_reads_as_cold() -> None:
    assert is_cache_hit(None) is False
    assert is_cache_hit({}) is False
    # bool must not read as a token count (bool is an int subclass).
    assert cache_read_tokens({"cache_read_input_tokens": True}) == 0
    assert cache_read_tokens({"cache_read_input_tokens": "1200"}) == 0


# --- end-to-end: a warm thread hits cache through the real pipeline --------


def _gen_span_per_turn(usages: list[dict]) -> list:
    """Drive one warm thread of N turns; return each turn's llm.generation span."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    scripts = iter(usages)
    runner = SessionRunner(
        session_factory=lambda: FakeModelSession(lambda: _turn(next(scripts))),
        ceiling=0,
        tracer=RunTracer(provider),
        classifier=SideEffectClassifier(),
        trace_name="agentos-run:cache-smoke",
        model="fake-model",
    )

    async def go() -> None:
        await runner.start()
        for i in range(len(usages)):
            async for _ in runner.run_turn(
                Event(type="message", text=f"turn-{i}", user="U", ts=str(i))
            ):
                pass

    anyio.run(go)
    # Generation spans finish in turn order.
    return [s for s in exporter.get_finished_spans() if s.name == "llm.generation"]


def test_warm_thread_second_turn_hits_cache_end_to_end() -> None:
    gens = _gen_span_per_turn([_COLD_USAGE, _WARM_USAGE])
    assert len(gens) == 2

    cold, warm = gens
    # Cold turn: the prefix was written, nothing was read.
    assert cold.attributes["gen_ai.usage.cache_creation_input_tokens"] == 1200
    assert cold.attributes["gen_ai.usage.cache_read_input_tokens"] == 0
    # Warm turn: the repeated prefix was served from cache. If this is 0 the
    # thread did not hit cache and the smoke test fails loudly -- exactly the
    # gateway cache-breakage signal the issue calls out.
    assert warm.attributes["gen_ai.usage.cache_read_input_tokens"] > 0
