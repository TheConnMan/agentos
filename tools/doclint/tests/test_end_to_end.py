"""The clean-tree floor and the one real-repo end-to-end test."""

from __future__ import annotations

from pathlib import Path

from .conftest import REPO_ROOT, RunLint


# --- Test 5: the correct fixture tree passes -------------------------------


def test_clean_tree_passes(clean_repo: Path, run_lint: RunLint) -> None:
    # The test that stops a linter that just always fails.
    code, out = run_lint(clean_repo)
    assert code == 0, out


# --- Test 15: the real repo docs are clean ---------------------------------


def test_real_repo_docs_are_clean(run_lint: RunLint) -> None:
    # The only test that points at the real docs/ tree. Green only once Streams
    # B and C are done; this is the test that proves the ticket.
    code, out = run_lint(REPO_ROOT)
    assert code == 0, out
