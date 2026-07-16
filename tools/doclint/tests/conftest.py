"""Shared fixtures for the doclint suite.

Nothing is mocked. Every test that is not the one end-to-end case copies the
miniature ``fixtures/clean_repo`` tree into a tmp dir, optionally mutates it,
and runs the real linter over it: a genuine filesystem walk and a genuine
``ast`` parse over a small tree. Only ``test_real_repo_docs_are_clean`` points
at the real repo.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from agentos_doclint import main

FIXTURES = Path(__file__).parent / "fixtures"
CLEAN_REPO = FIXTURES / "clean_repo"

# The worktree root: tests/ -> doclint/ -> tools/ -> repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]

# Type of the run_lint callable: takes a repo root, returns (exit_code, output).
RunLint = Callable[[Path], tuple[int, str]]


@pytest.fixture
def clean_repo(tmp_path: Path) -> Path:
    """A fresh, writable copy of the clean fixture tree per test."""
    dest = tmp_path / "repo"
    shutil.copytree(CLEAN_REPO, dest)
    return dest


@pytest.fixture
def run_lint(capsys: pytest.CaptureFixture[str]) -> RunLint:
    """Drive the public CLI entrypoint and return (exit_code, combined output).

    Tests assert through the exit code and message text only, never internal
    function names, so renaming a helper does not break the suite.
    """

    def _run(root: Path) -> tuple[int, str]:
        code = main(["--repo-root", str(root)])
        captured = capsys.readouterr()
        return code, captured.out + captured.err

    return _run


def write(root: Path, rel: str, content: str) -> Path:
    """Write a file under the repo, creating parent dirs."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path
