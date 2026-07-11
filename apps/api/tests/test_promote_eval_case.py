"""Promote-a-trace-to-an-eval-case endpoint (#259).

Only the external Langfuse read is faked (permitted); the extraction,
anonymization, and FastAPI wiring run for real. Also unit-tests the pure
anonymization/extraction helpers.
"""

from typing import Any

import pytest
from agentos_api.deps import get_langfuse
from agentos_api.evalcase import extract_io, redact, trace_to_eval_case
from agentos_api.main import create_app
from fastapi.testclient import TestClient


class FakeLangfuse:
    def __init__(
        self, observations: list[dict[str, Any]], trace: dict[str, Any] | None = None
    ) -> None:
        self._observations = observations
        self._trace = trace or {"id": "t", "name": "demo"}

    async def get_observations(self, trace_id: str) -> list[dict[str, Any]]:
        return self._observations

    async def get_trace(self, trace_id: str) -> dict[str, Any]:
        return {**self._trace, "id": trace_id}


def _app_with(
    observations: list[dict[str, Any]], trace: dict[str, Any] | None = None
) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_langfuse] = lambda: FakeLangfuse(observations, trace)
    return TestClient(app)


# --- Endpoint --------------------------------------------------------------


def test_promote_emits_runnable_anonymized_case(auth_headers: dict[str, str]) -> None:
    trace = {
        "name": "agentos-run:agent-x",
        "input": [{"role": "user", "content": "Email me at jane.doe@acme.com about U012ABCDEF"}],
        "output": "Sure, I sent the summary to your address.\nMore detail below.",
    }
    with _app_with([{"id": "r", "type": "SPAN"}], trace) as client:
        resp = client.post("/langfuse/traces/abc/eval-case", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    case = resp.json()

    # Conforms to the frozen eval-case shape.
    assert case["id"] == "promoted-abc"
    assert set(case["grader"]) == {"kind", "expected", "case_sensitive"}
    assert case["grader"]["kind"] == "contains"

    # Anonymized: the email and Slack id never reach the emitted case.
    assert "jane.doe@acme.com" not in case["input"]
    assert "U012ABCDEF" not in case["input"]
    assert "<email>" in case["input"]
    assert "<slack-id>" in case["input"]

    # Expected keys off the first salient (redacted) output line, capped.
    assert case["grader"]["expected"] == "Sure, I sent the summary to your address."


def test_promote_falls_back_to_observation_io(auth_headers: dict[str, str]) -> None:
    # The trace has no top-level input/output; the endpoint pulls them from the
    # observations (first input, last output).
    observations = [
        {"id": "a", "type": "GENERATION", "input": "What is the refund policy?"},
        {"id": "b", "type": "GENERATION", "output": "Refunds are issued within 30 days."},
    ]
    with _app_with(observations, {"name": "run"}) as client:
        resp = client.post("/langfuse/traces/xyz/eval-case", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    case = resp.json()
    assert case["input"] == "What is the refund policy?"
    assert case["grader"]["expected"] == "Refunds are issued within 30 days."


def test_promote_404_when_no_observations(auth_headers: dict[str, str]) -> None:
    with _app_with([]) as client:
        resp = client.post("/langfuse/traces/abc/eval-case", headers=auth_headers)
    assert resp.status_code == 404


def test_promote_rejects_malformed_trace_id(auth_headers: dict[str, str]) -> None:
    with _app_with([{"id": "r"}]) as client:
        resp = client.post("/langfuse/traces/abc.def/eval-case", headers=auth_headers)
    assert resp.status_code == 400


def test_promote_requires_api_key() -> None:
    with _app_with([{"id": "r"}]) as client:
        resp = client.post("/langfuse/traces/abc/eval-case")
    assert resp.status_code == 401


# --- Pure helpers ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,needle,absent",
    [
        ("ping a@b.com", "<email>", "a@b.com"),
        ("user U01ABCDEFG here", "<slack-id>", "U01ABCDEFG"),
        ("call +1 (415) 555-1234 now", "<phone>", "555-1234"),
        ("token xoxb-123456-abcdef leaked", "<token>", "xoxb-123456-abcdef"),
        ("key sk-ant-abc123def456 used", "<token>", "sk-ant-abc123def456"),
    ],
)
def test_redact_masks_identifiers(raw: str, needle: str, absent: str) -> None:
    out = redact(raw)
    assert needle in out
    assert absent not in out


def test_redact_leaves_plain_text_untouched() -> None:
    text = "What is the weather in San Francisco today?"
    assert redact(text) == text


def test_extract_io_prefers_trace_level_fields() -> None:
    trace = {"input": "hello", "output": "world"}
    assert extract_io(trace, []) == ("hello", "world")


def test_extract_io_picks_last_user_message() -> None:
    trace = {
        "input": [
            {"role": "system", "content": "be nice"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "second"},
        ],
        "output": "ok",
    }
    got_input, _ = extract_io(trace, [])
    assert got_input == "second"


def test_trace_to_eval_case_empty_output_is_still_runnable() -> None:
    case = trace_to_eval_case("t1", {"input": "hi", "output": ""}, [{"id": "r"}])
    assert case.input == "hi"
    # No output -> a trivially-passing contains-"" grader keeps the case runnable.
    assert case.grader.expected == ""
    assert case.grader.kind == "contains"
