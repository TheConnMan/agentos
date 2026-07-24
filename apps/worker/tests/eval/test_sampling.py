"""Multi-sample aggregation: majority vote / pass@k over per-sample results (#332)."""

from __future__ import annotations

import pytest
from curie_worker.eval.models import EvalCaseResult, EvalOutcome
from curie_worker.eval.sampling import AggregationPolicy, SampleConfig, aggregate


def _result(outcome: EvalOutcome, *, output: str = "", latency: float = 1.0,
            cost: float | None = None) -> EvalCaseResult:
    return EvalCaseResult(
        case_id="c", outcome=outcome, output=output, latency_ms=latency, cost_usd=cost
    )


def _samples(passes: int, fails: int) -> list[EvalCaseResult]:
    return (
        [_result(EvalOutcome.PASS, output="ok") for _ in range(passes)]
        + [_result(EvalOutcome.FAIL, output="no") for _ in range(fails)]
    )


def test_single_sample_is_returned_unchanged() -> None:
    # n=1 must be a bit-for-bit no-op: the exact pre-#332 result.
    one = _result(EvalOutcome.PASS, output="answer", latency=12.5, cost=0.5)
    assert aggregate("c", [one], SampleConfig(n=1)) is one


def test_majority_two_of_three_is_green() -> None:
    agg = aggregate("c", _samples(2, 1), SampleConfig(n=3, policy=AggregationPolicy.MAJORITY))
    assert agg.passed is True
    assert agg.error is None
    assert agg.output == "ok"  # representative is a passing sample


def test_majority_one_of_three_is_red_and_reports_variance() -> None:
    agg = aggregate("c", _samples(1, 2), SampleConfig(n=3, policy=AggregationPolicy.MAJORITY))
    assert agg.passed is False
    assert agg.error is not None and "1/3 samples passed" in agg.error
    assert agg.output == "no"  # representative is a failing sample


def test_majority_tie_fails_deny_by_default() -> None:
    agg = aggregate("c", _samples(1, 1), SampleConfig(n=2, policy=AggregationPolicy.MAJORITY))
    assert agg.passed is False  # 1-of-2 is not a strict majority


def test_pass_at_k_one_of_three_is_green() -> None:
    agg = aggregate(
        "c", _samples(1, 2), SampleConfig(n=3, policy=AggregationPolicy.PASS_AT_K, k=1)
    )
    assert agg.passed is True


def test_pass_at_k_threshold_not_met_is_red() -> None:
    agg = aggregate(
        "c", _samples(1, 2), SampleConfig(n=3, policy=AggregationPolicy.PASS_AT_K, k=2)
    )
    assert agg.passed is False
    assert agg.error is not None and "pass@2" in agg.error


def test_summed_cost_across_samples() -> None:
    samples = [
        _result(EvalOutcome.PASS, cost=0.10),
        _result(EvalOutcome.PASS, cost=0.20),
        _result(EvalOutcome.FAIL, cost=None),
    ]
    agg = aggregate("c", samples, SampleConfig(n=3))
    assert agg.cost_usd == pytest.approx(0.30)
    assert agg.latency_ms == pytest.approx(3.0)


def test_cost_none_when_no_sample_reported_cost() -> None:
    agg = aggregate("c", _samples(2, 1), SampleConfig(n=3))
    assert agg.cost_usd is None


def test_all_plumbing_samples_stay_non_graded() -> None:
    samples = [_result(EvalOutcome.PLUMBING_OK, output="all done") for _ in range(3)]
    agg = aggregate("c", samples, SampleConfig(n=3))
    assert agg.outcome is EvalOutcome.PLUMBING_OK
    assert agg.passed is None
    assert agg.error is None


def test_config_rejects_nonpositive_n_and_k() -> None:
    with pytest.raises(ValueError):
        SampleConfig(n=0)
    with pytest.raises(ValueError):
        SampleConfig(k=0)


def test_effective_k_clamps_to_n() -> None:
    assert SampleConfig(n=3, k=5).effective_k == 3
