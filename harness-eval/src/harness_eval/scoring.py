"""Deterministic scorers, one per task category.

Each scorer grades a produced workspace against the primer landmine for its
category and returns ``(passed, detail)``. ``score_run`` looks the scorer up by
``task.category`` and wraps the outcome in a ``TaskScore`` carrying the run's
token and error counts. An unknown category raises ``KeyError`` by design: a
category with no scorer must never silently pass.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from .models import AgentRun, HarnessTask, TaskScore

Scorer = Callable[[Path, str], tuple[bool, str]]


def _skill_frontmatter(text: str) -> dict[str, Any] | None:
    """Parse the leading ``---`` YAML frontmatter block of a SKILL.md."""
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return None
    parts = stripped.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        loaded = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None
    return loaded if isinstance(loaded, dict) else None


def score_build_skill(workspace: Path, _transcript: str) -> tuple[bool, str]:
    """Pass when a SKILL.md restricts tools via ``allowed-tools`` (not ``tools``)."""
    skills = list(workspace.glob("skills/**/SKILL.md"))
    if not skills:
        return False, "no skills/**/SKILL.md found"
    for skill in skills:
        frontmatter = _skill_frontmatter(skill.read_text())
        if frontmatter is None:
            continue
        if "allowed-tools" in frontmatter:
            return True, f"{skill.name} uses allowed-tools"
        if "tools" in frontmatter:
            return False, f"{skill.name} uses tools, not allowed-tools"
    return False, "no SKILL.md declares allowed-tools"


def score_add_mcp_server(workspace: Path, _transcript: str) -> tuple[bool, str]:
    """Pass when .mcp.json has a server as an inline object with a command."""
    path = workspace / ".mcp.json"
    if not path.is_file():
        return False, "no .mcp.json found"
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return False, f".mcp.json is not valid JSON: {exc}"
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or not servers:
        return False, ".mcp.json has no mcpServers"
    for name, entry in servers.items():
        if isinstance(entry, dict) and "command" in entry:
            return True, f"server {name} is an inline object with a command"
    return False, "mcpServers entries are string pointers, not inline objects"


def score_write_eval_gate(workspace: Path, _transcript: str) -> tuple[bool, str]:
    """Pass when evals/cases.json is non-empty and every case names a grader.

    A ``grader`` that is present but unusable (null or an empty object) must
    fail: a case that names no real grader cannot gate anything.
    """
    path = workspace / "evals" / "cases.json"
    if not path.is_file():
        return False, "no evals/cases.json found"
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        return False, f"cases.json is not valid JSON: {exc}"
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list) or not cases:
        return False, "cases.json has no cases"
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or "grader" not in case:
            return False, f"case {index} has no grader"
        grader = case["grader"]
        if not isinstance(grader, dict) or not grader:
            return False, f"case {index} has an empty grader"
    return True, f"{len(cases)} cases, each with a grader"


def score_fix_empty_api_key(workspace: Path, _transcript: str) -> tuple[bool, str]:
    """Pass when no residual empty ANTHROPIC_API_KEY assignment remains in .env.

    Every ``ANTHROPIC_API_KEY=`` line is inspected: a single empty assignment
    fails even when another line assigns a real value, because the later empty
    line wins and re-breaks the key.
    """
    path = workspace / ".env"
    if not path.is_file():
        return True, "no .env file, nothing left empty"
    found = False
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped.startswith("ANTHROPIC_API_KEY="):
            continue
        found = True
        value = stripped.split("=", 1)[1].strip()
        if not (value and value.strip("\"'")):
            return False, "ANTHROPIC_API_KEY is still assigned an empty value"
    if found:
        return True, "ANTHROPIC_API_KEY has a non-empty value"
    return True, "ANTHROPIC_API_KEY assignment removed"


SCORERS: dict[str, Scorer] = {
    "build-skill": score_build_skill,
    "add-mcp-server": score_add_mcp_server,
    "write-eval-gate": score_write_eval_gate,
    "fix-empty-api-key": score_fix_empty_api_key,
}


def score_run(task: HarnessTask, run: AgentRun) -> TaskScore:
    """Score a run by dispatching on ``task.category`` (KeyError if unknown)."""
    scorer = SCORERS[task.category]
    passed, detail = scorer(run.workspace, run.transcript)
    return TaskScore(
        task_id=run.task_id,
        condition=run.condition,
        success=passed,
        detail=detail,
        total_tokens=run.total_tokens,
        errors=run.errors,
    )
