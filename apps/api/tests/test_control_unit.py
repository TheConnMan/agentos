"""Unit tests for the kill-key format and budget validation (no I/O)."""

import uuid

import pytest
from agentos_api.killswitch import KILL_CHANNEL, kill_key
from agentos_api.schemas import BudgetConfig
from pydantic import ValidationError


def test_kill_key_matches_the_seam_contract() -> None:
    agent_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert kill_key(agent_id) == "agentos:kill:00000000-0000-0000-0000-000000000001"
    assert KILL_CHANNEL == "agentos:kill-events"


def test_budget_allows_null_and_positive_values() -> None:
    assert BudgetConfig().max_usd_per_day is None
    ok = BudgetConfig(max_usd_per_day=5.0, max_output_tokens_per_run=1000)
    assert ok.max_usd_per_day == 5.0
    assert ok.max_output_tokens_per_run == 1000


def test_budget_rejects_non_positive_values() -> None:
    with pytest.raises(ValidationError):
        BudgetConfig(max_usd_per_day=0)
    with pytest.raises(ValidationError):
        BudgetConfig(max_output_tokens_per_run=-1)
