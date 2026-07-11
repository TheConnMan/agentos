"""Offline unit tests for the #313 parity-eval driver (plan §4.1).

These are the contract the parity capture driver must satisfy. They are committed
*before* the implementation (`scripts/parity_eval.py` and
`docs/evals/opencode-parity/cases.json`) so every test starts red and turns green
once the driver + suite land. Nothing here touches the network, a runner, or
Langfuse -- the tests exercise the driver's *pure* helpers and the committed
suite's platform-loadability only.

Four groups, one per plan §4.1 risk:

1. **Suite validity** -- the committed 5-case suite loads under the platform's own
   ``EvalSuite`` model, every grader kind is in the frozen set, and the two bundle
   cases pin the literal fixture tokens so a fixture-token change breaks *this*
   test, not the live matrix.
2. **Normalization math** -- ``compute_usd`` does exact list-price arithmetic and
   returns the literal ``"unavailable"`` (never ``0.0``) when usage is empty/zero.
3. **Grading parity** -- the driver's grade helper agrees with
   ``agentos_worker.eval.runner.EvalRunner`` on the three cases that matter
   (final-wins-over-deltas, classified-failure-fails-regardless, no-final falls
   back to joined deltas).
4. **Span->case matching** -- order-based attribution with a session-id cross-check
   that raises loudly on a mismatched span set instead of mis-attributing silently.

``scripts/`` is not importable, so the driver module is loaded by path via
``importlib``. The load happens *inside* each test (through ``_load_parity_module``),
so a missing script fails the test red with a clear message rather than erroring at
collection time.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from aci_protocol import ErrorEvent, Final, SessionStatus, TextDelta
from agentos_worker.eval.models import EvalSuite, Grader, GraderKind

# Repo root: apps/worker/tests/eval/test_parity_suite.py -> parents[4].
_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "parity_eval.py"
_CASES_PATH = _REPO_ROOT / "docs" / "evals" / "opencode-parity" / "cases.json"

# The two literal fixture tokens the bundle cases must grade against (plan §7,
# Decision 7). Pinning them here means a fixture-token rename breaks this unit
# test loudly instead of silently failing the live run.
_SKILL_TOKEN = "AGENTOS-ROUNDTRIP"
_MCP_NONCE = "NONCE-7f3a9c"


def _load_parity_module() -> ModuleType:
    """Load ``scripts/parity_eval.py`` by path, failing the test if it is missing.

    ``scripts/`` is not on the import path, so the driver is loaded via
    ``spec_from_file_location``. A missing file is a red *failure* (the driver is
    not implemented yet), never a collection error.
    """
    if not _SCRIPT_PATH.exists():
        pytest.fail(
            f"parity driver not implemented yet: expected {_SCRIPT_PATH}",
            pytrace=False,
        )
    spec = importlib.util.spec_from_file_location("parity_eval_under_test", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- Group 1: suite validity (platform-loader contract) -----------------------


def test_committed_suite_is_platform_loadable() -> None:
    """The committed suite loads under the platform's own ``EvalSuite`` model,
    carries exactly 5 cases, and every grader kind is in the frozen set."""
    if not _CASES_PATH.exists():
        pytest.fail(
            f"parity suite not implemented yet: expected {_CASES_PATH}",
            pytrace=False,
        )
    suite = EvalSuite.model_validate_json(_CASES_PATH.read_text(encoding="utf-8"))

    assert len(suite.cases) == 5
    allowed = {GraderKind.EXACT, GraderKind.CONTAINS, GraderKind.REGEX}
    for case in suite.cases:
        assert case.grader.kind in allowed


def test_bundle_cases_pin_literal_fixture_tokens() -> None:
    """The skill and MCP bundle cases must grade against the literal fixture
    tokens, so a fixture-token change breaks this test (not the live matrix)."""
    if not _CASES_PATH.exists():
        pytest.fail(
            f"parity suite not implemented yet: expected {_CASES_PATH}",
            pytrace=False,
        )
    suite = EvalSuite.model_validate_json(_CASES_PATH.read_text(encoding="utf-8"))

    expected_tokens = {case.grader.expected for case in suite.cases}
    assert _SKILL_TOKEN in expected_tokens
    assert _MCP_NONCE in expected_tokens


# --- Group 2: normalization math (``compute_usd``) ----------------------------

_PRICES = {"input": 0.000003, "output": 0.000015}


def test_compute_usd_exact_arithmetic() -> None:
    """USD is ``in_tokens * in_price + out_tokens * out_price`` exactly."""
    mod = _load_parity_module()
    usage = {"input_tokens": 1000, "output_tokens": 500}
    expected = 1000 * 0.000003 + 500 * 0.000015
    assert mod.compute_usd(usage, _PRICES) == expected


def test_compute_usd_empty_usage_is_unavailable() -> None:
    """An empty usage map means capture failed: return ``"unavailable"``, not 0.0."""
    mod = _load_parity_module()
    result = mod.compute_usd({}, _PRICES)
    assert result == "unavailable"
    assert result != 0.0


def test_compute_usd_absent_tokens_is_unavailable() -> None:
    """Usage present but with no token counts is still ``"unavailable"``."""
    mod = _load_parity_module()
    result = mod.compute_usd({"model": "claude-sonnet-5"}, _PRICES)
    assert result == "unavailable"
    assert result != 0.0


def test_compute_usd_explicit_zero_tokens_is_unavailable() -> None:
    """Explicit zero in *and* out tokens must not silently read as $0.0 -- this is
    the silent-zero failure mode the guard exists for."""
    mod = _load_parity_module()
    result = mod.compute_usd({"input_tokens": 0, "output_tokens": 0}, _PRICES)
    assert result == "unavailable"
    assert result != 0.0


# --- Group 3: grading parity with EvalRunner ----------------------------------
# EvalRunner semantics (apps/worker/src/agentos_worker/eval/runner.py:40-86):
#   output = final_text if a Final arrived else "".join(text_deltas)
#   if final.status is CLASSIFIED_FAILURE -> passed = False (grader ignored)
#   else -> passed = grader.grade(output)
# The driver's ``grade_frames(frames, grader)`` must return the identical verdict.


def test_grade_frames_final_wins_over_deltas() -> None:
    """The graded output is the Final text, not the accumulated deltas: deltas
    that would fail the grader are ignored once a Final arrives."""
    mod = _load_parity_module()
    frames = [
        TextDelta(text="wrong"),
        TextDelta(text="answer"),
        Final(text="144", status=SessionStatus.DONE),
    ]
    grader = Grader(kind=GraderKind.CONTAINS, expected="144")
    # EvalRunner grades "144" (the Final), which contains "144" -> True.
    assert mod.grade_frames(frames, grader) is True


def test_grade_frames_classified_failure_fails_regardless() -> None:
    """A classified-failure Final can never grade green, even when its text matches
    the grader (runner.py:73-80)."""
    mod = _load_parity_module()
    frames = [
        ErrorEvent(message="budget exhausted", classification="budget"),
        Final(text="144", status=SessionStatus.CLASSIFIED_FAILURE),
    ]
    grader = Grader(kind=GraderKind.CONTAINS, expected="144")
    # Text matches, but the classified-failure status forces a fail.
    assert mod.grade_frames(frames, grader) is False


def test_grade_frames_no_final_falls_back_to_joined_deltas() -> None:
    """With no Final, the graded output is the joined text deltas (runner.py:69)."""
    mod = _load_parity_module()
    frames = [TextDelta(text="1"), TextDelta(text="4"), TextDelta(text="4")]
    grader = Grader(kind=GraderKind.CONTAINS, expected="144")
    # Joined deltas -> "144", which contains "144" -> True.
    assert mod.grade_frames(frames, grader) is True


def test_grade_case_empty_frames_fails_even_for_match_anything_grader() -> None:
    """The transport-exception path (``_consume_case`` returns ``frames == []``)
    must grade ``passed=False`` unconditionally, mirroring ``EvalRunner`` at
    ``runner.py:60-67`` where the grader never runs on that exception set.

    A ``contains ""`` grader matches *anything* -- ``grade_frames([], grader)``
    alone returns True -- so this proves the driver's ``grade_case`` guard, not
    the grader, forces the fail. Deleting the ``if not frames`` guard flips this
    to True and fails the test."""
    mod = _load_parity_module()
    match_anything = Grader(kind=GraderKind.CONTAINS, expected="")
    # Baseline: the grader alone would pass empty frames.
    assert mod.grade_frames([], match_anything) is True
    # The composed verdict run_suite applies must still fail: empty frames only
    # arise from the transport-error path, which EvalRunner fails unconditionally.
    assert mod.grade_case([], match_anything) is False
    # A normal turn (at least a Final) still grades through the grader.
    non_empty = [Final(text="anything", status=SessionStatus.DONE)]
    assert mod.grade_case(non_empty, match_anything) is True


# --- Group 4: span->case matching ---------------------------------------------
# Contract (documented here since the test writer defines the shape the implementer
# accepts):
#   match_spans_to_cases(spans, case_ids, session_id) -> list[tuple[str, dict]]
#   * ``spans``: a list of plain dicts in span *arrival order*; each span carries
#     its session identity on the ``"agentos.session_id"`` key (plus arbitrary
#     token/model attrs the driver reads elsewhere).
#   * ``case_ids``: the case ids in the same fixed order the cases were sent.
#   * Returns ``[(case_id, span), ...]`` pairing case i with span i.
#   * Raises ``ValueError`` (loudly) when the span set does not match: a different
#     count than ``case_ids``, or any span whose ``agentos.session_id`` disagrees
#     with ``session_id``. It must never silently mis-attribute.


def _span(session_id: str, **attrs: object) -> dict:
    return {"agentos.session_id": session_id, **attrs}


def test_match_spans_to_cases_pairs_in_order() -> None:
    """N spans + N case ids pair positionally when the session ids agree."""
    mod = _load_parity_module()
    spans = [
        _span("parity-A-1", input_tokens=1000, output_tokens=500),
        _span("parity-A-1", input_tokens=20, output_tokens=10),
        _span("parity-A-1", input_tokens=7, output_tokens=3),
    ]
    case_ids = ["plain-answer", "format-following", "multi-step"]
    pairs = mod.match_spans_to_cases(spans, case_ids, "parity-A-1")
    assert pairs == [
        ("plain-answer", spans[0]),
        ("format-following", spans[1]),
        ("multi-step", spans[2]),
    ]


def test_match_spans_to_cases_raises_on_count_mismatch() -> None:
    """A span set of the wrong size is a capture error, raised loudly."""
    mod = _load_parity_module()
    spans = [_span("parity-A-1")]
    with pytest.raises(ValueError):
        mod.match_spans_to_cases(spans, ["plain-answer", "format-following"], "parity-A-1")


def test_match_spans_to_cases_raises_on_session_mismatch() -> None:
    """A span whose session id disagrees means the spans are from another run;
    the matcher raises rather than mis-attributing tokens to the wrong session."""
    mod = _load_parity_module()
    spans = [_span("SOME-OTHER-SESSION", input_tokens=1000, output_tokens=500)]
    with pytest.raises(ValueError):
        mod.match_spans_to_cases(spans, ["plain-answer"], "parity-A-1")
