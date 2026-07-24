"""Unit tests for hoisting the runner's sandbox id out of a Langfuse trace.

The runner stamps `curie.sandbox_id` as an OTel resource attribute
(runner/otel.py). Where Langfuse surfaces that attribute -- on the trace's
resource/metadata or only on the observations -- is cluster/Langfuse-defined, so
the hoist probes both. These tests fake both payload shapes; the real
propagation is exercised only against a live Langfuse (see
test_langfuse_integration.py).
"""

from typing import Any

from curie_api.langfuse import hoist_sandbox_id


def test_hoist_from_trace_metadata() -> None:
    trace = {"id": "t1", "metadata": {"curie.sandbox_id": "runner-deal-desk-abc"}}
    assert hoist_sandbox_id(trace, []) == "runner-deal-desk-abc"


def test_hoist_from_trace_resource_attributes() -> None:
    # Nested under resourceAttributes.attributes, the shape an OTel resource
    # export can take once Langfuse maps it onto the trace.
    trace = {
        "id": "t1",
        "resourceAttributes": {"attributes": {"curie.sandbox_id": "sbx-9"}},
    }
    assert hoist_sandbox_id(trace, []) == "sbx-9"


def test_hoist_from_first_observation_when_trace_lacks_it() -> None:
    # Langfuse may surface the resource attr only on the observations; the hoist
    # falls back to the first observation's resource attributes.
    trace = {"id": "t1", "metadata": {"other": "x"}}
    observations: list[dict[str, Any]] = [
        {
            "id": "root",
            "type": "SPAN",
            "resourceAttributes": {"curie.sandbox_id": "sbx-obs"},
        },
        {"id": "child", "type": "SPAN"},
    ]
    assert hoist_sandbox_id(trace, observations) == "sbx-obs"


def test_hoist_prefers_trace_over_observation() -> None:
    trace = {"id": "t1", "metadata": {"curie.sandbox_id": "sbx-trace"}}
    observations = [{"id": "root", "metadata": {"curie.sandbox_id": "sbx-obs"}}]
    assert hoist_sandbox_id(trace, observations) == "sbx-trace"


def test_hoist_accepts_bare_sandbox_id_key() -> None:
    trace = {"id": "t1", "metadata": {"sandbox_id": "sbx-bare"}}
    assert hoist_sandbox_id(trace, []) == "sbx-bare"


def test_hoist_returns_none_when_absent() -> None:
    trace = {"id": "t1", "metadata": {"other": "x"}}
    observations = [{"id": "root", "type": "SPAN"}]
    assert hoist_sandbox_id(trace, observations) is None


def test_hoist_ignores_empty_value() -> None:
    trace = {"id": "t1", "metadata": {"curie.sandbox_id": ""}}
    assert hoist_sandbox_id(trace, []) is None
