"""The harness entry-point registry and its two fail-closed guard rules (ADR-0060)."""

from importlib.metadata import EntryPoint

import pytest
from agentos_runner import CLAUDE_READONLY_TOOLS
from agentos_runner.harness.registry import (
    ENTRY_POINT_GROUP,
    FlatHarnessPackageError,
    HarnessNameCollisionError,
    UnknownHarnessError,
    discover_contributions,
    resolve_harness,
)


def _entry_point(name: str, value: str) -> EntryPoint:
    return EntryPoint(name=name, value=value, group=ENTRY_POINT_GROUP)


def test_discovers_claude_via_real_entry_point() -> None:
    contributions = discover_contributions()
    claude = contributions["claude"]
    assert claude.readonly_tools == CLAUDE_READONLY_TOOLS


def test_flat_package_path_refused() -> None:
    with pytest.raises(FlatHarnessPackageError):
        discover_contributions(entry_points=[_entry_point("evil", "evil:get_contribution")])


def test_builtin_name_collision_refused() -> None:
    with pytest.raises(HarnessNameCollisionError):
        discover_contributions(
            entry_points=[_entry_point("claude", "some.impostor.module:get_contribution")]
        )


def test_claudes_own_registration_is_not_flagged_as_collision() -> None:
    contributions = discover_contributions(
        entry_points=[_entry_point("claude", "agentos_runner.harness.claude:get_contribution")]
    )
    assert contributions["claude"].name == "claude"


def test_resolve_harness_by_alias() -> None:
    contributions = discover_contributions()
    assert resolve_harness("claude-code", contributions=contributions) is resolve_harness(
        "claude", contributions=contributions
    )


def test_resolve_unknown_harness_raises() -> None:
    with pytest.raises(UnknownHarnessError):
        resolve_harness("nonexistent", contributions={})
