"""Pricing table tests: real token usage x model price -> dollar cost (#390)."""

from __future__ import annotations

import pytest
from agentos_worker.eval.pricing import cost_usd


def test_prices_known_model_from_input_and_output_tokens() -> None:
    # claude-opus-4-8: $5/1M input, $25/1M output.
    # 1M input + 200k output = 5.0 + 5.0 = 10.0
    assert cost_usd("claude-opus-4-8", 1_000_000, 200_000) == pytest.approx(10.0)


def test_resolves_family_from_prefixed_or_dated_model_id() -> None:
    # A platform-prefixed / dated id resolves to the same family price by
    # containment, so a Bedrock-style id is not silently unpriced.
    exact = cost_usd("claude-sonnet-4-5", 1_000_000, 0)
    prefixed = cost_usd("anthropic.claude-sonnet-4-5-20250929", 1_000_000, 0)
    assert exact == prefixed == pytest.approx(3.0)


def test_specific_family_wins_over_shorter_prefix() -> None:
    # claude-opus-4-1 is $15/1M input; the shorter claude-opus-4 prefix must not
    # shadow it (longest key wins).
    assert cost_usd("claude-opus-4-1-20250805", 1_000_000, 0) == pytest.approx(15.0)


def test_unknown_model_is_none_not_guessed() -> None:
    assert cost_usd("some-unpriced-model", 1_000_000, 200_000) is None


def test_none_model_is_none() -> None:
    assert cost_usd(None, 1_000_000, 200_000) is None


def test_no_usage_at_all_is_none() -> None:
    assert cost_usd("claude-opus-4-8", None, None) is None


def test_partial_usage_prices_the_known_side() -> None:
    # Only output reported: input treated as zero once one side is known.
    assert cost_usd("claude-opus-4-8", None, 1_000_000) == pytest.approx(25.0)
