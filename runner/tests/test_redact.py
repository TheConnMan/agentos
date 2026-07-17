"""Secret redaction (#518): the shared patterns, the log filter, and the span
attribute choke point. The tripwire half fails if a pattern is added without
being applied by ``redact``; the behavioral half proves a secret is scrubbed from
a log line and a span attribute.
"""

from __future__ import annotations

import logging

import anyio
from aci_protocol import Event
from agentos_runner import RunTracer, SideEffectClassifier
from agentos_runner.fake import FakeModelSession
from agentos_runner.redact import (
    _PATTERNS,
    PLACEHOLDER,
    RedactingLogFilter,
    redact,
)
from agentos_runner.session import SessionRunner
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_SECRET = "sk-ant-oat01SECRETSENTINELvalue999"


# --- tripwire: every pattern is wired into redact ------------------------------


def test_every_pattern_is_applied_by_redact() -> None:
    assert _PATTERNS, "the redaction pattern list must not be empty"
    for name, _pattern, sample in _PATTERNS:
        out = redact(f"prefix {sample} suffix")
        assert PLACEHOLDER in out, f"pattern {name!r} produced no redaction for its sample"
        # The distinctive tail of the sample secret must be gone. (url-credential
        # keeps the `token=` key but drops the value, so check the value tail.)
        secret_tail = sample.split("=")[-1][-8:]
        assert secret_tail not in out, f"pattern {name!r} left its secret sample in the output"


def test_redact_leaves_ordinary_text_untouched() -> None:
    text = "connecting to the model; 42 tokens used; tool Read ran on /etc/hosts"
    assert redact(text) == text


# --- behavioral: logs ----------------------------------------------------------


def test_log_filter_scrubs_an_interpolated_secret() -> None:
    record = logging.LogRecord(
        name="agentos_runner.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="credential resolution failed: %s",
        args=(f"bad token {_SECRET}",),
        exc_info=None,
    )
    assert RedactingLogFilter().filter(record) is True
    message = record.getMessage()
    assert _SECRET not in message
    assert PLACEHOLDER in message


# --- behavioral: span attributes ----------------------------------------------


def test_span_string_attributes_are_redacted() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # A secret smuggled into the model string (an SDK/provider echo) must not ship
    # to Langfuse as a span attribute; it flows through _set_str_attr -> redact.
    runner = SessionRunner(
        session_factory=FakeModelSession,
        ceiling=0,
        tracer=RunTracer(provider),
        classifier=SideEffectClassifier(),
        trace_name=f"agentos-run:{_SECRET}",
        model=f"model-{_SECRET}",
    )

    async def go() -> None:
        await runner.start()
        async for _ in runner.run_turn(Event(type="message", text="go", user="U", ts="1")):
            pass

    anyio.run(go)

    spans = {s.name: s for s in exporter.get_finished_spans()}
    trace_name = spans["agent.run"].attributes["langfuse.trace.name"]
    model = spans["llm.generation"].attributes["gen_ai.request.model"]
    assert _SECRET not in trace_name and PLACEHOLDER in trace_name
    assert _SECRET not in model and PLACEHOLDER in model
