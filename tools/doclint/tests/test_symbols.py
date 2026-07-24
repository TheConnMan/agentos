"""ast-based symbol resolution: the four resolution forms, and syntax errors.

Nothing is imported or executed; the resolver parses the cited file with ast.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import RunLint, write

APPROVAL = "runner/src/curie_runner/approval.py"


# --- Test 2: unresolvable symbol fails -------------------------------------


def test_unresolvable_symbol_fails(clean_repo: Path, run_lint: RunLint) -> None:
    write(clean_repo, "docs/miss.md", f"See `{APPROVAL}::no_such_function`.\n")
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "no_such_function" in out  # names the symbol
    assert "does not resolve" in out.lower()  # names the reason


# --- Test 2 partner: all four resolution forms pass ------------------------


@pytest.mark.parametrize(
    "symbol",
    [
        "authorize_approval",  # module-level function
        "ApprovalGate",  # class
        "ApprovalGate.consume_grant",  # method
        "build_options",  # ImportFrom-bound name
    ],
)
def test_resolvable_symbol_passes(
    clean_repo: Path, run_lint: RunLint, symbol: str
) -> None:
    # Without this positive partner, a linter that fails everything would pass
    # the negative test above.
    write(clean_repo, "docs/hit.md", f"See `{APPROVAL}::{symbol}`.\n")
    code, _ = run_lint(clean_repo)
    assert code == 0


# --- Test 14: a syntax error in a cited file is a clean lint failure -------


def test_syntax_error_in_cited_file_reports_cleanly(
    clean_repo: Path, run_lint: RunLint
) -> None:
    write(
        clean_repo,
        "runner/src/curie_runner/broken.py",
        "def oops(:\n    pass\n",  # deliberate syntax error
    )
    write(
        clean_repo,
        "docs/broken_cite.md",
        "See `runner/src/curie_runner/broken.py::oops`.\n",
    )
    # Must not raise an unhandled SyntaxError; must fail naming the file.
    code, out = run_lint(clean_repo)
    assert code != 0
    assert "broken.py" in out
    assert "syntax" in out.lower()
