"""CI drift gate on the closed OTel attribute schema (ADR-0076 Stone 5, #891).

Mirrors ADR-0074's committed-artifact-plus-CI-gate discipline for the CLI's
``--json`` schemas, adapted for this Python-only surface: ``SpanAttributeKey``
(``otel.py``) is the single source of truth in code, this file's committed
mirror (``runner/schema/otel-attributes.schema.json``) is the reviewed
artifact, and the tests below fail loudly if the two diverge -- catching a
new attribute key (or a bumped ``SCHEMA_VERSION``) that lands without an
accompanying schema update. A real emitted-attribute contract test closes the
loop by proving a live run's span attributes never escape the committed set.

Regenerate the committed file after a deliberate schema change by running, from
the repo root: import ``json``, ``SCHEMA_VERSION`` and ``SpanAttributeKey``
from ``agentos_runner.otel``, then write
``{"id": "https://schemas.agentos.dev/runner/otel-attributes/v1.json",
"schema_version": SCHEMA_VERSION, "keys": sorted(m.value for m in
SpanAttributeKey)}`` as indented JSON to
``runner/schema/otel-attributes.schema.json``.
"""

import json
from pathlib import Path

import anyio
from aci_protocol import Event
from agentos_runner import RunTracer, SideEffectClassifier
from agentos_runner.fake import FakeModelSession
from agentos_runner.otel import SCHEMA_VERSION, SpanAttributeKey
from agentos_runner.session import SessionRunner
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_SCHEMA_PATH = Path(__file__).parent.parent / "schema" / "otel-attributes.schema.json"


def _committed_schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text())


def test_committed_schema_matches_the_enum() -> None:
    committed_keys = set(_committed_schema()["keys"])
    code_keys = {member.value for member in SpanAttributeKey}
    assert code_keys == committed_keys, (
        "SpanAttributeKey (otel.py) and the committed schema "
        f"({_SCHEMA_PATH}) have diverged -- a key was added or removed on one "
        "side but not the other. Regenerate the committed file (see this "
        "module's docstring) and commit it alongside the code change."
    )


def test_committed_schema_version_matches_the_code() -> None:
    committed_version = _committed_schema()["schema_version"]
    assert committed_version == SCHEMA_VERSION, (
        "SCHEMA_VERSION (otel.py) and the committed schema's schema_version "
        f"({_SCHEMA_PATH}) disagree. Per ADR-0076, bump both together only "
        "for a removed/renamed/retyped key -- a new optional key is additive "
        "and must not bump either."
    )


def test_a_real_run_only_emits_attributes_within_the_committed_schema() -> None:
    # The contract half: proves the enum's closure actually holds on the wire,
    # not just in the committed inventory. Drives a real turn (permission gate
    # + tool call + usage, the FakeModelSession default script) and asserts
    # every attribute key on every emitted span is in the committed set.
    committed_keys = set(_committed_schema()["keys"])
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    runner = SessionRunner(
        session_factory=FakeModelSession,
        ceiling=0,
        tracer=RunTracer(provider),
        classifier=SideEffectClassifier(),
        trace_name="agentos-run:test",
        session_id="s1",
        model="fake-model",
    )

    async def go() -> None:
        await runner.start()
        async for _ in runner.run_turn(Event(type="message", text="go", user="U", ts="1")):
            pass

    anyio.run(go)

    spans = exporter.get_finished_spans()
    assert spans, "expected at least one emitted span to check"
    for span in spans:
        emitted_keys = set(span.attributes)
        assert emitted_keys <= committed_keys, (
            f"span {span.name!r} emitted attribute key(s) outside the "
            f"committed schema: {emitted_keys - committed_keys}"
        )
