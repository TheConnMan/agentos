"""Scorer semantics: one PASS and one FAIL per scorer, the SCORERS registry,
and ``score_run`` mapping (including the unknown-category error)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from harness_eval.models import AgentRun, Condition, HarnessTask
from harness_eval.scoring import (
    SCORERS,
    score_add_mcp_server,
    score_build_skill,
    score_fix_empty_api_key,
    score_run,
    score_write_eval_gate,
)

# The ``workspace_factory`` fixture is provided by conftest.py; this alias just
# names its shape for readable signatures (mapping of relpath -> content).
WorkspaceFactory = Callable[..., Path]

# --- SKILL.md contents (the primer landmine: allowed-tools, not tools) --------

_SKILL_GOOD = """---
name: my-skill
description: Does a thing.
allowed-tools:
  - Read
  - Bash
---
# My Skill

Body text.
"""

_SKILL_WRONG_KEY = """---
name: my-skill
description: Does a thing.
tools:
  - Read
  - Bash
---
# My Skill

Body text.
"""

# Frontmatter that is well delimited but not valid YAML (a bare unclosed
# bracket makes ``yaml.safe_load`` raise ``yaml.YAMLError``).
_SKILL_MALFORMED_YAML = """---
name: my-skill
allowed-tools: [Read, Bash
---
# My Skill

Body text.
"""


def test_score_build_skill_pass(workspace_factory: WorkspaceFactory) -> None:
    ws = workspace_factory({"skills/my-skill/SKILL.md": _SKILL_GOOD})
    passed, _detail = score_build_skill(ws, "")
    assert passed is True


def test_score_build_skill_fail_wrong_key(workspace_factory: WorkspaceFactory) -> None:
    ws = workspace_factory({"skills/my-skill/SKILL.md": _SKILL_WRONG_KEY})
    passed, _detail = score_build_skill(ws, "")
    assert passed is False


def test_score_build_skill_fail_missing(workspace_factory: WorkspaceFactory) -> None:
    ws = workspace_factory({})
    passed, _detail = score_build_skill(ws, "")
    assert passed is False


def test_score_build_skill_malformed_yaml_no_raise(
    workspace_factory: WorkspaceFactory,
) -> None:
    ws = workspace_factory({"skills/my-skill/SKILL.md": _SKILL_MALFORMED_YAML})
    passed, _detail = score_build_skill(ws, "")
    assert passed is False


# --- .mcp.json ----------------------------------------------------------------

_MCP_INLINE = '{"mcpServers": {"fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}}}'
_MCP_POINTER = '{"mcpServers": {"fetch": "mcp-server-fetch"}}'


def test_score_add_mcp_server_pass(workspace_factory: WorkspaceFactory) -> None:
    ws = workspace_factory({".mcp.json": _MCP_INLINE})
    passed, _detail = score_add_mcp_server(ws, "")
    assert passed is True


def test_score_add_mcp_server_fail_pointer(workspace_factory: WorkspaceFactory) -> None:
    ws = workspace_factory({".mcp.json": _MCP_POINTER})
    passed, _detail = score_add_mcp_server(ws, "")
    assert passed is False


def test_score_add_mcp_server_fail_absent(workspace_factory: WorkspaceFactory) -> None:
    ws = workspace_factory({})
    passed, _detail = score_add_mcp_server(ws, "")
    assert passed is False


# --- evals/cases.json ---------------------------------------------------------

_CASES_GOOD = (
    '{"cases": [{"id": "c1", "input": "2+2", '
    '"grader": {"kind": "contains", "expected": "4"}}]}'
)
_CASES_NO_GRADER = '{"cases": [{"id": "c1", "input": "2+2"}]}'
_CASES_EMPTY = '{"cases": []}'
_CASES_NULL_GRADER = '{"cases": [{"id": "c1", "input": "2+2", "grader": null}]}'
_CASES_EMPTY_GRADER = '{"cases": [{"id": "c1", "input": "2+2", "grader": {}}]}'


def test_score_write_eval_gate_pass(workspace_factory: WorkspaceFactory) -> None:
    ws = workspace_factory({"evals/cases.json": _CASES_GOOD})
    passed, _detail = score_write_eval_gate(ws, "")
    assert passed is True


def test_score_write_eval_gate_fail_missing_grader(
    workspace_factory: WorkspaceFactory,
) -> None:
    ws = workspace_factory({"evals/cases.json": _CASES_NO_GRADER})
    passed, _detail = score_write_eval_gate(ws, "")
    assert passed is False


def test_score_write_eval_gate_fail_empty(workspace_factory: WorkspaceFactory) -> None:
    ws = workspace_factory({"evals/cases.json": _CASES_EMPTY})
    passed, _detail = score_write_eval_gate(ws, "")
    assert passed is False


def test_score_write_eval_gate_fail_null_grader(
    workspace_factory: WorkspaceFactory,
) -> None:
    ws = workspace_factory({"evals/cases.json": _CASES_NULL_GRADER})
    passed, _detail = score_write_eval_gate(ws, "")
    assert passed is False


def test_score_write_eval_gate_fail_empty_grader(
    workspace_factory: WorkspaceFactory,
) -> None:
    ws = workspace_factory({"evals/cases.json": _CASES_EMPTY_GRADER})
    passed, _detail = score_write_eval_gate(ws, "")
    assert passed is False


# --- .env empty-API-key fix ---------------------------------------------------

_ENV_FIXED_REMOVED = "OTHER=1\nMODEL=claude\n"
_ENV_FIXED_NONEMPTY = 'ANTHROPIC_API_KEY="sk-real"\nOTHER=1\n'
_ENV_STILL_EMPTY_QUOTED = 'ANTHROPIC_API_KEY=""\nOTHER=1\n'
_ENV_STILL_EMPTY_BARE = "ANTHROPIC_API_KEY=\nOTHER=1\n"
# A good key line that a later line re-assigns to empty: the residual wins.
_ENV_GOOD_THEN_EMPTY = 'ANTHROPIC_API_KEY="sk-real"\nOTHER=1\nANTHROPIC_API_KEY=""\n'


def test_score_fix_empty_api_key_pass_removed(
    workspace_factory: WorkspaceFactory,
) -> None:
    ws = workspace_factory({".env": _ENV_FIXED_REMOVED})
    passed, _detail = score_fix_empty_api_key(ws, "")
    assert passed is True


def test_score_fix_empty_api_key_pass_nonempty(
    workspace_factory: WorkspaceFactory,
) -> None:
    ws = workspace_factory({".env": _ENV_FIXED_NONEMPTY})
    passed, _detail = score_fix_empty_api_key(ws, "")
    assert passed is True


def test_score_fix_empty_api_key_fail_quoted_empty(
    workspace_factory: WorkspaceFactory,
) -> None:
    ws = workspace_factory({".env": _ENV_STILL_EMPTY_QUOTED})
    passed, _detail = score_fix_empty_api_key(ws, "")
    assert passed is False


def test_score_fix_empty_api_key_fail_bare_empty(
    workspace_factory: WorkspaceFactory,
) -> None:
    ws = workspace_factory({".env": _ENV_STILL_EMPTY_BARE})
    passed, _detail = score_fix_empty_api_key(ws, "")
    assert passed is False


def test_score_fix_empty_api_key_fail_good_then_empty(
    workspace_factory: WorkspaceFactory,
) -> None:
    ws = workspace_factory({".env": _ENV_GOOD_THEN_EMPTY})
    passed, _detail = score_fix_empty_api_key(ws, "")
    assert passed is False


# --- SCORERS registry + score_run ---------------------------------------------


def test_scorers_registry_keys() -> None:
    assert set(SCORERS) == {
        "build-skill",
        "add-mcp-server",
        "write-eval-gate",
        "fix-empty-api-key",
    }


def _task(category: str) -> HarnessTask:
    return HarnessTask(
        id=f"task-{category}",
        title="t",
        category=category,
        prompt="do it",
        landmine="x",
    )


def test_score_run_maps_to_scorer(workspace_factory: WorkspaceFactory) -> None:
    ws = workspace_factory({"skills/my-skill/SKILL.md": _SKILL_GOOD})
    task = _task("build-skill")
    run = AgentRun(
        task_id=task.id,
        condition=Condition.WITH_PRIMER,
        workspace=ws,
        transcript="did the thing",
        input_tokens=40,
        output_tokens=10,
        errors=1,
    )
    score = score_run(task, run)
    assert score.success is True
    assert score.task_id == task.id
    assert score.condition == Condition.WITH_PRIMER
    assert score.total_tokens == 50
    assert score.errors == 1


def test_score_run_carries_failure(workspace_factory: WorkspaceFactory) -> None:
    ws = workspace_factory({"skills/my-skill/SKILL.md": _SKILL_WRONG_KEY})
    task = _task("build-skill")
    run = AgentRun(
        task_id=task.id,
        condition=Condition.BASELINE,
        workspace=ws,
        transcript="",
        input_tokens=100,
        output_tokens=50,
        errors=3,
    )
    score = score_run(task, run)
    assert score.success is False
    assert score.total_tokens == 150
    assert score.errors == 3


def test_score_run_unknown_category(tmp_path: Path) -> None:
    task = _task("no-such-category")
    run = AgentRun(
        task_id=task.id,
        condition=Condition.BASELINE,
        workspace=tmp_path,
        transcript="",
        input_tokens=0,
        output_tokens=0,
        errors=0,
    )
    with pytest.raises(KeyError):
        score_run(task, run)
